import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from traffic_simulator import persistence
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import commit, digest, record
from traffic_simulator.store import Store

from backend import api_settings as settings
from backend import simulation_jobs
from backend.evacuation import service
from backend.evacuation.contracts import IncidentRequest, PrepareRequest, SessionRequest, VariantRequest
from backend.tests.builders import HIGHWAYS, located, prepared, resolution, submission
from backend.traffic_lab import service as traffic_lab_service


def test_duplicate_clicks_different_request_ids_submit_draft_once(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr("traffic_simulator.traffic.configured", lambda: True)
    _, _, draft = prepared(store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        runs = list(pool.map(lambda _: service.submit(store, "alice", submission(draft)), range(8)))
    assert len({r["run_id"] for r in runs}) == 1
    assert len([j for j in store.list("alice") if j.get("kind") == "run"]) == 1
    assert store.get(runs[0]["run_id"])["outbox_pending"] is True


def test_new_synthetic_preparations_and_runs_do_not_require_paid_traffic(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr("traffic_simulator.traffic.configured", lambda: False)
    _, _, draft = prepared(store)
    assert draft["scenario"]["traffic_mode"] == "synthetic"
    run = service.submit(store, "alice", submission(draft))
    assert run["scenario"]["traffic_mode"] == "synthetic" and run["status"] == "queued"


def test_repreparation_reloads_latest_draft_without_hiding_it_behind_a_previous_run(tmp_path):
    store = Store(tmp_path)
    session, incident, original = prepared(store)
    run = service.submit(store, "alice", submission(original))
    store.update(run["run_id"], status="completed", progress=1.0, phase="Comparison ready")
    retained = store.get(run["run_id"])
    assert service.session_view(store.get("session:"+session["session_id"]))["selected_run_id"] == run["run_id"]

    latest = service.prepare(store, "alice", PrepareRequest(
        request_id=uuid4(), session_id=session["session_id"], incident_id=incident["incident_id"],
        location_revision=incident["location_revision"]))
    reloaded = service.session_view(store.get("session:"+session["session_id"]))
    assert reloaded["latest_draft_id"] == latest["draft_id"]
    assert reloaded["selected_run_id"] is None
    assert service.draft_view(store, store.get("draft:"+reloaded["latest_draft_id"]))["status"] == "preparing"
    assert simulation_jobs.owned_job(store, run["run_id"], "alice", "run") == retained
    assert service.run_view(retained)["status"] == "completed"
    assert store.get("draft:"+original["draft_id"])["run_id"] == run["run_id"]


def test_explicit_provider_traffic_still_requires_configuration(tmp_path, monkeypatch):
    from backend.tests.builders import complete_preparation
    store = Store(tmp_path)
    monkeypatch.setattr("traffic_simulator.traffic.configured", lambda: False)
    _, _, request = located(store)
    pending = service.prepare(store, "alice", request.model_copy(update={"traffic_mode": "provider_speed_limits"}))
    ready = complete_preparation(store, pending)
    with pytest.raises(IntegrationError) as error:
        service.submit(store, "alice", submission(ready))
    assert error.value.code == "TRAFFIC_NOT_CONFIGURED"
    assert not any(job.get("kind") == "run" for job in store.list("alice"))


def test_completed_preparation_cannot_publish_an_unresolved_scenario(tmp_path):
    store = Store(tmp_path)
    _, _, request = located(store)
    pending = service.prepare(store, "alice", request)
    store.update(pending["preparation_id"], status="completed", prepared_scenario=pending["scenario"],
                 network_validation={})
    with pytest.raises(IntegrationError) as error:
        service.draft_view(store, store.get("draft:"+pending["draft_id"]))
    assert error.value.code == "PREPARATION_INVALID"
    assert not any(job.get("kind") == "run" for job in store.list("alice"))


def test_admission_is_atomic_and_owner_scoped(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr("traffic_simulator.traffic.configured", lambda: True)
    drafts = [prepared(store)[2] for _ in range(5)]

    def submit(draft):
        try:
            return service.submit(store, "alice", submission(draft))
        except IntegrationError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=5) as pool:
        outcomes = list(pool.map(submit, drafts))
    assert sum(isinstance(r, dict) for r in outcomes) == 2
    assert outcomes.count("ACTIVE_RUN_LIMIT") == 3
    _, _, other = prepared(store, "bob")
    assert service.submit(store, "bob", submission(other))["status"] == "queued"


def test_over_budget_preparation_is_refused_before_highway_discovery(tmp_path):
    store = Store(tmp_path)
    _, _, request = located(store)
    for _ in range(settings.active_jobs_per_session()):
        service.prepare(store, "alice", request.model_copy(update={"request_id": uuid4()}),
                        discoverer=lambda area: HIGHWAYS)
    with pytest.raises(IntegrationError) as error:
        service.prepare(store, "alice", request.model_copy(update={"request_id": uuid4()}),
                        discoverer=lambda area: pytest.fail("An over-budget owner reached Caltrans"))
    assert (error.value.code, error.value.status) == ("ACTIVE_RUN_LIMIT", 429)


def test_conflicting_commit_rebuilds_without_calling_road_discovery(tmp_path, monkeypatch):
    store = Store(tmp_path)
    _, _, request = located(store)
    conflicts = []

    def observed(target, writes):
        if not conflicts:
            # Another tab changes the session before this intent commits.
            session = store.get(f"session:{request.session_id}")
            commit(store, [({**session, "revision": session["revision"]+1}, session["revision"])])
        try:
            return commit(target, writes)
        except IntegrationError as exc:
            conflicts.append(exc.code)
            raise
    monkeypatch.setattr(persistence, "commit", observed)

    draft = service.prepare(store, "alice", request, discoverer=lambda _: pytest.fail("API reached road discovery"))
    assert conflicts == ["REVISION_CONFLICT"]
    assert draft["status"] == "preparing"
    assert [job["kind"] for job in store.list("alice")] == ["network_preparation"]


def test_preparation_queues_without_a_discovery_slot_or_provider_call(tmp_path, monkeypatch):
    store = Store(tmp_path)
    _, _, request = located(store)
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(simulation_jobs, "discovery_slots", lambda: slots)
    assert slots.acquire(blocking=False)  # another request's discovery holds the only slot
    pending = service.prepare(store, "alice", request,
                              discoverer=lambda _: pytest.fail("API performed synchronous discovery"))
    assert pending["status"] == "preparing"
    assert pending["scenario"]["highway"] == pending["scenario"]["exit_direction"] == "auto"
    assert service.prepare(store, "alice", request) == pending
    slots.release()
    assert slots.acquire(blocking=False)


def test_discovery_slots_per_api_process_come_from_the_setting(monkeypatch):
    monkeypatch.setenv("DISCOVERIES_PER_API_PROCESS", "2")
    simulation_jobs.discovery_slots.cache_clear()
    try:
        slots = simulation_jobs.discovery_slots()
        assert slots.acquire(blocking=False) and slots.acquire(blocking=False)
        assert not slots.acquire(blocking=False)
    finally:
        simulation_jobs.discovery_slots.cache_clear()


def test_daily_geocoding_budget_is_shared_and_refuses_with_retry_after(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOCODING_REQUESTS_PER_DAY", "1")
    store = Store(tmp_path)
    session = service.create_session(store, "alice", SessionRequest(request_id=uuid4(), entry_mode="lab"))
    request = IncidentRequest(request_id=uuid4(), session_id=session["session_id"], preset_id="paradise-2018")
    first = service.create_incident(store, "alice", request, resolver=resolution)
    # An idempotent replay returns its receipt without a lookup, so it spends nothing.
    assert service.create_incident(store, "alice", request, resolver=resolution) == first
    other = service.create_session(store, "bob", SessionRequest(request_id=uuid4(), entry_mode="lab"))
    with pytest.raises(IntegrationError) as error:
        service.create_incident(store, "bob", IncidentRequest(
            request_id=uuid4(), session_id=other["session_id"], preset_id="paradise-2018"),
            resolver=lambda *a, **k: pytest.fail("Looked up past the daily budget"))
    assert (error.value.code, error.value.status, error.value.retryable) == ("DAILY_LIMIT_REACHED", 429, True)
    assert "Daily demo limit reached" in error.value.message
    assert 0 < error.value.retry_after <= 86400


def test_every_record_and_job_expires_after_the_demo_record_lifetime(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_SESSION_DAYS", "3")
    monkeypatch.setattr("traffic_simulator.traffic.configured", lambda: True)
    store = Store(tmp_path)
    started = time.time()
    _, _, draft = prepared(store)
    run = service.submit(store, "alice", submission(draft))
    research = traffic_lab_service.create_research_job(store, "bob", {"radius_km": 8})
    with store.db() as db:
        records = [json.loads(body) for (body,) in db.execute("SELECT body FROM jobs")]
    kinds = {value["kind"] for value in records}
    assert {"session", "incident", "draft", "request", "admission", "network_preparation", "run",
            "research", "budget"} <= kinds
    for value in records:
        if value["kind"] == "budget":
            # Daily counters expire a day after their UTC date ends, whatever the record lifetime.
            assert 86400 < value["expires"] - started <= 2 * 86400, value["id"]
        else:
            lifetime = value["expires"] - value.get("created", value.get("created_at"))
            assert 3 * 86400 - 2 <= lifetime <= 3 * 86400 + 2, value["id"]
    assert {run["run_id"], research["id"]} <= {value["id"] for value in records if value["kind"] in ("run", "research")}


def test_location_revision_and_ownership_rechecked_on_submit(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr("traffic_simulator.traffic.configured", lambda: True)
    session, _, draft = prepared(store)
    with pytest.raises(IntegrationError, match="not found"):
        service.submit(store, "bob", submission(draft))
    service.create_incident(store, "alice", IncidentRequest(
        request_id=uuid4(), session_id=session["session_id"], preset_id="paradise-2018"), resolver=resolution)
    with pytest.raises(IntegrationError) as error:
        service.submit(store, "alice", submission(draft))
    assert error.value.code == "STALE_LOCATION"


def test_idempotent_resolution_does_not_refetch_and_body_conflicts(tmp_path):
    store = Store(tmp_path)
    session = service.create_session(store, "alice", SessionRequest(request_id=uuid4(), entry_mode="lab"))
    request = IncidentRequest(request_id=uuid4(), session_id=session["session_id"], preset_id="paradise-2018")
    first = service.create_incident(store, "alice", request, resolver=resolution)
    assert service.create_incident(store, "alice", request, resolver=lambda *a, **k: pytest.fail("refetched")) == first
    with pytest.raises(IntegrationError) as error:
        service.create_incident(store, "alice", request.model_copy(update={"preset_id": "palisades-2025"}))
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize("source", [
    {"reported_address": "525 Market St, San Francisco, CA"},
    {"reported_place": "Seattle, Washington"},
    {"coordinates": {"lat": 47.6, "lon": -122.3}},
    {"fire_assessment_id": str(uuid4())},
    {"preset_id": "paradise-2018"},
])
def test_legacy_incident_request_receipts_replay_after_optional_source_contract_extension(tmp_path, source):
    store = Store(tmp_path)
    session = service.create_session(store, "alice", SessionRequest(
        request_id=uuid4(), entry_mode="lab" if "preset_id" in source else "radio"))
    request = IncidentRequest(request_id=uuid4(), session_id=session["session_id"], **source)
    # This is the canonical body and saved response of a pre-release broker request.
    legacy_body = {
        "request_id": str(request.request_id), "session_id": str(request.session_id),
        "reported_address": request.reported_address, "reported_place": request.reported_place,
        "coordinates": request.coordinates.model_dump() if request.coordinates else None,
        "fire_assessment_id": str(request.fire_assessment_id) if request.fire_assessment_id else None,
        "preset_id": request.preset_id,
    }
    response = {"incident_id": str(uuid4()), "session_id": session["session_id"], "location_revision": 1,
                "preset_id": request.preset_id, "location": resolution("Legacy saved source", preset=True)}
    receipt = record("request", digest(["alice", "create_incident", str(request.request_id)]), "alice",
                     body_hash=digest(legacy_body), response=response)
    commit(store, [(receipt, None), (record("incident", response["incident_id"], "alice", **response), None)])

    assert service.create_incident(
        store, "alice", request, resolver=lambda *args, **kwargs: pytest.fail("Repeated legacy provider lookup")) == response
    assert store.get(receipt["id"]) == receipt
    changed = request.model_copy(update={
        "reported_address": None, "reported_place": None, "coordinates": None,
        "fire_assessment_id": None, "preset_id": None, "fire_observation_id": uuid4(),
    })
    if "preset_id" not in source:
        with pytest.raises(IntegrationError) as error:
            service.create_incident(store, "alice", changed)
        assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_variants_extend_parent_and_context_retention_through_child_submission(tmp_path, monkeypatch):
    import hashlib

    import sumolib
    from traffic_simulator.input_bundles import BUNDLE_VERSION, MODEL_VERSION
    from traffic_simulator.persistence import digest

    store = Store(tmp_path)
    session, incident, draft = prepared(store)
    monkeypatch.setattr("traffic_simulator.traffic.configured", lambda: True)
    parent = service.submit(store, "alice", submission(draft))
    original = store.get(parent["run_id"])
    store.update(parent["run_id"], status="completed")
    xml = "<net />"
    content = {"bundle_version": BUNDLE_VERSION, "model_version": MODEL_VERSION,
               "engine_version": sumolib.__version__, "network_xml": xml,
               "metadata": {"net_sha256": hashlib.sha256(xml.encode()).hexdigest()}}
    store.artifact(parent["run_id"], "bundle", {**content, "sha256": digest(content)})
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 86400)
    variant = service.variant(store, "alice", VariantRequest(
        request_id=uuid4(), parent_run_id=parent["run_id"], capability_version="1.0.0",
        changes={"vehicles": 300}))
    assert store.get(parent["run_id"])["expires"] > original["expires"]
    monkeypatch.setattr(time, "time", lambda: now + 2 * 86400)
    child = service.submit(store, "alice", submission(variant))
    until = store.get(child["run_id"])["expires"]
    for identifier in [parent["run_id"], f"session:{session['session_id']}", f"incident:{incident['incident_id']}",
                       f"draft:{variant['draft_id']}"]:
        assert store.get(identifier)["expires"] >= until, identifier
    assert store.artifact(parent["run_id"], "bundle")["sha256"] == digest(content)


def test_metadata_expiring_during_retention_returns_a_reviewable_service_error(tmp_path, monkeypatch):
    store = Store(tmp_path)
    _, _, draft = prepared(store)
    monkeypatch.setattr("traffic_simulator.traffic.configured", lambda: True)

    def expired(*_):
        raise FileNotFoundError("Metadata disappeared during its TTL extension.")

    monkeypatch.setattr(store, "retain", expired)
    with pytest.raises(IntegrationError) as error:
        service.submit(store, "alice", submission(draft))
    assert error.value.code == "FROZEN_INPUTS_UNAVAILABLE"
    assert error.value.status == 409
    assert "Prepare a new scenario" in error.value.message
