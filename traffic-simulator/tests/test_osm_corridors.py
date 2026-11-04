"""Nationwide corridor selection uses one bounded OSM snapshot, including Alaska and Hawaii."""
import hashlib
import json
from xml.etree.ElementTree import Element, SubElement, tostring

import pytest
from pydantic import ValidationError
from traffic_simulator import network, network_preparation, osm_corridors
from traffic_simulator.models import Area, PreparationScenario, Scenario
from traffic_simulator.simulation import demand_for, simulate


def road_xml(latitude=47.6062, longitude=-122.3321):
    """A synthetic connected OSM-format grid; these fixtures are not observations of real streets."""
    root = Element("osm", version="0.6")
    for row in range(5):
        for column in range(5):
            lon = (longitude + (column-2)*.012 + 180) % 360 - 180
            SubElement(root, "node", id=str(row*5+column+1), lat=str(latitude+(row-2)*.009), lon=str(lon))
    for index in range(5):
        for vertical in (True, False):
            identifier = 100+index if vertical else 200+index
            way = SubElement(root, "way", id=str(identifier))
            for position in range(5):
                row, column = (position, index) if vertical else (index, position)
                SubElement(way, "nd", ref=str(row*5+column+1))
            SubElement(way, "tag", k="highway", v="primary")
            SubElement(way, "tag", k="name", v=f"Fixture {'Avenue' if vertical else 'Street'} {index}")
    return tostring(root)


@pytest.mark.parametrize("latitude,longitude", [(47.6062, -122.3321), (61.2181, -149.9003), (21.3069, -157.8583),
                                                (52.0, 179.995)])
def test_us_points_have_bounded_extents_and_stable_corridor_identity(latitude, longitude):
    area = Area(lat=latitude, lon=longitude, radius_km=2)
    raw = road_xml(latitude, longitude)
    result = network.corridor_discovery(raw, area)
    assert result["osm_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["attribution"] == osm_corridors.ATTRIBUTION
    assert result["highways"] and result["highways"][0]["corridor_id"].startswith("osm:name:")
    assert all(candidate["source_way_ids"] and candidate["districts"] == [] for candidate in result["highways"])
    boxes = result["study_bounds"]
    assert len(boxes) == (2 if longitude > 179 else 1)
    assert all(-180 <= west < east <= 180 and -90 <= south < north <= 90 for west, south, east, north in boxes)
    again = network.corridor_discovery(raw, area)
    assert again["highways"] == result["highways"]


def test_preparation_only_auto_values_never_validate_as_a_runnable_scenario():
    pending = PreparationScenario(lat=47.6062, lon=-122.3321)
    assert (pending.highway, pending.exit_direction) == ("auto", "auto")
    with pytest.raises(ValidationError):
        Scenario.model_validate(pending.model_dump())
    for highway in ("State 191", "US 101", "Interstate 5", "osm:ref:"+"a"*20, "osm:way:123"):
        assert Scenario(highway=highway).highway == highway
    for values in ({"lat": float("nan")}, {"lon": float("inf")}, {"lat": 91}, {"lon": -181}):
        with pytest.raises(ValidationError):
            Area.model_validate(values)


@pytest.mark.parametrize("raw", [
    b"<osm><node",
    b"<!DOCTYPE osm [<!ENTITY x 'expanded'>]><osm/>",
    b"<osm><remark>Timed out</remark></osm>",
    b"<osm><way id='1'><nd ref='99'/><nd ref='100'/><tag k='highway' v='primary'/></way></osm>",
    b"<osm><node id='1' lat='nan' lon='0'/></osm>",
])
def test_unusable_osm_geometry_is_refused(raw):
    with pytest.raises(ValueError):
        osm_corridors.geometry_from_xml(raw)


def test_provider_coordinate_text_is_not_exposed_as_a_domain_error():
    with pytest.raises(ValueError) as raised:
        osm_corridors.geometry_from_xml(b"<osm><node id='1' lat='private-provider-detail' lon='0'/></osm>")
    assert str(raised.value) == "OSM road response contains invalid coordinates"


def test_private_and_non_drivable_ways_are_not_corridor_candidates():
    root = Element("osm")
    SubElement(root, "node", id="1", lat="47.6", lon="-122.3")
    SubElement(root, "node", id="2", lat="47.61", lon="-122.3")
    for identifier, tags in enumerate(({"highway": "footway"}, {"highway": "primary", "access": "private"},
                                       {"highway": "primary", "motorcar": "no"}, {"highway": "residential"}), 1):
        way = SubElement(root, "way", id=str(identifier))
        for ref in ("1", "2"):
            SubElement(way, "nd", ref=ref)
        for key, value in tags.items():
            SubElement(way, "tag", k=key, v=value)
    features = osm_corridors.geometry_from_xml(tostring(root))["features"]
    assert [feature["id"] for feature in features] == [4]
    assert features[0]["properties"]["corridor_id"] == "osm:way:4"


@pytest.mark.integration
@pytest.mark.parametrize("latitude,longitude", [(47.6062, -122.3321), (61.2181, -149.9003), (21.3069, -157.8583),
                                                (52.0, 179.995)])
def test_native_sumo_preparation_resolves_auto_from_one_snapshot(monkeypatch, latitude, longitude):
    pending = PreparationScenario(
        lat=latitude, lon=longitude, radius_km=3, vehicles=20, crews=2, duration_minutes=5,
        departure_minutes=1, hazard_radius_m=0, hazard_growth_m_min=0, traffic_mode="synthetic")
    calls = []
    raw = road_xml(latitude, longitude)

    def extract(area, source, directory, run, progress, check):
        check()
        calls.append(source)
        return raw, hashlib.sha256(raw).hexdigest()

    from test_network_providers import source_manifest

    monkeypatch.setattr(network.osm_extracts, "plan", lambda *_: source_manifest())
    monkeypatch.setattr(network.osm_extracts, "extract", extract)
    monkeypatch.setattr(network, "discover", lambda *_: pytest.fail("Worker fetched a second corridor snapshot"))
    prepared = network.prepare_network(pending, lambda *_: None, lambda: None)
    resolved = Scenario.model_validate(prepared[7]["resolved_scenario"])
    assert resolved.highway.startswith("osm:name:") and resolved.exit_direction != "auto"
    assert len(calls) == 1 and prepared[4], "one snapshot and actual passenger exit gates"
    assert all(gate.allows("passenger") for gate in prepared[4])
    selected_ids = set(prepared[7]["selected_route"]["source_way_ids"])
    assert all(selected_ids.intersection(network.source_way_ids(gate)) for gate in prepared[4])
    assert prepared[7]["osm_sha256"] == hashlib.sha256(raw).hexdigest()
    assert all(prepared[7]["study_bounds"])
    civilians, crews = demand_for(resolved, *prepared[1:6])
    assert any(person["route"] for person in civilians)
    assert len(crews) == 2 and any(crew["route"] for crew in crews)
    cached = network.prepare_network(pending, lambda *_: None, lambda: None)
    assert len(calls) == 1 and cached[7]["resolved_scenario"] == prepared[7]["resolved_scenario"]
    assert cached[0].read_bytes() == prepared[0].read_bytes()
    if latitude == 47.6062:
        result = simulate(resolved, prepared, lambda *_: None, lambda: None, parallelism=1)
        assert {policy["policy"] for policy in result["policies"]} == {"baseline", "coordinated", "phased"}
        assert all(policy["summary"]["valid_engine_run"] and policy["summary"]["valid_accounting"]
                   for policy in result["policies"])
        assert result["scenario"]["highway"] == resolved.highway


@pytest.mark.integration
def test_preparation_seals_resolved_scenario_and_restores_without_provider_calls(monkeypatch):
    pending = PreparationScenario(
        lat=47.6062, lon=-122.3321, radius_km=3, vehicles=20, crews=2,
        duration_minutes=5, departure_minutes=1, hazard_radius_m=0, hazard_growth_m_min=0)
    from test_network_providers import source_manifest

    monkeypatch.setattr(network.osm_extracts, "plan", lambda *_: source_manifest())
    monkeypatch.setattr(network.osm_extracts, "extract", lambda *_: (road_xml(), "fixture-sha"))
    bundle = network_preparation.prepare(pending, lambda *_: None, lambda: None)
    scenario = Scenario.model_validate(bundle["scenario"])
    assert scenario.highway != "auto" and scenario.exit_direction != "auto"
    assert bundle["validation"]["corridor_label"].startswith("Fixture")
    monkeypatch.setattr(network.osm_extracts, "plan", lambda *_: pytest.fail("Restored bundle queried a provider"))
    restored = network_preparation.restore(bundle, scenario)
    assert restored[0].read_text() == bundle["network_xml"]
    assert [gate.getID() for gate in restored[4]] == bundle["gate_ids"]
    assert json.loads(json.dumps(bundle))["scenario"] == scenario.model_dump()


@pytest.mark.integration
def test_rural_automatic_selection_skips_gates_unreachable_by_real_seeded_demand(monkeypatch):
    """Ellensburg regression: plausible isolated motorway gates must not make all resident/crew demand unroutable."""
    from test_network_providers import source_manifest

    latitude, longitude = 46.977, -120.517
    root = osm_corridors.parse_xml(road_xml(latitude, longitude))
    for identifier, offset in ((1001, -.02), (1002, .02)):
        SubElement(root, "node", id=str(identifier), lat=str(latitude+.012), lon=str(longitude+offset))
    motorway = SubElement(root, "way", id="999")
    SubElement(motorway, "nd", ref="1001")
    SubElement(motorway, "nd", ref="1002")
    SubElement(motorway, "tag", k="highway", v="motorway")
    SubElement(motorway, "tag", k="name", v="Isolated fixture motorway")
    raw = tostring(root)
    monkeypatch.setattr(network.osm_extracts, "plan", lambda *_: source_manifest())
    monkeypatch.setattr(network.osm_extracts, "extract", lambda *_: (raw, hashlib.sha256(raw).hexdigest()))
    discover = network.corridor_discovery

    def isolated_first(*args):
        result = discover(*args)
        result["highways"].sort(key=lambda road: road["label"] != "Isolated fixture motorway")
        return result

    monkeypatch.setattr(network, "corridor_discovery", isolated_first)
    pending = PreparationScenario(lat=latitude, lon=longitude, radius_km=3, vehicles=20, crews=2,
                                  duration_minutes=5, departure_minutes=1, hazard_radius_m=0,
                                  hazard_growth_m_min=0)
    prepared = network.prepare_network(pending, lambda *_: None, lambda: None)
    assert prepared[7]["corridor_label"] != "Isolated fixture motorway"
    resolved = Scenario.model_validate(prepared[7]["resolved_scenario"])
    civilians, crews = demand_for(resolved, *prepared[1:6])
    assert any(person["route"] for person in civilians) and any(crew["route"] for crew in crews)
