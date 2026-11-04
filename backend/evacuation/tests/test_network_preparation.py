import copy
import hashlib
import threading
from uuid import uuid4

import pytest
from traffic_simulator import network_preparation, worker
from traffic_simulator.errors import IntegrationError
from traffic_simulator.network import NetworkTooLarge
from traffic_simulator.store import Store

from backend.evacuation import service
from backend.evacuation.contracts import IncidentRequest, PrepareRequest, SessionRequest, SubmitRequest
from backend.tests.builders import resolution


def start(store, owner="alice"):
    session = service.create_session(store, owner, SessionRequest(request_id=uuid4(), entry_mode="lab"))
    incident = service.create_incident(store, owner, IncidentRequest(
        request_id=uuid4(), session_id=session["session_id"], preset_id="paradise-2018"), resolver=resolution)
    request = PrepareRequest(request_id=uuid4(), session_id=session["session_id"],
                             incident_id=incident["incident_id"], location_revision=1)
    draft = service.prepare(store, owner, request, discoverer=lambda _: {
        "highways": [{"route_type": "State", "route_number": 191, "approx_distance_km": 0.5}]})
    return request, draft


def fixture_network(grid, scenario=None):
    grid[7].update(net_sha256=hashlib.sha256(grid[0].read_bytes()).hexdigest(),
                   highway_edge_count=4, gates=[{"edge": grid[4][0].getID(),
                                                "coordinate": [-121.62, 39.75]}])
    if scenario is not None:
        from traffic_simulator.models import Scenario
        grid[7]["resolved_scenario"] = Scenario.model_validate({
            **scenario.model_dump(), "highway": "State 191", "exit_direction": "south",
        }).model_dump()
    return grid


@pytest.mark.parametrize("requested,expected", [(None, 4), (8.0, 8)])
def test_custom_address_uses_conservative_default_but_honors_explicit_extent(tmp_path, requested, expected):
    from backend.evacuation.locations import select_receipt
    store = Store(tmp_path)
    session = service.create_session(store, "alice", SessionRequest(request_id=uuid4(), entry_mode="radio"))
    address = "525 Market St, San Francisco, CA"
    incident = service.create_incident(store, "alice", IncidentRequest(
        request_id=uuid4(), session_id=session["session_id"], reported_address=address),
        resolver=lambda address, preset: select_receipt(address, {
            "provider": "Amazon Location", "resolver_version": "amazon-location-v1",
            "candidates": [{"matched_address": address, "score": 100, "address_type": "PointAddress",
                            "region": "CA", "country": "USA", "lat": 37.79053, "lon": -122.39943}],
        }, preset=preset))
    draft = service.prepare(store, "alice", PrepareRequest(
        request_id=uuid4(), session_id=session["session_id"], incident_id=incident["incident_id"],
        location_revision=1, radius_km=requested),
        discoverer=lambda area: {"highways": [{"route_type": "Interstate", "route_number": 80,
                                               "approx_distance_km": 0.5}]})
    assert draft["scenario"]["radius_km"] == expected
    assert draft["status"] == "preparing"
    job = store.get(draft["preparation_id"])
    assert job["extent_mode"] == ("auto" if requested is None else "fixed")
    assert job["requested_radius_km"] == requested
    assert job["max_radius_km"] == 15
    assert store.get("session:"+session["session_id"])["latest_draft_id"] == draft["draft_id"]
    assert not any(job.get("kind") == "run" for job in store.list("alice"))


def test_background_validation_requires_review_and_pins_exact_network(tmp_path, monkeypatch, grid):
    store = Store(tmp_path)
    request, pending = start(store)
    assert pending["status"] == "preparing" and pending["draft_revision"] == 1
    with pytest.raises(IntegrationError) as error:
        service.submit(store, "alice", SubmitRequest(request_id=uuid4(), draft_id=pending["draft_id"],
                                                     draft_revision=1, capability_version="1.0.0"))
    assert error.value.code == "NETWORK_NOT_READY"
    calls = []

    def prepare(scenario, progress, check, **_):
        calls.append(scenario.radius_km)
        if scenario.radius_km > 4:
            raise NetworkTooLarge("Oversized fixture", edge_count=18000)
        return fixture_network(grid, scenario)
    monkeypatch.setattr(network_preparation, "prepare_network", prepare)
    assert worker.execute(store, store.get(pending["preparation_id"]), threading.Event())
    ready = service.prepare(store, "alice", request, discoverer=lambda _: pytest.fail("Repeated discovery"))
    assert ready["status"] == "ready" and ready["draft_revision"] == 2
    assert ready["scenario"]["highway"] == "State 191" and ready["scenario"]["exit_direction"] == "south"
    assert ready["scenario"]["radius_km"] == 4
    assert ready["network_validation"]["requested_radius_km"] == 8
    assert ready["network_validation"]["adjusted"] and calls == [8, 6, 4]
    assert not any(j.get("kind") == "run" for j in store.list("alice"))
    monkeypatch.setattr("traffic_simulator.traffic.configured", lambda: True)
    with pytest.raises(IntegrationError) as error:
        service.submit(store, "alice", SubmitRequest(request_id=uuid4(), draft_id=ready["draft_id"],
                                                     draft_revision=1, capability_version="1.0.0"))
    assert error.value.code == "DRAFT_REVISION_CONFLICT"
    run = service.submit(store, "alice", SubmitRequest(request_id=uuid4(), draft_id=ready["draft_id"],
                                                      draft_revision=2, capability_version="1.0.0"))
    assert run["scenario"]["radius_km"] == 4
    assert store.get(run["run_id"])["prepared_network_id"] == pending["preparation_id"]
    from traffic_simulator.models import Scenario
    bundle = store.artifact(pending["preparation_id"], "bundle")
    restored = network_preparation.restore(bundle, Scenario.model_validate(run["scenario"]))
    assert restored[0].read_bytes() == grid[0].read_bytes()
    bad = copy.deepcopy(bundle)
    bad["network_xml"] += "tampered"
    with pytest.raises(ValueError, match="integrity"):
        network_preparation.restore(bad, Scenario.model_validate(run["scenario"]))


def test_provider_errors_do_not_silently_shrink_extent(tmp_path, monkeypatch):
    store = Store(tmp_path)
    _, draft = start(store)
    attempts = []

    def fail(scenario, *_, **__):
        attempts.append(scenario.radius_km)
        raise RuntimeError("Both public OSM network services are unavailable.")
    monkeypatch.setattr(network_preparation, "prepare_network", fail)
    assert worker.execute(store, store.get(draft["preparation_id"]), threading.Event())
    current = service.draft_view(store, store.get("draft:"+draft["draft_id"]))
    assert current["status"] == "failed" and "OSM" in current["error"]
    assert attempts == [8]


def test_completed_roadless_coverage_is_unavailable_and_cannot_be_submitted(tmp_path):
    store = Store(tmp_path)
    _, draft = start(store)
    coverage = {
        "reason_code": "NO_DRIVABLE_ROADS", "message": "Only footways and paths occur in this study area.",
        "extent_mode": "fixed", "requested_radius_km": 8.0, "searched_radius_km": 8.0,
        "study_bounds": [[-121.8, 39.6, -121.4, 39.9]],
        "attempts": [{"radius_km": 8.0, "eligible_road_count": 0}],
        "source_snapshot": {"publisher": "Geofabrik", "snapshot_timestamp": "2026-10-26T21:00:00Z"},
    }
    store.update(draft["preparation_id"], status="completed", phase="Road coverage analysis complete",
                 preparation_outcome="unavailable", road_coverage=coverage)
    value = service.draft_view(store, store.get("draft:"+draft["draft_id"]))
    assert value["status"] == "unavailable" and value["road_coverage"] == coverage
    assert value["error"] is None and value["error_code"] is None
    with pytest.raises(IntegrationError) as error:
        service.submit(store, "alice", SubmitRequest(
            request_id=uuid4(), draft_id=draft["draft_id"], draft_revision=1, capability_version="1.0.0"))
    assert error.value.code == "NETWORK_NOT_READY"
    assert not any(job.get("kind") == "run" for job in store.list("alice"))


def test_operational_preparation_failure_retains_its_error_code(tmp_path):
    store = Store(tmp_path)
    _, draft = start(store)
    store.update(draft["preparation_id"], status="failed", phase="Source resolution failed",
                 error="The canonical OSM road source is unavailable.", error_code="OSM_SOURCE_UNAVAILABLE")
    value = service.draft_view(store, store.get("draft:"+draft["draft_id"]))
    assert value["status"] == "failed" and value["error_code"] == "OSM_SOURCE_UNAVAILABLE"
    assert value["road_coverage"] is None


def test_preparation_has_atomic_admission_and_no_foreign_draft_access(tmp_path):
    store = Store(tmp_path)
    start(store)
    start(store)
    with pytest.raises(IntegrationError) as error:
        start(store)
    assert error.value.code == "ACTIVE_RUN_LIMIT"
    _, other = start(store, "bob")
    from traffic_simulator.persistence import owned
    with pytest.raises(IntegrationError):
        owned(store, "draft", other["draft_id"], "alice")


def test_cancelled_preparation_never_publishes_a_ready_draft(tmp_path, monkeypatch, grid):
    store = Store(tmp_path)
    _, draft = start(store)

    def cancel(scenario, *_, **__):
        store.cancel_run(draft["preparation_id"])
        return fixture_network(grid, scenario)
    monkeypatch.setattr(network_preparation, "prepare_network", cancel)
    assert worker.execute(store, store.get(draft["preparation_id"]), threading.Event())
    assert service.draft_view(store, store.get("draft:"+draft["draft_id"]))["status"] == "failed"
