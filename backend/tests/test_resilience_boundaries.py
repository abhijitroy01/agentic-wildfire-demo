"""Regression checks for retained request identity and safe API failure boundaries."""
import logging
import time
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import digest
from traffic_simulator.store import Store

from backend import firetruck_broker
from backend.http_errors import install_error_handlers
from backend.tests.builders import client


def test_accepted_start_lost_ack_rejoins_one_real_run_and_holds_the_active_slot(
    tmp_path, monkeypatch, api_app
):
    store, private_runs = Store(tmp_path), {}
    app = api_app(store, "owner", firetruck_broker.create_firetruck_router)
    monkeypatch.setenv("FIRETRUCK_ACTIVE_RUNS_PER_SESSION", "1")
    lose_first_ack = True

    async def private(method, path, body=None, owner=None):
        nonlocal lose_first_ack
        run_id = path.rsplit("/", 1)[-1]
        if method == "PUT":
            private_runs[run_id] = {"session_id": run_id, "status": "running"}
            if lose_first_ack:
                lose_first_ack = False
                raise IntegrationError("FIRETRUCK_UNAVAILABLE", "Synthetic lost acknowledgment.", 503)
        return private_runs[run_id]

    monkeypatch.setattr(firetruck_broker, "upstream", private)
    body = {"request_id": str(uuid4()), "fire_location": {"latitude": 34.04, "longitude": -118.52}}
    with client(app) as browser:
        first = browser.post("/api/v1/firetruck/runs", json=body)
        assert first.status_code == 503
        retry = browser.post("/api/v1/firetruck/runs", json=body)
        assert retry.status_code == 201 and retry.json()["status"] == "running"
        status = browser.get("/api/v1/firetruck/requests/" + body["request_id"])
        assert status.json()["session_id"] == retry.json()["session_id"]
        next_run = browser.post("/api/v1/firetruck/runs", json={**body, "request_id": str(uuid4())})
        assert next_run.status_code == 429
    assert len(private_runs) == 1, "A lost acknowledgement must not free a real active run slot."


def test_report_artifact_failure_marks_receipt_failed_and_releases_lease(
    tmp_path, monkeypatch, api_app
):
    store = Store(tmp_path)
    app = api_app(store, "owner", firetruck_broker.create_firetruck_router)
    monkeypatch.setattr(firetruck_broker.settings, "runtime_arn", lambda: "configured")
    monkeypatch.setattr(firetruck_broker.runtime_client, "spend_agent_request", lambda *_: None)

    async def private(method, path, body=None, owner=None):
        return {"session_id": path.rsplit("/", 1)[-1], "status": "running"}

    monkeypatch.setattr(firetruck_broker, "upstream", private)
    with client(app) as browser:
        run = browser.post("/api/v1/firetruck/runs", json={
            "request_id": str(uuid4()), "fire_location": {"latitude": 34.04, "longitude": -118.52}
        }).json()["session_id"]
    launched = []
    monkeypatch.setattr(firetruck_broker, "launch_report", lambda *args: launched.append(args))

    def fail_artifact(*_args):
        raise OSError("Synthetic artifact outage.")

    monkeypatch.setattr(firetruck_broker, "report_artifact", fail_artifact)
    body = {"request_id": str(uuid4())}
    with TestClient(app, raise_server_exceptions=False) as browser:
        response = browser.post(f"/api/v1/firetruck/runs/{run}/reports", json=body)
        assert response.status_code == 503
        retry = browser.post(f"/api/v1/firetruck/runs/{run}/reports", json=body)
        assert retry.status_code == 202 and retry.json()["status"] == "failed"
        receipt = store.get("firetruck_report:" + retry.json()["report_id"])
    assert launched == []
    assert receipt["status"] == "failed"
    lease = store.get("firetruck_report_lease:" + digest("owner"))
    assert lease["lease_until"] <= time.time()


def test_service_error_has_correlated_safe_envelope_and_one_operational_event(caplog):
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/api/v1/review-fixture")
    def failed():
        raise IntegrationError("FIRETRUCK_UNAVAILABLE", "Synthetic upstream timeout.", 503, retryable=True)

    caplog.set_level(logging.WARNING)
    with TestClient(app) as browser:
        response = browser.get("/api/v1/review-fixture")
    assert response.status_code == 503
    request_id = response.json()["error"]["request_id"]
    assert request_id and response.headers["x-request-id"] == request_id
    events = [entry for entry in caplog.records if entry.name == "backend.http_errors"]
    assert len(events) == 1 and request_id in events[0].getMessage()




def test_unexpected_api_failure_is_safe_correlated_and_logged_once(caplog):
    app = FastAPI()
    install_error_handlers(app)
    sensitive = "private-token-and-signed-provider-url"

    @app.get("/api/v1/unexpected")
    def failed():
        raise OSError(sensitive)

    with TestClient(app) as browser:
        response = browser.get("/api/v1/unexpected")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
    events = [entry for entry in caplog.records if entry.name == "backend.http_errors"]
    assert len(events) == 1
    assert sensitive not in caplog.text and sensitive not in response.text
    assert "OSError" in caplog.text and '"frames"' in caplog.text
