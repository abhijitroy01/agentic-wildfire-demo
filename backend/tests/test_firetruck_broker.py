import base64
import copy
import hashlib
import html
import json
import logging
import re
import sqlite3
import time
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
import pytest
from botocore.exceptions import ClientError
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import digest

from backend import firetruck_broker, runtime_client, trusted_context
from backend.demo_access import workspace_owner
from backend.tests.builders import client, owner_of


@pytest.fixture
def trucks(demo, monkeypatch):
    app, store = demo
    app.include_router(firetruck_broker.create_firetruck_router(workspace_owner))
    snapshots = {}
    calls = []

    async def upstream(method, path, body=None, owner=None):
        # Every run route names its owner, so the service can refuse other visitors' runs.
        assert re.fullmatch(r"[0-9a-f]{64}", owner or "")
        calls.append((method, path, body, owner))
        run_id = path.split("/")[-1]
        if method == "PUT":
            snapshots[run_id] = {
                "session_id": run_id, "status": "running", "incident_elapsed_seconds": 300,
                "phase": "INITIAL_ATTACK", "phase_history": [
                    {"phase": "INITIAL_DISPATCH", "elapsed_seconds": 0},
                    {"phase": "INITIAL_ATTACK", "elapsed_seconds": 180}],
                "summary": {"total_water_deployed_gal": 123, "name": "<script>alert(1)</script>"},
                "events": [{"message": "<img src=x onerror=alert(1)>"}], "final_stats": None,
            }
            return {"session_id": run_id, "status": "running"}
        if method == "DELETE":
            snapshots[run_id]["status"] = "stopped"
            snapshots[run_id]["final_stats"] = {"total_water_deployed_gal": 123}
        return copy.deepcopy(snapshots[run_id])

    monkeypatch.setattr(firetruck_broker, "upstream", upstream)
    return app, store, calls, snapshots


def start(browser, **overrides):
    body = {"request_id": str(uuid4()), "fire_location": {
        "latitude": 34.04, "longitude": -118.52, "name": "Synthetic exercise"}}
    return browser.post("/api/v1/firetruck/runs", json={**body, **overrides})


def test_owner_isolation_and_idempotent_start(trucks):
    app, _, calls, _ = trucks
    with client(app) as alice, client(app) as bob:
        alice.post("/api/v1/demo/session")
        bob.post("/api/v1/demo/session")
        request_id = str(uuid4())
        created = start(alice, request_id=request_id)
        assert created.status_code == 201
        run_id = created.json()["session_id"]
        assert start(alice, request_id=request_id).json()["session_id"] == run_id
        assert sum(method == "PUT" for method, *_ in calls) == 1
        path = f"/api/v1/firetruck/runs/{run_id}"
        assert alice.get(path).json()["summary"]["total_water_deployed_gal"] == 123
        count = len(calls)
        assert bob.get(path).status_code == 404
        assert bob.delete(path).status_code == 404
        assert bob.post(path+"/reports", json={"request_id": str(uuid4())}).status_code == 404
        assert bob.get("/api/v1/firetruck/runs").json() == []
        assert len(calls) == count
        assert len({owner for *_, owner in calls}) == 1
        assert start(alice, request_id=request_id, initial_truck_count=6).status_code == 409
        assert start(alice, url="http://169.254.169.254").status_code == 422
        assert start(alice, initial_truck_count=12, max_truck_count=4).status_code == 422
        assert start(alice).status_code == 201
        assert start(alice).status_code == 429


def test_a_start_the_service_refuses_is_failed_and_frees_its_active_slot(trucks, monkeypatch):
    app, _, _, _ = trucks
    working = firetruck_broker.upstream

    async def refuse(method, path, body=None, owner=None):
        raise IntegrationError("FIRETRUCK_UNAVAILABLE", "Firetruck service declined the start.", 429)

    with client(app) as alice:
        alice.post("/api/v1/demo/session")
        monkeypatch.setattr(firetruck_broker, "upstream", refuse)
        assert start(alice).status_code == 429
        assert [run["status"] for run in alice.get("/api/v1/firetruck/runs").json()] == ["failed"]
        monkeypatch.setattr(firetruck_broker, "upstream", working)
        assert start(alice).status_code == 201
        assert start(alice).status_code == 201
        assert start(alice).status_code == 429


def test_a_racing_first_start_conflicts_instead_of_replacing_the_run_index(trucks, monkeypatch):
    """Two first starts in one session: the one that read no run index conflicts with the one that committed."""
    app, store, _, _ = trucks
    with client(app) as alice:
        alice.post("/api/v1/demo/session")
        first = start(alice).json()["session_id"]
        index_key = f"firetruck_owner:{digest(owner_of(alice))}"
        read, stale = store.get, []

        def before_the_other_commit(key):
            """Answer this request's first run-index read as it was before the other start committed."""
            if key == index_key and not stale:
                stale.append(key)
                return None
            return read(key)
        monkeypatch.setattr(store, "get", before_the_other_commit)
        racing = start(alice)
        assert racing.status_code == 409 and racing.json()["error"]["code"] == "REVISION_CONFLICT"
        monkeypatch.setattr(store, "get", read)
        assert [run["session_id"] for run in alice.get("/api/v1/firetruck/runs").json()] == [first]


def test_stream_enforces_owner_and_releases_connection_lease(trucks, caplog):
    from fastapi import WebSocketDisconnect

    app, _, _, _ = trucks
    caplog.set_level(logging.INFO, logger="backend.security_events")
    with client(app) as alice, client(app) as bob:
        alice.post("/api/v1/demo/session")
        bob.post("/api/v1/demo/session")
        run_id = start(alice).json()["session_id"]
        path = f"wss://workspace.cloudfront.net/api/v1/firetruck/runs/{run_id}/stream"
        with pytest.raises(WebSocketDisconnect), bob.websocket_connect(path):
            pass
        # WebSocket refusals bypass the HTTP error handler, so the broker logs them as security events.
        events = [json.loads(line.getMessage()) for line in caplog.records
                  if line.name == "backend.security_events"]
        assert [event for event in events if event["outcome"] == "denied"] == [{
            "security_event": "request_refused", "outcome": "denied", "code": "NOT_FOUND", "status": 404,
            "reason": "other_owner", "method": "WEBSOCKET", "route": "/api/v1/firetruck/runs/{run_id}/stream",
            "owner_digest": digest(owner_of(bob))}]
        with alice.websocket_connect(path) as socket:
            assert socket.receive_json()["event_type"] == "CONNECTED"
            assert socket.receive_json()["event_type"] == "TRUCK_UPDATE"
            socket.send_text("not a supported command")
            with pytest.raises(WebSocketDisconnect):
                socket.receive_json()
        with alice.websocket_connect(path) as socket:
            assert socket.receive_json()["event_type"] == "CONNECTED"


def test_session_run_limits_and_the_run_lifetime_come_from_deployment_settings(trucks, monkeypatch):
    """capacity.firetruck_active_runs_per_session, runs_per_session and run_lifetime_minutes bound one session."""
    app, _, _, _ = trucks
    monkeypatch.setenv("FIRETRUCK_ACTIVE_RUNS_PER_SESSION", "1")
    monkeypatch.setenv("FIRETRUCK_RUNS_PER_SESSION", "3")
    monkeypatch.setenv("FIRETRUCK_RUN_LIFETIME_MINUTES", "10")
    with client(app) as alice:
        alice.post("/api/v1/demo/session")
        assert start(alice).status_code == 201
        busy = start(alice)
        assert busy.status_code == 429 and busy.json()["error"]["message"].startswith("Stop an active firetruck run")
        # Past the 10-minute run lifetime the service has stopped a run, whatever status the broker last saved, so
        # it no longer holds the session's one active slot.
        clock = time.time
        monkeypatch.setattr(firetruck_broker, "time", SimpleNamespace(time=lambda: clock() + 11 * 60))
        assert start(alice).status_code == 201
        assert start(alice).status_code == 201
        spent = start(alice)
        assert spent.status_code == 429 and "reached its run limit" in spent.json()["error"]["message"]


def test_open_streams_and_their_lifetime_come_from_deployment_settings(trucks, monkeypatch):
    """capacity.firetruck_streams_per_session and stream_lifetime_minutes bound one session's live streams."""
    from fastapi import WebSocketDisconnect

    app, store, _, _ = trucks
    monkeypatch.setenv("FIRETRUCK_STREAMS_PER_SESSION", "1")
    monkeypatch.setenv("FIRETRUCK_STREAM_LIFETIME_MINUTES", "1")
    with client(app) as alice:
        alice.post("/api/v1/demo/session")
        path = f"wss://workspace.cloudfront.net/api/v1/firetruck/runs/{start(alice).json()['session_id']}/stream"
        with alice.websocket_connect(path) as socket:
            assert socket.receive_json()["event_type"] == "CONNECTED"
            # The stream's lease lasts its one-minute lifetime plus the 30-second disconnect grace.
            lease = store.get(f"firetruck_streams:{digest(owner_of(alice))}")
            assert [85 <= expires - time.time() <= 90 for expires in lease["connections"].values()] == [True]
            with pytest.raises(WebSocketDisconnect), alice.websocket_connect(path):
                pass
        with alice.websocket_connect(path) as socket:
            assert socket.receive_json()["event_type"] == "CONNECTED"


def test_report_uses_owned_snapshot_survives_stop_and_escapes_model_html(trucks, monkeypatch):
    """Reports use owned evidence, owner-scoped keys and a firetruck_report context, and escape model text."""
    app, store, _, snapshots = trucks
    monkeypatch.setenv("SIMULATION_CONTEXT_SECRET", "S"*64)
    trusted_context.clear_secret_cache()
    captured = []

    def invoke(payload, *, timeout_seconds):
        assert 0 < timeout_seconds <= 190
        captured.append(payload)
        yield {"type": "STATE_SNAPSHOT", "snapshot": {"content": {
            "response": "Recorded water: 123 gallons. <script>alert(2)</script>"}}}
    monkeypatch.setattr(runtime_client, "invoke_runtime", invoke)

    monkeypatch.setattr(firetruck_broker, "launch_report", firetruck_broker.generate_report)
    with client(app) as alice, client(app) as bob:
        alice.post("/api/v1/demo/session")
        bob.post("/api/v1/demo/session")
        run_id = start(alice).json()["session_id"]
        path = f"/api/v1/firetruck/runs/{run_id}"
        alice.delete(path)
        assert alice.get(path).json()["status"] == "stopped"
        # Evidence may count earlier events it does not carry; the report keeps that accounting.
        snapshots[run_id]["omitted_events"] = 7
        request_id = str(uuid4())
        result = alice.post(path+"/reports", json={"request_id": request_id})
        report_id = result.json()["report_id"]
        assert result.status_code == 202
        # The UI links the report page itself, so the status carries no URL.
        assert alice.get(f"/api/v1/firetruck/reports/{report_id}").json() == {
            "report_id": report_id, "status": "completed"}
        assert alice.post(path+"/reports", json={"request_id": request_id}).json()["report_id"] == report_id
        # The artifact key is owner-scoped: the report ID hashes the owner, run and request, never a clock.
        assert report_id == str(uuid5(NAMESPACE_URL, digest([owner_of(alice), run_id, request_id, "report"])))
        assert (store.root / "reports" / report_id / "report.json.gz").is_file()
        assert len(captured) == 1
        forwarded = captured[0]["forwardedProps"]
        # The token is signed for the firetruck_report purpose and bound to this report and its exact context.
        claims = trusted_context.verify(forwarded["firetruck_report_token"], "firetruck_report")
        assert claims["context_hash"] == digest(forwarded["firetruck_report_context"])
        assert claims["session_id"] == report_id == captured[0]["threadId"]
        for other in ("simulation", "investigation"):
            with pytest.raises(trusted_context.jwt.InvalidSignatureError):
                trusted_context.verify(forwarded["firetruck_report_token"], other)
        sent = forwarded["firetruck_report_context"]["evidence"]
        assert sent["final_stats"]["total_water_deployed_gal"] == 123
        assert sent["omitted_events"] == 7
        snapshots[run_id]["summary"]["total_water_deployed_gal"] = 999
        report = alice.get(f"/api/v1/firetruck/reports/{report_id}/html")
        assert report.status_code == 200
        assert "<script>" not in report.text and "<img" not in report.text
        assert "&lt;script&gt;" in report.text and "Recorded water: 123 gallons." in report.text
        # Random run IDs/evidence hashes can contain "999". Inspect the actual
        # recorded field rather than rejecting those digits anywhere in HTML.
        summary = report.text.split("<h2>Recorded resource summary</h2><pre>", 1)[1].split("</pre>", 1)[0]
        assert json.loads(html.unescape(summary))["total_water_deployed_gal"] == 123
        assert "7 earlier events are counted" in report.text
        policy = report.headers["content-security-policy"]
        assert "sandbox" in policy and "unsafe-inline" not in policy
        # The policy allows exactly the page's one inline style, by its SHA-256 hash.
        style = re.search(r"<style>(.*?)</style>", report.text, re.DOTALL).group(1)
        style_hash = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
        assert f"style-src 'sha256-{style_hash}';" in policy
        assert "<h2>Recorded stages</h2>" in report.text
        assert bob.get(f"/api/v1/firetruck/reports/{report_id}/html").status_code == 404
        assert bob.get(f"/api/v1/firetruck/reports/{report_id}").status_code == 404
    trusted_context.clear_secret_cache()


def test_a_failed_report_is_failed_and_logs_only_the_exception_type(trucks, monkeypatch, caplog):
    """A report whose Runtime call fails is marked failed; the server log keeps the type, never the text."""
    app, _, _, _ = trucks
    monkeypatch.setenv("SIMULATION_CONTEXT_SECRET", "S"*64)
    trusted_context.clear_secret_cache()
    detail = "fixture-provider-" + "detail"

    def unreachable(payload, **_kwargs):
        raise ConnectionError(detail)

    monkeypatch.setattr(runtime_client, "invoke_runtime", unreachable)
    monkeypatch.setattr(firetruck_broker, "launch_report", firetruck_broker.generate_report)
    caplog.set_level(logging.WARNING, logger="backend.firetruck_broker")
    with client(app) as alice:
        alice.post("/api/v1/demo/session")
        run_id = start(alice).json()["session_id"]
        report_id = alice.post(f"/api/v1/firetruck/runs/{run_id}/reports",
                               json={"request_id": str(uuid4())}).json()["report_id"]
        assert alice.get(f"/api/v1/firetruck/reports/{report_id}").json()["status"] == "failed"
    assert f"Firetruck report {report_id} did not complete: reason=ConnectionError" in caplog.text
    assert detail not in caplog.text
    trusted_context.clear_secret_cache()


def test_a_malformed_runtime_arn_leaves_reports_not_configured(trucks, monkeypatch):
    """Reports use the same Runtime binding rule as investigations: a name or partial ARN is 503, never a call."""
    app, _, _, _ = trucks
    monkeypatch.setenv("SIMULATION_RUNTIME_ARN", "configured-server-runtime")
    monkeypatch.setattr(runtime_client, "invoke_runtime", lambda *_, **__: pytest.fail("Runtime called"))
    with client(app) as alice:
        alice.post("/api/v1/demo/session")
        run = start(alice).json()
        # The run view carries no stream URL: the UI builds it from its own origin.
        assert set(run) == {"session_id", "fire_location", "status", "created_at"}
        refused = alice.post(f"/api/v1/firetruck/runs/{run['session_id']}/reports",
                             json={"request_id": str(uuid4())})
        assert refused.status_code == 503 and refused.json()["error"]["code"] == "AGENT_NOT_CONFIGURED"


def test_report_cannot_accept_forged_telemetry(trucks):
    with client(trucks[0]) as browser:
        browser.post("/api/v1/demo/session")
        run_id = start(browser).json()["session_id"]
        response = browser.post(f"/api/v1/firetruck/runs/{run_id}/reports", json={
            "request_id": str(uuid4()), "evidence": {"total_water_deployed_gal": 999}})
        assert response.status_code == 422


def test_render_keeps_missing_measurements_unavailable():
    rendered = firetruck_broker.render_report({"run_id": str(uuid4()), "evidence_hash": "hash",
        "evidence": {"status": "interrupted", "incident_elapsed_seconds": 2, "final_stats": None}})
    assert "interrupted" in rendered
    assert "null" in rendered and "unavailable, not zero" in rendered
    assert "earlier events" not in rendered
    assert json.loads("null") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["status", "invalid_json", "timeout"])
async def test_adapter_diagnostics_identify_operation_without_provider_text(monkeypatch, caplog, failure):
    canary = "private-provider-body https://signed.invalid/?credential=private-secret"
    monkeypatch.setenv("FIRETRUCK_SERVICE_URL", "https://private-service.invalid")
    monkeypatch.setenv("FIRETRUCK_SERVICE_KEY", "private-key-" * 4)
    original_client = httpx.AsyncClient

    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(canary, request=request)
        return httpx.Response(503 if failure == "status" else 200, content=canary)

    monkeypatch.setattr(firetruck_broker.httpx, "AsyncClient",
                        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))
    with pytest.raises(IntegrationError) as raised:
        await firetruck_broker.upstream("GET", "/internal/runs/private-run-path", owner="private-owner")
    assert raised.value.code == "FIRETRUCK_UNAVAILABLE"
    own_records = [record for record in caplog.records if record.name == "backend.firetruck_broker"]
    diagnostics = [json.loads(record.getMessage().split(" ", 1)[1]) for record in own_records]
    assert diagnostics == [{
        "operation": "read_run", "method": "GET", "status": 503,
        "provider_status": {"status": 503, "invalid_json": 200, "timeout": None}[failure],
        "cause_type": {"status": "IntegrationError", "invalid_json": "JSONDecodeError",
                       "timeout": "ReadTimeout"}[failure],
        "code": "FIRETRUCK_UNAVAILABLE" if failure == "status" else None,
    }]
    diagnostic_text = "\n".join(record.getMessage() for record in own_records)
    for secret in (canary, "private-service", "private-run-path", "private-owner", "private-key"):
        assert secret not in diagnostic_text
    assert all(record.exc_info is None for record in own_records)


def test_stream_logs_service_failure_and_releases_its_connection(trucks, monkeypatch, caplog):
    from fastapi import WebSocketDisconnect

    app, store, _, _ = trucks
    with client(app) as alice:
        alice.post("/api/v1/demo/session")
        run_id = start(alice).json()["session_id"]

        async def unavailable(*_args):
            raise IntegrationError("FIRETRUCK_UNAVAILABLE", "private signed URL and provider response", 503)

        monkeypatch.setattr(firetruck_broker, "read_run", unavailable)
        path = f"wss://workspace.cloudfront.net/api/v1/firetruck/runs/{run_id}/stream"
        with alice.websocket_connect(path) as socket:
            assert socket.receive_json()["event_type"] == "CONNECTED"
            with pytest.raises(WebSocketDisconnect):
                socket.receive_json()
        assert store.get(f"firetruck_streams:{digest(owner_of(alice))}")["connections"] == {}
    diagnostics = [json.loads(record.getMessage().split(" ", 1)[1]) for record in caplog.records
                   if record.name == "backend.firetruck_broker"]
    assert diagnostics == [{
        "operation": "stream_read", "method": "WEBSOCKET", "status": 503, "run_id": run_id,
        "cause_type": "IntegrationError", "code": "FIRETRUCK_UNAVAILABLE",
    }]
    assert "private signed" not in caplog.text


@pytest.mark.parametrize("failure", ["aws", "sqlite", "os", "lost_ack"])
def test_report_receipt_publication_failure_releases_lease_without_launch(
        trucks, monkeypatch, caplog, failure):
    app, store, _, _ = trucks
    original_commit = firetruck_broker.commit
    canary = "private storage response https://signed.invalid/?credential=private-secret"
    launch = []
    monkeypatch.setattr(firetruck_broker, "launch_report", lambda *_args: launch.append(_args))
    failed = []

    def fail_publication(target, writes):
        if not failed and writes[0][0]["kind"] == "firetruck_report":
            failed.append(True)
            if failure == "lost_ack":
                original_commit(target, writes)
            if failure == "aws":
                raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException",
                                            "Message": canary}}, "TransactWriteItems")
            if failure == "sqlite":
                raise sqlite3.OperationalError(canary)
            raise OSError(canary)
        return original_commit(target, writes)

    with client(app) as alice:
        alice.post("/api/v1/demo/session")
        owner = owner_of(alice)
        run_id = start(alice).json()["session_id"]
        request_id = str(uuid4())
        report_id = firetruck_broker.requested_report_id(owner, run_id, request_id)
        monkeypatch.setattr(firetruck_broker, "commit", fail_publication)
        response = alice.post(f"/api/v1/firetruck/runs/{run_id}/reports", json={"request_id": request_id})
        assert response.status_code == 503 and response.json()["error"]["code"] == "REPORT_SETUP_FAILED"
        assert store.get(f"firetruck_report_lease:{digest(owner)}")["lease_until"] == 0
        saved = store.get(f"firetruck_report:{report_id}")
        assert saved["status"] == "failed" if failure == "lost_ack" else saved is None
        assert not launch and not (store.root / "reports" / report_id / "report.json.gz").exists()
        # A different explicit report request can start immediately after the failed setup.
        following = alice.post(f"/api/v1/firetruck/runs/{run_id}/reports", json={"request_id": str(uuid4())})
        assert following.status_code == 202 and len(launch) == 1
    diagnostics = [json.loads(record.getMessage().split(" ", 1)[1]) for record in caplog.records
                   if record.name == "backend.firetruck_broker"]
    assert diagnostics == [{
        "operation": "report_setup", "report_id": report_id,
        "cause_type": {"aws": "ClientError", "sqlite": "OperationalError", "os": "OSError",
                       "lost_ack": "OSError"}[failure],
        "code": "ProvisionedThroughputExceededException" if failure == "aws" else None,
    }]
    assert canary not in caplog.text
