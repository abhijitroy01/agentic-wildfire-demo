"""Road-aware planning distinguishes geographic coverage from provider/native failures."""
import copy
import hashlib
import math
import shutil

import pytest
from shapely.geometry import Point
from test_network_providers import source_manifest
from test_osm_corridors import road_xml
from traffic_simulator import network, network_preparation, osm_corridors, road_planning
from traffic_simulator.errors import IntegrationError
from traffic_simulator.models import PreparationScenario, Scenario
from traffic_simulator.network import NetworkTooLarge


def ready(grid, candidate, source):
    resolved = Scenario.model_validate({**candidate.model_dump(), "highway": "osm:way:1", "exit_direction": "south"})
    grid[7].update(net_sha256=hashlib.sha256(grid[0].read_bytes()).hexdigest(), highway_edge_count=2,
                   gates=[], source_manifest=source, resolved_scenario=resolved.model_dump())
    return grid


def pin_plan(monkeypatch, source):
    plans = []

    def plan(area, *_):
        plans.append(area.model_dump())
        return source

    monkeypatch.setattr(network_preparation.osm_extracts, "plan", plan)
    return plans


def test_auto_derives_interior_margin_and_pins_maximum_envelope_before_search(monkeypatch, grid):
    source, observed = source_manifest(), []
    source["sources"][0].update(etag="transport-only", local_path="private-path")
    plans = pin_plan(monkeypatch, source)
    scenario = PreparationScenario(lat=46.94685564720169, lon=-121.71706464002244, radius_km=4)
    interior_radius = math.nextafter(6.696 / .8, math.inf)

    def prepare(candidate, progress, check, *, timings, source_manifest):
        check()
        observed.append((candidate.lat, candidate.lon, candidate.radius_km, source_manifest))
        if candidate.radius_km == 4:
            raise road_planning.RoadCoverageUnavailable(
                "NO_DRIVABLE_ROADS", "Only hiking paths are mapped.", details={"passenger_ways": 0})
        if candidate.radius_km == 8:
            raise road_planning.RoadCoverageUnavailable(
                "NO_ROUTABLE_CORRIDOR", "Corridor outside the interior margin.",
                details={"nearest_corridor_distance_km": 6.696, "interior_corridors": 0})
        assert candidate.radius_km == interior_radius
        return ready(grid, candidate, source)

    monkeypatch.setattr(network_preparation, "prepare_network", prepare)
    bundle = network_preparation.prepare(scenario, lambda *_: None, lambda: None, extent_mode="auto")
    assert len(plans) == 1 and plans[0]["radius_km"] == 15
    assert [(lat, lon) for lat, lon, *_ in observed] == [(scenario.lat, scenario.lon)] * 3
    assert [radius for _, _, radius, _ in observed] == [4, 8, interior_radius]
    assert all(snapshot is source for *_, snapshot in observed)
    validation = bundle["validation"]
    assert validation["extent_mode"] == "auto" and validation["effective_radius_km"] == interior_radius
    assert validation["coverage"]["requested_radius_km"] is None
    assert "transport-only" not in str(validation["coverage"]) and "private-path" not in str(validation["coverage"])
    restored = network_preparation.restore(bundle, Scenario.model_validate(bundle["scenario"]))
    assert restored[0].read_bytes() == grid[0].read_bytes()
    shutil.rmtree(restored[0].parent)


def test_fixed_roadless_extent_is_not_expanded_or_turned_into_a_runnable_bundle(monkeypatch):
    observed = []

    def prepare(candidate, *_, **__):
        observed.append(candidate.radius_km)
        raise road_planning.RoadCoverageUnavailable("NO_DRIVABLE_ROADS", "Only hiking paths are mapped.")

    monkeypatch.setattr(network_preparation, "prepare_network", prepare)
    scenario = PreparationScenario(radius_km=4)
    outcome = network_preparation.prepare(scenario, lambda *_: None, lambda: None)
    assert observed == [4]
    assert outcome["outcome"] == "unavailable" and outcome["scenario"] == scenario.model_dump()
    assert outcome["coverage"]["reason_code"] == "NO_DRIVABLE_ROADS"
    assert outcome["coverage"]["requested_radius_km"] == 4
    assert "network_xml" not in outcome


def test_retained_complete_way_bounds_are_distinct_and_split_at_date_line():
    geometry = {"features": [{"geometry": {"type": "LineString",
                                         "coordinates": [[179.8, 52], [-179.7, 52.1]]}}]}
    assert road_planning.network_bounds(geometry, 179.99) == [
        [179.8, 52, 180, 52.1], [-180, 52, -179.7, 52.1],
    ]


@pytest.mark.parametrize("failure", [
    IntegrationError("OSM_SOURCE_UNAVAILABLE", "Source unavailable.", 503),
    IntegrationError("OSM_SOURCE_CAPACITY", "Source capacity exceeded.", 503),
    RuntimeError("Native conversion failed"),
    ValueError("Malformed graph"),
    TimeoutError("Configured deadline exceeded"),
])
def test_operational_failure_stops_without_extent_retry(monkeypatch, failure):
    source, observed = source_manifest(), []
    pin_plan(monkeypatch, source)

    def prepare(candidate, *_, **__):
        observed.append(candidate.radius_km)
        raise failure

    monkeypatch.setattr(network_preparation, "prepare_network", prepare)
    with pytest.raises(type(failure)) as raised:
        network_preparation.prepare(PreparationScenario(radius_km=4), lambda *_: None, lambda: None,
                                    extent_mode="auto")
    assert raised.value is failure and observed == [4]


def test_roadless_search_stops_at_configured_limit_without_inventing_roads(monkeypatch):
    source, observed = source_manifest(), []
    pin_plan(monkeypatch, source)

    def prepare(candidate, *_, **__):
        observed.append(candidate.radius_km)
        raise road_planning.RoadCoverageUnavailable("NO_DRIVABLE_ROADS", "No passenger roads mapped.")

    monkeypatch.setattr(network_preparation, "prepare_network", prepare)
    outcome = network_preparation.prepare(PreparationScenario(radius_km=4), lambda *_: None, lambda: None,
                                          extent_mode="auto", max_radius_km=10)
    assert observed == [4, 8, 10]
    assert outcome["coverage"]["searched_radius_km"] == 10
    assert outcome["coverage"]["reason_code"] == "NO_DRIVABLE_ROADS"
    assert len(outcome["coverage"]["attempts"]) == 3


def test_dense_network_reductions_keep_pinned_source_and_publish_capacity_outcome(monkeypatch):
    source, observed = source_manifest(), []
    pin_plan(monkeypatch, source)

    def prepare(candidate, *_, **__):
        observed.append(candidate.radius_km)
        raise NetworkTooLarge("Configured edge cap exceeded.", edge_count=15000, source_manifest=source)

    monkeypatch.setattr(network_preparation, "prepare_network", prepare)
    outcome = network_preparation.prepare(PreparationScenario(radius_km=8), lambda *_: None, lambda: None,
                                          extent_mode="auto")
    assert observed == [8, 6, 4, 3, 2]
    assert outcome["coverage"]["reason_code"] == "NETWORK_LIMIT"
    assert all(attempt["edge_count"] == 15000 for attempt in outcome["coverage"]["attempts"])


def test_size_reduction_does_not_shrink_hazard_assumption_or_fail_validation(monkeypatch):
    observed = []
    scenario = PreparationScenario(radius_km=4, hazard_radius_m=1000, hazard_growth_m_min=60)

    def prepare(candidate, *_, **__):
        observed.append(candidate.radius_km)
        raise NetworkTooLarge("Configured edge cap exceeded.", edge_count=15000)

    monkeypatch.setattr(network_preparation, "prepare_network", prepare)
    outcome = network_preparation.prepare(scenario, lambda *_: None, lambda: None)
    assert observed == [4]
    assert outcome["scenario"] == scenario.model_dump()
    assert outcome["coverage"]["reason_code"] == "NETWORK_LIMIT"
    assert [attempt["status"] for attempt in outcome["coverage"]["attempts"]] == [
        "too_large", "ineligible", "ineligible",
    ]


def test_new_source_identity_is_refused_between_candidates(monkeypatch):
    source, changed = source_manifest(), copy.deepcopy(source_manifest())
    changed["sources"][0]["sha256"] = "c" * 64
    pin_plan(monkeypatch, source)

    def prepare(*_, **__):
        raise road_planning.RoadCoverageUnavailable("NO_DRIVABLE_ROADS", "No roads.", source_manifest=changed)

    monkeypatch.setattr(network_preparation, "prepare_network", prepare)
    with pytest.raises(IntegrationError, match="source changed") as raised:
        network_preparation.prepare(PreparationScenario(radius_km=4), lambda *_: None, lambda: None,
                                    extent_mode="auto")
    assert raised.value.code == "OSM_SOURCE_INVALID"


def test_cancellation_before_next_candidate_does_not_publish_outcome(monkeypatch):
    observed = []
    pin_plan(monkeypatch, source_manifest())

    def check():
        if observed:
            raise InterruptedError("cancelled")

    def prepare(candidate, *_, **__):
        observed.append(candidate.radius_km)
        raise road_planning.RoadCoverageUnavailable("NO_DRIVABLE_ROADS", "No roads.")

    monkeypatch.setattr(network_preparation, "prepare_network", prepare)
    with pytest.raises(InterruptedError, match="cancelled"):
        network_preparation.prepare(PreparationScenario(radius_km=4), lambda *_: None, check, extent_mode="auto")
    assert observed == [4]


def test_staging_gap_discloses_routed_crews_without_dropping_unresolved_crews(monkeypatch, grid):
    scenario, source = PreparationScenario(radius_km=4), source_manifest()
    stage = grid[2][0].getID()
    plans = [{"id": "crew-0", "route": [stage], "stage": stage},
             {"id": "crew-1", "route": None}]
    monkeypatch.setattr(network_preparation, "prepare_network",
                        lambda candidate, *_, **__: ready(grid, candidate, source))
    monkeypatch.setattr(network_preparation.network, "demand_for", lambda *_, **__: ([], plans))
    bundle = network_preparation.prepare(scenario, lambda *_: None, lambda: None)
    expected = grid[3][stage].distance(Point(grid[5])) / 1000
    assert bundle["validation"]["crew_staging_distance_km"] == expected
    assert plans[1]["route"] is None


def test_legacy_ready_bundle_still_restores_without_new_validation_fields(monkeypatch, grid):
    source, scenario = source_manifest(), PreparationScenario(radius_km=4)
    monkeypatch.setattr(network_preparation, "prepare_network",
                        lambda candidate, *_, **__: ready(grid, candidate, source))
    bundle = network_preparation.prepare(scenario, lambda *_: None, lambda: None)
    for key in ("extent_mode", "coverage", "crew_staging_distance_km"):
        bundle["validation"].pop(key)
    bundle["sha256"] = network_preparation.digest({key: value for key, value in bundle.items() if key != "sha256"})
    restored = network_preparation.restore(bundle, Scenario.model_validate(bundle["scenario"]))
    assert restored[0].read_bytes() == grid[0].read_bytes()
    shutil.rmtree(restored[0].parent)


@pytest.mark.integration
@pytest.mark.parametrize("latitude,longitude", [
    (47.6062, -122.3321), (61.2181, -149.9003), (21.3069, -157.8583), (52, 179.995),
])
def test_native_preparation_extracts_candidate_bounds_from_pinned_maximum_source(
    monkeypatch, latitude, longitude,
):
    source, observed = source_manifest(), []
    scenario = PreparationScenario(lat=latitude, lon=longitude, radius_km=4, vehicles=20, crews=2,
                                   duration_minutes=5, departure_minutes=1)
    source["study_bounds"] = osm_corridors.study_bounds(latitude, longitude, 15)
    pin_plan(monkeypatch, source)
    raw = road_xml(latitude, longitude)

    def extract(candidate, selected, *_):
        observed.append((candidate.radius_km, selected))
        return raw, hashlib.sha256(raw).hexdigest()

    monkeypatch.setattr(network.osm_extracts, "extract", extract)
    bundle = network_preparation.prepare(scenario, lambda *_: None, lambda: None, extent_mode="auto")
    assert bundle["validation"]["extent_mode"] == "auto" and bundle["scenario"]["radius_km"] == 4
    assert len(observed) == 1
    radius, selected = observed[0]
    assert radius == 4 and selected["study_bounds"] == osm_corridors.study_bounds(latitude, longitude, 4)
    assert selected["study_bounds"] != source["study_bounds"]
    assert selected["sources"] == source["sources"]
    assert source["study_bounds"] == osm_corridors.study_bounds(latitude, longitude, 15)


@pytest.mark.integration
def test_native_routing_error_is_not_misclassified_as_missing_roads(monkeypatch):
    scenario = PreparationScenario(lat=47.6062, lon=-122.3321, radius_km=4, vehicles=20, crews=2,
                                   duration_minutes=5, departure_minutes=1)
    raw = road_xml(scenario.lat, scenario.lon)
    monkeypatch.setattr(network.osm_extracts, "plan", lambda *_: source_manifest())
    monkeypatch.setattr(network.osm_extracts, "extract", lambda *_:
                        (raw, hashlib.sha256(raw).hexdigest()))

    def routing_error(*_, **__):
        raise ValueError("Native graph route error")

    monkeypatch.setattr(network, "demand_for", routing_error)
    with pytest.raises(ValueError, match="Native graph route error"):
        network.prepare_network(scenario, lambda *_: None, lambda: None)
