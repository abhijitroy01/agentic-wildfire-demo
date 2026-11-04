import asyncio
import json
import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from traffic_simulator.models import Scenario
from traffic_simulator.persistence import commit

from backend import api_settings as settings
from backend import demo_access, runtime_client, trusted_context
from backend.evacuation import service
from backend.investigation import broker as investigation_broker
from backend.tests.builders import client, owner_of, prepared, submission


def test_cookie_is_opaque_secure_and_never_returned_in_json(demo):
    app, store = demo
    with client(app) as browser:
        response = browser.post("/api/v1/demo/session")
        assert response.status_code == 200
        assert response.json()["session_created"] is True
        cookie = response.headers["set-cookie"]
        assert all(flag in cookie for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"))
        assert "Domain=" not in cookie
        token = browser.cookies.get(demo_access.COOKIE)
        assert len(token) == 43 and token not in response.text
        records = store.get("demo_session:" + demo_access.digest(token))
        assert token not in json.dumps(records)
        assert response.headers["cache-control"] == "no-store"
        reused = browser.post("/api/v1/demo/session")
        assert "set-cookie" not in reused.headers
        assert reused.json()["session_created"] is False


@pytest.mark.parametrize("headers", [
    {"origin": "https://attacker.example"}, {"origin": "null"}, {"origin": ""},
    {"sec-fetch-site": "cross-site"}, {"x-lab-workspace": ""},
])
def test_bootstrap_requires_trusted_route_and_same_origin(demo, headers):
    with client(demo[0]) as browser:
        response = browser.post("/api/v1/demo/session", headers=headers)
        assert response.status_code in (403, 404)
        assert "set-cookie" not in response.headers


def test_forged_expired_and_other_browser_cannot_access_runs(demo, monkeypatch):
    app, store = demo
    monkeypatch.setattr("traffic_simulator.traffic.configured", lambda: True)
    with client(app) as alice, client(app) as bob:
        alice.post("/api/v1/demo/session")
        token = alice.cookies.get(demo_access.COOKIE)
        owner = "demo-" + demo_access.digest(token)
        _, _, draft = prepared(store, owner)
        run = service.submit(store, owner, submission(draft))
        path = "/api/v1/runs/" + run["run_id"]
        assert alice.get(path).status_code == 200
        bob.post("/api/v1/demo/session")
        assert bob.get(path).status_code == 404
        assert bob.get(path+"/artifacts/result").status_code == 404
        assert bob.post(path+"/cancel", json={"request_id": str(uuid4())}).status_code == 404
        assert bob.get(path, headers={"cookie": demo_access.COOKIE+"="+"A"*43}).status_code == 401
        saved = store.get("demo_session:" + demo_access.digest(token))
        revision = saved["revision"]
        saved.update(revision=revision+1, expires=time.time()-1)
        commit(store, [(saved, revision)])
        assert alice.get(path).status_code == 401


def test_cross_origin_write_is_rejected_even_with_valid_cookie(demo):
    with client(demo[0]) as browser:
        browser.post("/api/v1/demo/session")
        response = browser.post("/api/v1/sessions", headers={"origin": "https://attacker.example"},
                                json={"request_id": str(uuid4()), "entry_mode": "lab"})
        assert response.status_code == 403


def test_oversized_body_is_rejected_before_model_or_provider(demo):
    with client(demo[0]) as browser:
        browser.post("/api/v1/demo/session")
        largest = settings.request_body_max_kib() * 1024
        response = browser.post("/api/v1/investigation/turns", content=b"x"*(largest+1))
        assert response.status_code == 413
        # The refusal uses the v1 error envelope like every other API error.
        assert response.json() == {"error": {"code": "BODY_TOO_LARGE", "message": "Reduce the request size.",
                                             "retryable": False,
                                             "request_id": response.json()["error"]["request_id"], "details": {}}}
        assert UUID(response.json()["error"]["request_id"])


@pytest.mark.asyncio
async def test_a_body_still_arriving_at_the_deadline_gets_408_in_the_error_envelope():
    """A body that takes longer than REQUEST_BODY_SECONDS never reaches the app; the caller gets the v1 envelope."""
    from backend.request_limits import RequestLimits

    async def application(scope, receive, send):
        pytest.fail("A timed-out request body reached the application")

    async def slow_body():
        await asyncio.sleep(5)
        return {"type": "http.request", "body": b"{}", "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    limits = RequestLimits(application)
    limits.seconds = 0.05
    await limits({"type": "http", "method": "POST", "path": "/api/v1/sessions", "headers": []}, slow_body, send)
    assert sent[0]["status"] == 408
    body = json.loads(sent[1]["body"])
    assert UUID(body["error"]["request_id"])
    assert body == {"error": {"code": "REQUEST_TIMEOUT", "message": "Request body timed out.",
                              "retryable": False, "request_id": body["error"]["request_id"], "details": {}}}


def test_body_limit_and_deadline_follow_the_settings(monkeypatch):
    from backend.request_limits import RequestLimits
    monkeypatch.setenv("REQUEST_BODY_MAX_KIB", "64")
    monkeypatch.setenv("REQUEST_BODY_SECONDS", "5")
    limits = RequestLimits(app=None)
    assert (limits.max_bytes, limits.seconds) == (64 * 1024, 5)


def test_new_demo_sessions_per_viewer_ip_are_limited_per_day(demo, monkeypatch):
    monkeypatch.setenv("DEMO_SESSIONS_PER_IP_PER_DAY", "2")
    monkeypatch.setenv("DEMO_SESSION_DAYS", "2")
    app, _ = demo
    viewer = {"x-lab-viewer-ip": "198.51.100.7"}
    for _ in range(2):
        with client(app) as browser:
            started = browser.post("/api/v1/demo/session", headers=viewer)
            assert started.status_code == 200 and f"Max-Age={2 * 86400}" in started.headers["set-cookie"]
            # A browser that already holds a valid session spends nothing.
            assert browser.post("/api/v1/demo/session", headers=viewer).json()["session_created"] is False
    with client(app) as browser:
        refused = browser.post("/api/v1/demo/session", headers=viewer)
        assert refused.status_code == 429 and "set-cookie" not in refused.headers
        assert refused.json()["error"]["code"] == "DEMO_SESSION_LIMIT"
        assert 0 < int(refused.headers["retry-after"]) <= 86400
        assert browser.post("/api/v1/demo/session", headers={"x-lab-viewer-ip": "203.0.113.9"}).status_code == 200


def test_daily_agent_budget_stops_runtime_calls_for_every_visitor(demo, monkeypatch):
    """Past the shared daily agent budget, every visitor's investigation gets 429 before any Runtime call."""
    monkeypatch.setenv("AGENT_REQUESTS_PER_DAY", "1")
    monkeypatch.setenv("SIMULATION_CONTEXT_SECRET", "fixture-secret-long-enough-not-a-real-credential")
    trusted_context.clear_secret_cache()
    calls = []

    def invoke(payload, **kwargs):
        assert 0 < kwargs["timeout_seconds"] <= 210
        calls.append(payload)
        yield {"type": "RUN_FINISHED"}
    monkeypatch.setattr(runtime_client, "invoke_runtime", invoke)
    body = {"question": "Show current weather.", "thread_id": "local-thread", "context_envelope": {}}
    with client(demo[0]) as alice, client(demo[0]) as bob:
        for browser in (alice, bob):
            browser.post("/api/v1/demo/session")
        assert alice.post("/api/v1/investigation/turns", json=body).status_code == 200
        limited = bob.post("/api/v1/investigation/turns", json=body)
        assert limited.status_code == 429 and limited.json()["error"]["code"] == "DAILY_LIMIT_REACHED"
        assert "Daily demo limit reached for AI agent requests" in limited.json()["error"]["message"]
        assert int(limited.headers["retry-after"]) > 0
    assert len(calls) == 1
    trusted_context.clear_secret_cache()


def test_investigation_binds_runtime_identity_in_a_signed_context_only(demo, monkeypatch):
    """The Runtime's investigation actor and session come only from a context signed for investigation."""
    monkeypatch.setenv("SIMULATION_CONTEXT_SECRET", "fixture-secret-long-enough-not-a-real-credential")
    trusted_context.clear_secret_cache()
    calls = []

    def invoke(payload, **kwargs):
        assert 0 < kwargs["timeout_seconds"] <= 210
        calls.append(payload)
        yield {"type": "RUN_FINISHED"}
    monkeypatch.setattr(runtime_client, "invoke_runtime", invoke)
    owners = []
    with client(demo[0]) as alice, client(demo[0]) as bob:
        body = {"question": "Show current weather.", "thread_id": "same-local-thread",
                "context_envelope": {"actor_id": "forged-user", "workspace_mode": "authenticated"}}
        for browser in (alice, bob):
            browser.post("/api/v1/demo/session")
            owners.append(owner_of(browser))
            assert browser.post("/api/v1/investigation/turns", json=body).status_code == 200
            assert browser.post("/api/v1/investigation/turns", json={
                **body, "forwardedProps": {"actor_id": "another-owner"}}).status_code == 422
            assert browser.post("/api/v1/investigation/turns", json={
                **body, "agentRuntimeArn": "forged-runtime"}).status_code == 422
    assert calls[0]["threadId"] != calls[1]["threadId"]
    for payload, owner in zip(calls, owners, strict=True):
        forwarded = payload["forwardedProps"]
        # Nothing unsigned names a workspace mode, actor or session for the Runtime to trust.
        assert set(forwarded) == {"prompt", "context_envelope", "investigation_context"}
        # The forged actor_id and workspace_mode are dropped; only retained observations are forwarded.
        assert forwarded["context_envelope"] == {"previous_observations": {}}
        # The context carries the canonical investigation claims and verifies with that purpose's key.
        claims = trusted_context.verify(forwarded["investigation_context"], "investigation")
        assert claims["purpose"] == "investigation" and claims["allowed_operations"] == []
        assert claims["sub"] == demo_access.digest(owner) and claims["session_id"] == payload["threadId"]
        assert claims["exp"] - claims["iat"] <= 300
        # Nothing signs with the secret itself.
        with pytest.raises(trusted_context.jwt.InvalidSignatureError):
            trusted_context.jwt.decode(
                forwarded["investigation_context"], trusted_context.secret(), algorithms=["HS256"],
                audience=trusted_context.AUDIENCE, issuer=trusted_context.ISSUER)
        # The internal tool route refuses an investigation context: it binds no turn or location.
        with pytest.raises(trusted_context.jwt.PyJWTError):
            trusted_context.verify(forwarded["investigation_context"], "simulation")
    trusted_context.clear_secret_cache()


def test_investigation_forwards_only_retained_observations_never_client_envelope_keys(demo, monkeypatch):
    """A posted envelope's run_id, evidence and other keys never reach the Runtime; only retained receipts do."""
    monkeypatch.setenv("SIMULATION_CONTEXT_SECRET", "fixture-secret-long-enough-not-a-real-credential")
    trusted_context.clear_secret_cache()
    envelopes = []
    point = {"latitude": 38.6, "longitude": -121.5}
    source = {"location": point, "mapData": {"summary": {"totalActiveFires": 1},
                                             "locations": [{**point, "fire": {"name": "Fixture Fire"}}]}}

    def invoke(payload, **_kwargs):
        envelopes.append(payload["forwardedProps"]["context_envelope"])
        yield {"type": "STATE_SNAPSHOT", "snapshot": {"content": source}}
        yield {"type": "RUN_FINISHED"}

    monkeypatch.setattr(runtime_client, "invoke_runtime", invoke)
    # In the Runtime, a truthy run_id with evidence selects the planning branch (wfm_agui_entrypoint.py).
    forged = {"run_id": str(uuid4()), "evidence": [{"view": "summary", "forged": True}],
              "workspace_mode": "authenticated", "previous_observations": {"forged": True}}
    with client(demo[0]) as browser:
        browser.post("/api/v1/demo/session")
        for _ in range(2):
            response = browser.post("/api/v1/investigation/turns", json={
                "thread_id": "planning", "question": "Which road should crews use?", "context_envelope": forged})
            assert response.status_code == 200
    # The first turn has no receipt yet; the second gets the first turn's completed receipt and nothing else.
    assert envelopes == [{"previous_observations": {}}, {"previous_observations": source}]
    trusted_context.clear_secret_cache()


def test_investigation_retains_only_completed_owner_and_conversation_scoped_observations(demo, monkeypatch):
    monkeypatch.setenv("SIMULATION_CONTEXT_SECRET", "fixture-secret-long-enough-not-a-real-credential")
    trusted_context.clear_secret_cache()
    calls = []
    point = {"latitude": 38.6, "longitude": -121.5}
    fire = {**point, "fire": {"name": "Fixture Fire"}}
    source = {"location": point, "mapData": {"summary": {"totalActiveFires": 1}, "locations": [fire]}}

    def invoke(payload, **_kwargs):
        calls.append(payload["forwardedProps"]["context_envelope"]["previous_observations"])
        yield {"type": "STATE_SNAPSHOT", "snapshot": {"content": source}}
        if payload["forwardedProps"]["prompt"] == "failed":
            yield {"type": "RUN_ERROR"}
        else:
            yield {"type": "RUN_FINISHED"}

    monkeypatch.setattr(runtime_client, "invoke_runtime", invoke)
    with client(demo[0]) as alice, client(demo[0]) as bob:
        for browser in (alice, bob):
            browser.post("/api/v1/demo/session")
        def ask(browser, thread="one", question="fire"):
            response = browser.post("/api/v1/investigation/turns", json={
                "thread_id": thread, "question": question,
                "context_envelope": {"previous_observations": {"forged": True}}})
            assert response.status_code == 200
        ask(alice)
        ask(alice, question="weather")
        ask(bob)
        ask(alice, thread="two", question="failed")
        ask(alice, thread="two")
    assert calls == [{}, source, {}, {}, {}]
    weather = {"location": point, "mapData": {"summary": None, "locations": [
        {**point, "currentWeather": {"temperature": {"value": 90, "unit": "degF"}}}]}}
    merged = investigation_broker.retain_investigation_observations(source, weather)
    assert merged["mapData"]["locations"][0]["fire"] == fire["fire"]
    assert merged["mapData"]["locations"][0]["currentWeather"]["temperature"]["value"] == 90
    assert merged["mapData"]["summary"] == source["mapData"]["summary"]
    empty = {"location": point, "mapData": {"summary": {"totalActiveFires": 0}, "locations": []}}
    assert investigation_broker.retain_investigation_observations(merged, empty)["mapData"]["locations"] == []
    after_empty = investigation_broker.retain_investigation_observations(empty, weather)
    assert after_empty["mapData"]["summary"]["totalActiveFires"] == 0
    assert not any(loc.get("fire") for loc in after_empty["mapData"]["locations"])
    other_area = {"location": {"latitude": 34.0, "longitude": -118.0}, "mapData": {"locations": []}}
    assert investigation_broker.retain_investigation_observations(merged, other_area) == other_area
    trusted_context.clear_secret_cache()


def test_investigation_without_a_signing_key_is_unavailable_and_holds_no_lease(demo, monkeypatch):
    """Without a readable signing secret an investigation gets 503 AGENT_NOT_CONFIGURED and takes no lease."""
    monkeypatch.delenv("SIMULATION_CONTEXT_SECRET", raising=False)
    monkeypatch.delenv("SIMULATION_CONTEXT_SECRET_ARN", raising=False)
    trusted_context.clear_secret_cache()
    monkeypatch.setattr(runtime_client, "invoke_runtime", lambda payload, **_kwargs: pytest.fail("Unsigned Runtime call"))
    app, store = demo
    with client(app) as browser:
        browser.post("/api/v1/demo/session")
        response = browser.post("/api/v1/investigation/turns", json={
            "question": "Show current weather.", "thread_id": "local-thread", "context_envelope": {}})
        assert response.status_code == 503 and response.json()["error"]["code"] == "AGENT_NOT_CONFIGURED"
        assert store.get("investigation_lease:" + demo_access.digest(owner_of(browser))) is None
    trusted_context.clear_secret_cache()


def test_deployed_requests_outside_the_open_public_route_are_refused(demo, monkeypatch):
    app, _ = demo
    with client(app) as browser:
        browser.post("/api/v1/demo/session")
        assert browser.get("/api/v1/runs").status_code == 200
        monkeypatch.setattr("traffic_simulator.settings.CLOUD", True)
        assert browser.get("/api/v1/runs").status_code == 200
        # Without CloudFront's workspace header, neither the cookie nor a bearer token is a credential.
        for headers in ({"x-lab-workspace": ""}, {"x-lab-workspace": "", "authorization": "Bearer forged"}):
            direct = browser.get("/api/v1/runs", headers=headers)
            assert direct.status_code == 403 and direct.json()["error"]["code"] == "WORKSPACE_CLOSED"
            assert browser.get("/api/v1/workspace-config", headers=headers).json()["access_mode"] == "closed"
        # Closing public access (the first deploy pass, or staging) closes the CloudFront route as well.
        monkeypatch.setenv("PUBLIC_DEMO_MODE", "false")
        closed = browser.get("/api/v1/research/jobs")
        assert closed.status_code == 403 and closed.json()["error"]["code"] == "WORKSPACE_CLOSED"
        assert browser.post("/api/v1/demo/session").status_code == 404
        assert browser.get("/api/v1/workspace-config").json()["access_mode"] == "closed"
    # Local development has no cookie and one owner.
    monkeypatch.setattr("traffic_simulator.settings.CLOUD", False)
    local = TestClient(app)
    assert local.get("/api/v1/workspace-config").json()["access_mode"] == "local"
    created = local.post("/api/v1/sessions", json={"request_id": str(uuid4()), "entry_mode": "lab"})
    assert created.status_code == 200
    assert demo[1].get(f"session:{created.json()['session_id']}")["subject"] == demo_access.LOCAL_OWNER


def test_demo_sessions_use_the_traffic_lab_scoped_to_their_own_runs(demo):
    app, store = demo
    with client(app) as alice, client(app) as bob:
        for browser in (alice, bob):
            browser.post("/api/v1/demo/session")
        assert alice.get("/api/v1/research/config").status_code == 200
        created = alice.post("/api/v1/research/jobs", json={**Scenario().model_dump(), "request_id": str(uuid4())})
        assert created.status_code == 202
        job_id = created.json()["id"]
        assert store.get(job_id)["owner"] == owner_of(alice)
        assert [job["id"] for job in alice.get("/api/v1/research/jobs").json()] == [job_id]
        assert bob.get("/api/v1/research/jobs").json() == []
        for path in (f"/api/v1/research/jobs/{job_id}", f"/api/v1/research/jobs/{job_id}/artifacts/result"):
            assert bob.get(path).status_code == 404
        assert bob.post(f"/api/v1/research/jobs/{job_id}/cancel").status_code == 404
        # Writes keep the v1 same-origin rule; a request without a session cookie is asked for one.
        forged = alice.post("/api/v1/research/jobs", json={**Scenario().model_dump(), "request_id": str(uuid4())},
                            headers={"origin": "https://attacker.example"})
        assert forged.status_code == 403 and forged.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"
    anonymous = client(app).get("/api/v1/research/jobs")
    assert anonymous.status_code == 401 and anonymous.json()["error"]["code"] == "DEMO_SESSION_REQUIRED"
