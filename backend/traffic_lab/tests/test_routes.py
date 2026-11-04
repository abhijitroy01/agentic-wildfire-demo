from backend import simulation_jobs

"""Traffic Lab routes: owner scoping, shared admission, public job views, artifact and provider errors."""
import gzip
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from fastapi.testclient import TestClient
from traffic_simulator.errors import IntegrationError
from traffic_simulator.models import Scenario
from traffic_simulator.store import Store

from backend import api_settings as settings
from backend.demo_access import LOCAL_OWNER, workspace_owner
from backend.evacuation import service
from backend.evacuation.routes import create_router
from backend.tests.builders import prepared, submission
from backend.traffic_lab import routes as research
from backend.traffic_lab.routes import create_research_router

BASE = "/api/v1/research"
# Local mode serves every request as one developer; demo sessions are covered in test_public_access.py.
OWNER = LOCAL_OWNER
AREA = {"lat": 39.7596, "lon": -121.6219, "radius_km": 8}
INTERNAL_FIELDS = {"owner", "subject", "leaseToken", "leaseUntil", "outbox_pending", "expires", "cancel",
                   "input_attempt", "artifact_attempt"}


@pytest.fixture
def app_for(api_app):
    """Return a builder of the Traffic Lab and v1 app for one owner (by default the local developer)."""
    return lambda store, owner=workspace_owner: api_app(store, owner, create_research_router, create_router)


def create(client):
    return client.post(f"{BASE}/jobs", json={**Scenario().model_dump(), "request_id": str(uuid4())})


@pytest.mark.parametrize("path", ["/jobs", "/discover"])
def test_concurrent_replays_and_lost_acknowledgements_create_one_owned_job(tmp_path, app_for, path):
    store = Store(tmp_path)
    client = TestClient(app_for(store))
    body = {**(Scenario().model_dump() if path == "/jobs" else AREA), "request_id": str(uuid4())}
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: client.post(BASE + path, json=body), range(6)))
    assert [reply.status_code for reply in responses] == [202] * 6
    identifiers = {reply.json()["id"] for reply in responses}
    assert len(identifiers) == 1
    assert len(store.list(OWNER)) == 1
    replay = client.post(BASE + path, json=body)
    assert replay.status_code == 202 and replay.json()["id"] in identifiers
    conflict = client.post(BASE + path, json={**body, "lat": AREA["lat"] + .01})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("path", ["/jobs", "/discover"])
@pytest.mark.parametrize("limit", ["active_jobs_per_session", "active_jobs_all_visitors"])
@pytest.mark.parametrize("changed_body", [False, True])
def test_last_slot_concurrent_receipt_rejoins_or_preserves_body_conflict(
        tmp_path, app_for, monkeypatch, path, limit, changed_body):
    store = Store(tmp_path)
    client = TestClient(app_for(store))
    body = {**(Scenario().model_dump() if path == "/jobs" else AREA), "request_id": str(uuid4())}
    winner_body = {**body, "lat": body["lat"] + .01} if changed_body else body
    original_admit, winners = simulation_jobs.admit, []
    monkeypatch.setattr(settings, limit, lambda: 1)

    def admit_after_competing_commit(target, owner, job_id):
        # Both requests have read an absent receipt. The competing builder commits before the
        # original builder checks admission, so the committed job occupies the last slot.
        if not winners:
            winners.append(None)
            winners[0] = client.post(BASE + path, json=winner_body)
            assert winners[0].status_code == 202
        return original_admit(target, owner, job_id)

    monkeypatch.setattr(simulation_jobs, "admit", admit_after_competing_commit)
    response = client.post(BASE + path, json=body)
    assert len(store.list(OWNER)) == 1
    if changed_body:
        assert response.status_code == 409 and response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    else:
        assert response.status_code == 202 and response.json()["id"] == winners[0].json()["id"]


@pytest.mark.parametrize(("error", "status", "retryable"), [
    (EndpointConnectionError(endpoint_url="https://storage.example.invalid"), 503, True),
    (ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject"), 503, False),
    (ClientError({"Error": {"Code": "SlowDown"}}, "GetObject"), 503, True),
    (FileNotFoundError(), 410, False),
])
def test_discovery_artifact_errors_share_retained_reader_mapping(tmp_path, app_for, monkeypatch,
                                                               error, status, retryable):
    store = Store(tmp_path)
    client = TestClient(app_for(store))
    identifier = client.post(f"{BASE}/discover", json={**AREA, "request_id": str(uuid4())}).json()["id"]
    store.update(identifier, status="completed")

    def fail(*_, **__):
        raise error
    monkeypatch.setattr(store, "artifact", fail)
    response = client.get(f"{BASE}/discover/{identifier}")
    assert response.status_code == status and response.json()["error"]["retryable"] is retryable


def test_hazard_inputs_are_saved_in_fractional_meters(tmp_path, app_for):
    store = Store(tmp_path)
    client = TestClient(app_for(store))
    scenario = Scenario(hazard_radius_m=500 * 0.3048, hazard_growth_m_min=10 * 0.3048)
    response = client.post(f"{BASE}/jobs", json={**scenario.model_dump(), "request_id": str(uuid4())})
    assert response.status_code == 202
    saved = store.get(response.json()["id"])["scenario"]
    assert saved["hazard_radius_m"] == pytest.approx(152.4)
    assert saved["hazard_growth_m_min"] == pytest.approx(3.048)


def test_job_ownership_and_conditional_cancel(tmp_path, app_for):
    store = Store(tmp_path)
    user = {"id": "alice"}
    client = TestClient(app_for(store, lambda: user["id"]))
    response = create(client)
    assert response.status_code == 202
    job_id = response.json()["id"]
    user["id"] = "bob"
    for path in (f"{BASE}/jobs/{job_id}", f"{BASE}/jobs/{job_id}/artifacts/result"):
        assert client.get(path).status_code == 404
    assert client.post(f"{BASE}/jobs/{job_id}/cancel").status_code == 404
    assert client.get(f"{BASE}/jobs").json() == []
    user["id"] = "alice"
    cancelled = client.post(f"{BASE}/jobs/{job_id}/cancel")
    assert cancelled.json()["status"] == "cancelling"
    assert not store.claim(job_id, "worker")
    assert client.get(f"{BASE}/jobs/not-a-uuid").status_code == 422


def test_discovery_is_queued_owned_and_reads_immutable_artifacts_without_a_provider(tmp_path, app_for):
    store = Store(tmp_path)
    user = {"id": "alice"}
    browser = TestClient(app_for(store, lambda: user["id"]))
    queued = browser.post(f"{BASE}/discover", json={**AREA, "request_id": str(uuid4())})
    assert queued.status_code == 202
    identifier = queued.json()["id"]
    job = store.get(identifier)
    assert job["kind"] == "road_discovery" and job["scenario"] == AREA
    assert browser.get(f"{BASE}/jobs/{identifier}").status_code == 404
    assert browser.get(f"{BASE}/jobs").json() == []
    user["id"] = "bob"
    assert browser.get(f"{BASE}/discover/{identifier}").status_code == 404
    assert browser.post(f"{BASE}/discover/{identifier}/cancel").status_code == 404
    user["id"] = "alice"
    assert browser.get(f"{BASE}/discover/{identifier}").json()["status"] == "queued"
    assert store.claim(identifier, "fixture-worker")
    result = {"geometry": {"type": "FeatureCollection", "features": []}, "highways": [],
              "source_manifest": {"publisher": "Geofabrik", "snapshot_timestamp": "2026-10-25T21:21:21Z"}}
    store.artifact(identifier, "result", result)
    assert store.finish(identifier, "fixture-worker", [], phase="Road corridors ready")
    saved = browser.get(f"{BASE}/discover/{identifier}").json()
    assert saved["result"] == result
    assert not (set(saved) & INTERNAL_FIELDS)
    store.update(identifier, expires=0)
    assert browser.get(f"{BASE}/discover/{identifier}").status_code == 404


def test_discovery_completion_winning_cancellation_returns_the_saved_result(tmp_path, app_for, monkeypatch):
    store = Store(tmp_path)
    browser = TestClient(app_for(store))
    identifier = browser.post(f"{BASE}/discover", json={**AREA, "request_id": str(uuid4())}).json()["id"]
    assert store.claim(identifier, "fixture-worker")
    result = {"geometry": {"type": "FeatureCollection", "features": []}, "highways": [],
              "source_manifest": {"publisher": "Geofabrik", "snapshot_timestamp": "2026-10-25T21:21:21Z"}}
    original_cancel = store.cancel_run

    def complete_before_cancel(job_id):
        store.artifact(job_id, "result", result)
        assert store.finish(job_id, "fixture-worker", [], phase="Road corridors ready")
        return original_cancel(job_id)

    monkeypatch.setattr(store, "cancel_run", complete_before_cancel)
    cancelled = browser.post(f"{BASE}/discover/{identifier}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "completed"
    assert cancelled.json()["result"] == result
    assert cancelled.json() == browser.get(f"{BASE}/discover/{identifier}").json()
    assert not (set(cancelled.json()) & INTERNAL_FIELDS)


def test_active_run_limit_uses_the_v1_error(tmp_path, app_for):
    client = TestClient(app_for(Store(tmp_path)))
    for _ in range(settings.active_jobs_per_session()):
        assert create(client).status_code == 202
    limited = create(client)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "ACTIVE_RUN_LIMIT"


def test_simulator_busy_across_all_visitors_is_a_retryable_503_with_retry_after(tmp_path, monkeypatch,
                                                                                 app_for):
    monkeypatch.setenv("ACTIVE_JOBS_ALL_VISITORS", "2")
    store = Store(tmp_path)
    user = {"id": "alice"}
    client = TestClient(app_for(store, lambda: user["id"]))
    for name in ("alice", "bob"):
        user["id"] = name
        assert create(client).status_code == 202
    user["id"] = "carol"
    busy = create(client)
    assert busy.status_code == 503 and busy.headers["retry-after"] == str(simulation_jobs.BUSY_RETRY_SECONDS)
    assert busy.json()["error"]["code"] == "SIMULATOR_BUSY" and busy.json()["error"]["retryable"] is True
    assert store.list("carol") == []
    # A finished run frees its place for every visitor.
    store.update(store.list("alice")[0]["id"], status="completed")
    assert create(client).status_code == 202


def quiet_for(store, job_id, seconds, **values):
    """Rewrite a local job record as if no worker had updated it for `seconds`, applying `values` too."""
    job = store.get(job_id)
    job.update(values, updated=time.time() - seconds)
    with store.db() as db:
        db.execute("UPDATE jobs SET body=? WHERE id=?", (json.dumps(job), job_id))


def test_a_job_no_worker_updates_for_an_hour_frees_its_admission_places(tmp_path, monkeypatch, app_for):
    """A dead-lettered or orphaned job stops holding session and all-visitor places after STALE_JOB_SECONDS."""
    monkeypatch.setenv("ACTIVE_JOBS_ALL_VISITORS", "2")
    monkeypatch.setenv("ACTIVE_JOBS_PER_SESSION", "1")
    store = Store(tmp_path)
    user = {"id": "alice"}
    client = TestClient(app_for(store, lambda: user["id"]))
    for name in ("alice", "bob"):
        user["id"] = name
        assert create(client).status_code == 202
    # Alice's worker stopped mid-run and the queue dead-lettered its message: the job stays running, unchanged.
    stuck = store.list("alice")[0]["id"]
    quiet_for(store, stuck, simulation_jobs.STALE_JOB_SECONDS - 60, status="running")
    user["id"] = "carol"
    assert create(client).json()["error"]["code"] == "SIMULATOR_BUSY"
    user["id"] = "alice"
    assert create(client).json()["error"]["code"] == "ACTIVE_RUN_LIMIT"
    quiet_for(store, stuck, simulation_jobs.STALE_JOB_SECONDS + 1)
    assert create(client).status_code == 202
    user["id"] = "carol"
    # Alice's new job and Bob's job now hold both places; the stale job is gone from the shared admission record.
    assert create(client).json()["error"]["code"] == "SIMULATOR_BUSY"
    assert stuck not in store.get(f"admission:{simulation_jobs.ALL_VISITORS}")["active_run_ids"]


def test_study_radius_above_the_setting_is_refused_before_any_provider_call(tmp_path, monkeypatch, app_for):
    monkeypatch.setenv("MAX_STUDY_RADIUS_KM", "5.0")
    monkeypatch.setattr(research.service, "create_research_job", lambda *_args, **_kwargs:
                        pytest.fail("A refused area reached the queue"))
    client = TestClient(app_for(Store(tmp_path)))
    for path, body in (("/discover", AREA), ("/sources", AREA), ("/jobs", Scenario().model_dump())):
        refused = client.post(BASE + path, json={**body, "request_id": str(uuid4())}
                              if path != "/sources" else body)
        assert refused.status_code == 422, path
        assert refused.json()["error"]["code"] == "RADIUS_TOO_LARGE"
        assert "5 km (3.1 mi)" in refused.json()["error"]["message"]


def test_source_lookups_spend_the_shared_tomtom_budget(tmp_path, monkeypatch, app_for):
    store = Store(tmp_path)
    monkeypatch.setattr(research.service, "create_research_job", lambda *_args, **_kwargs:
                        pytest.fail("Optional feeds need no road discovery"))
    seen = []

    async def sources(area, districts, readings=None):
        assert not districts
        seen.append(readings)
        return {"sources": []}
    monkeypatch.setattr(research, "fetch_sources", sources)
    client = TestClient(app_for(store))
    for _ in range(2):
        assert client.post(f"{BASE}/sources", json=AREA).json() == {"sources": []}
    # One budget-and-reuse object per process and store, so repeat lookups of a point reuse its sample.
    assert seen[0].store is store and seen[1] is seen[0]


def test_cancelling_run_still_counts_against_the_limit(tmp_path, app_for):
    client = TestClient(app_for(Store(tmp_path)))
    first, _ = (create(client).json() for _ in range(2))
    assert client.post(f"{BASE}/jobs/{first['id']}/cancel").json()["status"] == "cancelling"
    limited = create(client)
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "ACTIVE_RUN_LIMIT"


def test_parallel_creates_admit_at_most_the_limit(tmp_path, app_for):
    store = Store(tmp_path)
    app = app_for(store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: create(TestClient(app)).status_code, range(16)))
    limit = settings.active_jobs_per_session()
    assert statuses.count(202) == limit
    assert statuses.count(429) == 16 - limit
    assert len(store.list(OWNER)) == limit


def test_research_and_v1_jobs_share_one_budget(tmp_path, monkeypatch, app_for):
    store = Store(tmp_path)
    monkeypatch.setattr("traffic_simulator.traffic.configured", lambda: True)
    _, _, draft = prepared(store, OWNER)
    service.submit(store, OWNER, submission(draft))
    client = TestClient(app_for(store))
    assert create(client).status_code == 202
    assert create(client).json()["error"]["code"] == "ACTIVE_RUN_LIMIT"
    with pytest.raises(IntegrationError) as error:
        prepared(store, OWNER)
    assert error.value.code == "ACTIVE_RUN_LIMIT"


def test_job_views_hide_internals_and_other_job_kinds(tmp_path, monkeypatch, app_for):
    store = Store(tmp_path)
    monkeypatch.setattr("traffic_simulator.traffic.configured", lambda: True)
    _, _, draft = prepared(store, OWNER)
    run = service.submit(store, OWNER, submission(draft))
    client = TestClient(app_for(store))
    created = create(client).json()
    assert store.claim(created["id"], "worker-token")
    store.update(created["id"], input_attempt="worker-token")
    listed = client.get(f"{BASE}/jobs").json()
    assert [job["id"] for job in listed] == [created["id"]]
    current = client.get(f"{BASE}/jobs/{created['id']}").json()
    # Every field the Traffic Lab reads survives; worker, owner and outbox state does not.
    assert {"id", "status", "phase", "progress", "created", "scenario"} <= current.keys()
    assert current["status"] == "running" and current["scenario"]["highway"] == "State 191"
    assert not INTERNAL_FIELDS & (created.keys() | current.keys() | listed[0].keys())
    # The cancel route returns the same public view, even for a run a worker holds a lease on.
    cancelled = client.post(f"{BASE}/jobs/{created['id']}/cancel").json()
    assert cancelled["status"] == "cancelling" and cancelled["phase"] == "Cancellation requested"
    assert {"id", "progress", "created", "scenario"} <= cancelled.keys()
    assert not INTERNAL_FIELDS & cancelled.keys()
    store.update(created["id"], status="failed", error="The simulation could not complete.",
                 timings={"queue_wait": 1.5, "discovery": 0.4, "total": 9.25})
    failed = client.get(f"{BASE}/jobs/{created['id']}").json()
    assert failed["error"] == "The simulation could not complete."
    # The worker's phase timings (phase name -> seconds) are part of the public job view.
    assert failed["timings"] == {"queue_wait": 1.5, "discovery": 0.4, "total": 9.25}
    for other in (run["run_id"], draft["preparation_id"]):
        assert client.get(f"{BASE}/jobs/{other}").status_code == 404
        assert client.post(f"{BASE}/jobs/{other}/cancel").status_code == 404
        assert client.get(f"{BASE}/jobs/{other}/artifacts/result").status_code == 404
    assert store.get(run["run_id"])["status"] == "queued"


def completed_job(store, client):
    job_id = create(client).json()["id"]
    store.update(job_id, status="completed")
    return job_id


def test_v1_run_routes_refuse_the_owners_traffic_lab_jobs_and_network_preparations(tmp_path, caplog, app_for):
    """The reverse of the Traffic Lab check: every v1 run route answers 404 for the same owner's other job kinds."""
    caplog.set_level(logging.INFO, logger="backend.security_events")
    store = Store(tmp_path)
    client = TestClient(app_for(store))
    lab = completed_job(store, client)
    _, _, draft = prepared(store, OWNER)
    for other in (lab, draft["preparation_id"]):
        for response in (client.get(f"/api/v1/runs/{other}"), client.get(f"/api/v1/runs/{other}/artifacts/result"),
                         client.get(f"/api/v1/runs/{other}/capabilities"),
                         client.post("/api/v1/evidence", json={"view": "outcomes", "run_ids": [other]})):
            assert response.status_code == 404 and response.json()["error"]["code"] == "RUN_NOT_FOUND"
    events = [json.loads(record.getMessage()) for record in caplog.records
              if record.name == "backend.security_events"]
    # The server-only reason tells another kind from a missing record; the caller sees the same 404.
    assert [event["reason"] for event in events] == ["other_kind"] * 8


def test_artifacts_use_the_v1_reader_and_errors(tmp_path, monkeypatch, app_for):
    store = Store(tmp_path)
    client = TestClient(app_for(store))
    job_id = completed_job(store, client)
    # A completed v1 run whose result is missing too: both artifact routes answer it with the same error.
    monkeypatch.setattr("traffic_simulator.traffic.configured", lambda: True)
    _, _, draft = prepared(store, OWNER)
    run_id = service.submit(store, OWNER, submission(draft))["run_id"]
    store.update(run_id, status="completed")
    research_path, v1_path = (f"{BASE}/jobs/{job_id}/artifacts", f"/api/v1/runs/{run_id}/artifacts")
    missing = [client.get(f"{path}/result") for path in (research_path, v1_path)]
    assert [r.status_code for r in missing] == [410, 410]
    assert {key: value for key, value in missing[0].json()["error"].items() if key != "request_id"} == {
        key: value for key, value in missing[1].json()["error"].items() if key != "request_id"}
    for name in ("bundle", "unknown"):
        assert client.get(f"{research_path}/{name}").json()["error"]["code"] == "ARTIFACT_NOT_FOUND"
    result = tmp_path / f"runs/{job_id}/result.json.gz"
    result.parent.mkdir(parents=True)
    result.write_bytes(b"not a gzip document")
    invalid = client.get(f"{research_path}/result")
    assert invalid.status_code == 409 and invalid.json()["error"]["code"] == "ARTIFACT_INVALID"
    result.write_bytes(gzip.compress(json.dumps({"policies": []}).encode()))
    assert client.get(f"{research_path}/result").json() == {"policies": []}
    queued = create(client).json()["id"]
    assert client.get(f"{BASE}/jobs/{queued}/artifacts/network").json()["error"]["code"] == "RESULT_NOT_READY"


def test_storage_outage_is_a_retryable_503(tmp_path, monkeypatch, app_for):
    store = Store(tmp_path)
    client = TestClient(app_for(store))
    job_id = completed_job(store, client)

    def unavailable(*_, **__):
        raise ClientError({"Error": {"Code": "SlowDown", "Message": "provider detail"}}, "GetObject")
    monkeypatch.setattr(store, "artifact", unavailable)
    response = client.get(f"{BASE}/jobs/{job_id}/artifacts/result")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ARTIFACT_STORE_UNAVAILABLE"
    assert response.json()["error"]["retryable"] is True


@pytest.mark.parametrize("path,failure,status,code", [
    ("/sources", RuntimeError({"code": 400, "message": "provider-detail"}), 502, "SOURCES_FAILED"),
    ("/sources", ConnectionResetError("provider-detail"), 503, "SOURCES_UNAVAILABLE"),
])
def test_provider_failures_are_retryable_and_never_echo_provider_text(tmp_path, monkeypatch, path, failure,
                                                                       status, code, app_for):
    async def feed_fail(area, districts, readings=None):
        raise failure
    monkeypatch.setattr(research, "fetch_sources", feed_fail)
    response = TestClient(app_for(Store(tmp_path))).post(BASE + path, json={**AREA, "request_id": str(uuid4())}
                                                       if path == "/discover" else AREA)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["retryable"] is True
    assert "provider-detail" not in response.text


def test_feed_failures_log_only_the_exception_type(tmp_path, monkeypatch, caplog, app_for):
    monkeypatch.setattr(research.service, "create_research_job", lambda *_args, **_kwargs:
                        pytest.fail("Optional feeds need no road discovery"))

    async def fail(area, districts, readings=None):
        raise ValueError("GET https://api.tomtom.com/flow?key=fixture-provider-key failed")
    monkeypatch.setattr(research, "fetch_sources", fail)
    with caplog.at_level(logging.WARNING, logger="backend.traffic_lab.routes"):
        response = TestClient(app_for(Store(tmp_path))).post(f"{BASE}/sources", json=AREA)
    assert response.status_code == 502 and response.json()["error"]["code"] == "SOURCES_FAILED"
    assert "ValueError" in caplog.text
    assert "fixture-provider-key" not in caplog.text + response.text


def test_only_our_own_validation_returns_422(tmp_path, monkeypatch, app_for):
    client = TestClient(app_for(Store(tmp_path)))
    monkeypatch.setattr(research.service, "create_research_job", lambda *_args, **_kwargs:
                        pytest.fail("Invalid areas never reach the queue"))
    invalid = client.post(f"{BASE}/discover", json={**AREA, "lat": 91, "request_id": str(uuid4())})
    assert invalid.status_code == 422 and invalid.json()["error"]["code"] == "VALIDATION_ERROR"

    def outside(*_args, **_kwargs):
        raise IntegrationError("AREA_NOT_SUPPORTED", "No suitable modeled roads in this area.")
    monkeypatch.setattr(research.service, "create_research_job", outside)
    assert client.post(f"{BASE}/discover", json={**AREA, "request_id": str(uuid4())}).json()["error"]["code"] == "AREA_NOT_SUPPORTED"


def test_missing_key_blocks_traffic_mode_before_creating_a_job(tmp_path, monkeypatch, app_for):
    monkeypatch.delenv("TOMTOM_API_KEY", raising=False)
    store = Store(tmp_path)
    client = TestClient(app_for(store))
    result = client.post(f"{BASE}/jobs", json={**Scenario(traffic_mode="provider_speed_limits").model_dump(),
                                            "request_id": str(uuid4())})
    assert result.status_code == 422
    assert result.json()["error"]["code"] == "TRAFFIC_KEY_REQUIRED"
    assert store.list(OWNER) == []
    assert client.get(f"{BASE}/config").json()["traffic_key_configured"] is False


def test_saved_examples_load_from_package_data(tmp_path, app_for):
    client = TestClient(app_for(Store(tmp_path)))
    body = client.get(f"{BASE}/examples/paradise").json()
    assert body["area"]["radius_km"] == 8 and body["highways"]
    assert client.get(f"{BASE}/examples/unknown").status_code == 404


def test_research_routes_stay_out_of_the_published_contract():
    from backend.app import app
    assert not [path for path in app.openapi()["paths"] if path.startswith(BASE)]
