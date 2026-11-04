"""The workspace API keeps keyed URLs, cookies and tokens out of its logs, and logs security events.

The entry-point checks run backend.app in a fresh interpreter, which also shows which docs routes it serves.
"""
import json
import logging
import os
import subprocess  # nosec B404 - runs only this interpreter with the fixed code strings below, never a shell
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from traffic_simulator import settings as simulator_settings
from traffic_simulator.persistence import commit, digest, record
from traffic_simulator.store import Store

from backend import demo_access, radio_broker, runtime_client, trusted_context
from backend.evacuation import agent_broker, service
from backend.tests.builders import client, owner_of, prepared, submission

REPOSITORY = Path(__file__).resolve().parents[2]
# The loggers' own levels: unset (0) would inherit uvicorn's INFO root, which logs keyed request URLs.
CHECK = ("import logging, backend.app; "
         "print(*(logging.getLogger(name).level for name in ('httpx', 'httpcore')))")
PUBLIC_ORIGIN = "https://workspace.example"
# A new demo session (an INFO event) and a refused read (WARNING) through the deployed entry point.
VISIT = ("import uuid; from fastapi.testclient import TestClient; import backend.app as main; "
         f"browser = TestClient(main.app, base_url={PUBLIC_ORIGIN!r}, headers={{'origin': {PUBLIC_ORIGIN!r}, "
         "'sec-fetch-site': 'same-origin', 'x-lab-workspace': 'public-demo'}); "
         "browser.post('/api/v1/demo/session'); browser.get(f'/api/v1/runs/{uuid.uuid4()}')")
SECURITY_LOGGER = "backend.security_events"
TOOLS = "/api/internal/v1/simulations/"
# The interactive docs, their OAuth redirect page, ReDoc and the schema, then /health, through the deployed entry point.
DOCS = ("from fastapi.testclient import TestClient; import backend.app as main; client = TestClient(main.app); "
        "print(*(client.get(path).status_code for path in "
        "('/docs', '/docs/oauth2-redirect', '/redoc', '/openapi.json', '/health')))")
REFUSE_RISK_SETTINGS = """
import asyncio
from unittest.mock import patch
from pydantic import ValidationError
import backend.app as main

async def start():
    with patch("backend.app.threading.Thread.start", side_effect=AssertionError("Worker started before validation")):
        try:
            async with main.lifespan(main.app):
                raise AssertionError("API accepted invalid risk settings")
        except ValidationError as error:
            print(error.errors()[0]["msg"])

asyncio.run(start())
"""


def api_process(tmp_path, code, **variables):
    """Run code in a fresh interpreter that imports the workspace API (local mode unless variables say otherwise)."""
    environment = {k: v for k, v in os.environ.items() if not k.startswith("COGNITO_")}
    environment.update(STORAGE_MODE="local", DATA_DIR=str(tmp_path / "data"), PYTHONPATH=os.pathsep.join(
        str(REPOSITORY / folder) for folder in ("traffic-simulator", "fire-risk-modeler", ".")))
    environment.update(variables)
    # A fresh interpreter: the pytest session's logger levels and handlers are shared by every test. The code is one
    # of this module's fixed strings; no shell.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    result = subprocess.run([sys.executable, "-c", code], env=environment, cwd=tmp_path,  # nosec B603
                            capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr
    return result


def aws_task_variables():
    """Return an AWS-mode API task's variables: fixture storage names, the public URL and each settings.toml value."""
    values = {name: ",".join(spec["value"]) if isinstance(spec["value"], list) else str(spec["value"])
              for name, spec in simulator_settings.operational_settings().items()}
    return {**values, "STORAGE_MODE": "aws", "AWS_REGION": "us-west-2", "ARTIFACT_BUCKET": "fixture-artifacts",
            "JOBS_TABLE": "fixture-jobs", "QUEUE_URL": "https://sqs.us-west-2.amazonaws.com/123456789012/fixture",
            "FABRIC_PUBLIC_URL": PUBLIC_ORIGIN, "PUBLIC_DEMO_MODE": "true", "AWS_EC2_METADATA_DISABLED": "true"}


@pytest.fixture
def security_log(caplog):
    """Capture workspace API security events from INFO up; return a reader of their parsed JSON lines."""
    caplog.set_level(logging.INFO, logger=SECURITY_LOGGER)
    return lambda: [json.loads(line.getMessage()) for line in caplog.records if line.name == SECURITY_LOGGER]


def test_api_entry_point_quiets_http_client_loggers(tmp_path):
    result = api_process(tmp_path, CHECK)
    assert result.stdout.split() == ["30", "30"]  # logging.WARNING for httpx and httpcore


def test_api_entry_point_writes_security_events_to_the_container_log(tmp_path):
    # uvicorn configures only its own loggers, so main.py gives security events a stderr handler;
    # without it the INFO event would be dropped (Python's last-resort handler prints WARNING and up).
    result = api_process(tmp_path, VISIT, PUBLIC_DEMO_MODE="true", FABRIC_PUBLIC_URL=PUBLIC_ORIGIN)
    created, refused = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    owner = created.pop("owner_digest")
    assert created == {"security_event": "demo_session_created", "outcome": "allowed", "method": "POST",
                       "route": "/api/v1/demo/session"}
    assert refused == {"security_event": "request_refused", "outcome": "denied", "code": "RUN_NOT_FOUND",
                       "status": 404, "reason": "missing", "method": "GET", "route": "/api/v1/runs/{run_id}",
                       "owner_digest": owner}


def test_api_entry_point_serves_docs_and_schema_routes_only_in_local_mode(tmp_path):
    """Local development serves /docs, /redoc and /openapi.json; an AWS-mode task serves none of them."""
    assert api_process(tmp_path, DOCS).stdout.split() == ["200"] * 5
    # The deployed API still answers its health check.
    assert api_process(tmp_path, DOCS, **aws_task_variables()).stdout.split() == ["404"] * 4 + ["200"]


@pytest.mark.parametrize("variables, message", [
    ({"FIRE_RISK_INNER_KM": "20"}, "Distance bands must increase strictly"),
    ({"FIRE_RISK_INNER_KM": "15"}, "Distance bands must increase strictly"),
    ({"FIRE_RISK_SURFACE_FIRST_MINUTES": "60"}, "Scenario horizons must increase strictly"),
    ({"FIRE_RISK_SURFACE_FIRST_MINUTES": "30"}, "Scenario horizons must increase strictly"),
])
def test_api_startup_refuses_in_range_but_unordered_risk_settings_before_worker(tmp_path, variables, message):
    """Each value is individually valid; startup must also enforce the relationship between the three values."""
    result = api_process(tmp_path, REFUSE_RISK_SETTINGS, **variables)
    assert result.stdout.strip() == "Value error, " + message


def test_new_demo_sessions_and_cookie_refusals_are_logged_without_the_cookie(demo, security_log, caplog):
    app, store = demo
    with client(app) as browser:
        started = browser.post("/api/v1/demo/session", headers={"x-amz-cf-id": "Edge-Request_1=="})
        assert started.status_code == 200
        token, owner = browser.cookies.get(demo_access.COOKIE), owner_of(browser)
    saved = store.get("demo_session:" + digest(token))
    revision = saved["revision"]
    saved.update(revision=revision+1, expires=time.time()-1)
    commit(store, [(saved, revision)])
    for cookie in (None, "not-a-session-token", "A"*43, token):
        headers = {"cookie": f"{demo_access.COOKIE}={cookie}"} if cookie else {}
        assert client(app).get("/api/v1/runs", headers=headers).status_code == 401
    created, *refusals = security_log()
    assert created == {"security_event": "demo_session_created", "outcome": "allowed", "method": "POST",
                       "route": "/api/v1/demo/session", "owner_digest": digest(owner),
                       "request_id": "Edge-Request_1=="}
    assert [(event["code"], event["status"], event["reason"], event["route"]) for event in refusals] == [
        ("DEMO_SESSION_REQUIRED", 401, reason, "/api/v1/runs")
        for reason in ("no_cookie", "malformed_cookie", "unknown_session", "expired_session")]
    # A refused caller has no owner yet, and no line holds the cookie or the owner it names.
    assert not any("owner_digest" in event for event in refusals)
    assert token not in caplog.text and owner not in caplog.text


def test_cross_owner_reads_and_cross_origin_writes_are_refusals_naming_the_caller(demo, security_log,
                                                                                    monkeypatch):
    app, store = demo
    monkeypatch.setattr("traffic_simulator.traffic.configured", lambda: True)
    with client(app) as alice, client(app) as bob:
        for browser in (alice, bob):
            browser.post("/api/v1/demo/session")
        _, _, draft = prepared(store, owner_of(alice))
        run = service.submit(store, owner_of(alice), submission(draft))
        # A malformed edge request ID is left out rather than logged.
        probe = bob.get("/api/v1/runs/" + run["run_id"], headers={"x-amz-cf-id": "forged id"})
        assert probe.status_code == 404
        other = bob.get(f"/api/v1/runs/{uuid4()}").json()
        assert {k: v for k, v in probe.json()["error"].items() if k != "request_id"} == {
            k: v for k, v in other["error"].items() if k != "request_id"}
        assert bob.get("/api/v1/scenario-drafts/" + draft["draft_id"]).status_code == 404
        assert bob.post("/api/v1/sessions", headers={"origin": "https://attacker.example"},
                        json={"request_id": str(uuid4()), "entry_mode": "lab"}).status_code == 403
        assert alice.get("/api/v1/runs/" + run["run_id"]).status_code == 200
        bob_digest = digest(owner_of(bob))
    refusals = [event for event in security_log() if event["outcome"] == "denied"]
    assert [(e["code"], e["reason"], e["route"], e.get("owner_digest")) for e in refusals] == [
        ("RUN_NOT_FOUND", "other_owner", "/api/v1/runs/{run_id}", bob_digest),
        ("RUN_NOT_FOUND", "missing", "/api/v1/runs/{run_id}", bob_digest),
        ("NOT_FOUND", "other_owner", "/api/v1/scenario-drafts/{draft_id}", bob_digest),
        # The same-origin check runs before the session lookup, so this refusal names no owner.
        ("ORIGIN_NOT_ALLOWED", "origin_mismatch", "/api/v1/sessions", None),
    ]
    assert not any("request_id" in event for event in refusals)


def test_agent_tool_calls_and_service_context_refusals_are_audited_without_the_token(
        tmp_path, monkeypatch, security_log, caplog, api_app):
    """Tool calls are audited, and contexts with another key, the bare secret or another purpose are refused."""
    secret = "fixture-secret-long-enough-not-a-real-credential"
    monkeypatch.setenv("SIMULATION_CONTEXT_SECRET", secret)
    trusted_context.clear_secret_cache()
    monkeypatch.setattr("traffic_simulator.traffic.configured", lambda: True)
    store = Store(tmp_path)
    session, incident, draft = prepared(store)
    run = service.submit(store, "alice", submission(draft))
    turn_id = str(uuid4())
    commit(store, [(record("turn", turn_id, "alice", status="running", attempt_id="attempt",
                           lease_until=time.time()+100, effects=[]), None)])
    claims = {"sub": "alice", "session_id": session["session_id"], "incident_id": incident["incident_id"],
              "location_revision": 1, "turn_id": turn_id, "attempt_id": "attempt",
              "allowed_operations": ["get_simulation_status"]}
    token = trusted_context.sign(claims, "simulation")
    app = api_app(store, "alice", agent_broker.create_broker_router)
    tools = TestClient(app)
    status = {"run_id": run["run_id"]}
    signed = {"x-simulation-context": token}
    assert tools.post(TOOLS + "get_simulation_status", json=status, headers=signed).status_code == 200
    assert tools.post(TOOLS + "submit_simulation", json={}, headers=signed).status_code == 403
    signed_claims = trusted_context.verify(token, "simulation")
    # Another key, the signing secret itself (the earlier token format) and a context signed for another purpose.
    for forged in (trusted_context.jwt.encode({"sub": "alice"}, "another-signing-key-long-enough-for-hmac-sha256",
                                              algorithm="HS256"),
                   trusted_context.jwt.encode(signed_claims, secret, algorithm="HS256"),
                   trusted_context.sign({**claims, "allowed_operations": []}, "investigation")):
        assert tools.post(TOOLS + "get_simulation_status", json=status,
                          headers={"x-simulation-context": forged}).status_code == 401
    assert tools.post(TOOLS + "get_simulation_status", json=status).status_code == 401
    completed, *refusals = security_log()
    assert completed == {"security_event": "agent_tool_call", "outcome": "allowed",
                         "operation": "get_simulation_status", "turn_id": turn_id, "method": "POST",
                         "route": TOOLS + "{operation}", "owner_digest": digest("alice")}
    assert [(event["code"], event["status"], event.get("reason"), event.get("owner_digest"))
            for event in refusals] == [
        ("OPERATION_NOT_AUTHORIZED", 403, None, digest("alice")),
        *[("INVALID_SERVICE_CONTEXT", 401, "InvalidSignatureError", None)] * 3,
        ("INVALID_SERVICE_CONTEXT", 401, "DecodeError", None),
    ]
    # Neither the signed claims nor the signature of the capability token reach the log.
    assert not any(part in caplog.text for part in token.split(".")[1:])
    trusted_context.clear_secret_cache()


def test_only_access_and_limit_refusals_are_security_events(demo, security_log, monkeypatch):
    monkeypatch.setenv("DEMO_SESSIONS_PER_IP_PER_DAY", "1")
    monkeypatch.setenv("MAX_STUDY_RADIUS_KM", "5")
    app, _ = demo
    viewer = {"x-lab-viewer-ip": "198.51.100.7"}
    with client(app) as browser:
        assert browser.post("/api/v1/demo/session", headers=viewer).status_code == 200
        # A 422 for an input past a configured limit is an ordinary answer, not an access decision.
        assert browser.post("/api/v1/research/discover", json={"radius_km": 8}).status_code == 422
    with client(app) as other:
        assert other.post("/api/v1/demo/session", headers=viewer).status_code == 429
    events = security_log()
    assert [(event["security_event"], event.get("code")) for event in events] == [
        ("demo_session_created", None), ("request_refused", "DEMO_SESSION_LIMIT")]
    assert (events[1]["status"], events[1]["route"]) == (429, "/api/v1/demo/session")
    # The viewer address that keys the per-network limit is never written to the log.
    assert "198.51.100.7" not in json.dumps(events)


def test_failed_investigations_and_radio_relays_log_only_the_exception_type(demo, monkeypatch, caplog):
    """A swallowed broker failure leaves its exception type in the server log, never its text (a keyed URL)."""
    detail = "https://provider.example/stream?key=" + "fixture-provider-" + "detail"
    monkeypatch.setenv("SIMULATION_CONTEXT_SECRET", "fixture-secret-long-enough-not-a-real-credential")
    monkeypatch.setenv("RADIO_SERVICE_URL", "http://radio.fixture.internal:8000")
    monkeypatch.setenv("BROADCASTIFY_PRIVATE_KEY", "private-fixture-key-" + "x" * 40)
    trusted_context.clear_secret_cache()

    def unreachable(*_args, **_kwargs):
        raise ConnectionError(detail)

    monkeypatch.setattr(runtime_client, "invoke_runtime", unreachable)
    monkeypatch.setattr(radio_broker, "connect", unreachable)
    caplog.set_level(logging.WARNING, logger="backend")
    with client(demo[0]) as browser:
        browser.post("/api/v1/demo/session")
        investigation = browser.post("/api/v1/investigation/turns", json={
            "question": "Show current weather.", "thread_id": "local-thread", "context_envelope": {}})
        radio = browser.post("/api/v1/radio/stream", json={"playback": True})
    assert "Investigation could not complete" in investigation.text
    assert "Radio connection unavailable" in radio.text
    lines = [record.getMessage() for record in caplog.records if record.name.startswith("backend.")]
    assert [line for line in lines if line.startswith("Investigation turn did not complete")
            and line.endswith("reason=ConnectionError")]
    assert [line for line in lines if line.startswith("Radio relay ended early")
            and line.endswith("reason=ConnectionError")]
    assert "fixture-provider-detail" not in caplog.text + investigation.text + radio.text
    trusted_context.clear_secret_cache()
