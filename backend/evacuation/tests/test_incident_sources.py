"""A scenario point retains its input provenance without guessed coordinates or provider confidence."""
import itertools
import time
from uuid import uuid4

import pytest
from fire_risk_modeler.models import (
    ExposureAssessmentRequest,
    ExposureParameters,
    FireObservation,
    build_unavailable_result,
)
from pydantic import ValidationError
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import commit, record
from traffic_simulator.store import Store

from backend import budgets
from backend.evacuation import locations, service
from backend.evacuation.contracts import IncidentRequest, LocationReceipt, PrepareRequest, SessionRequest
from backend.investigation import fire_observations


def session(store, owner="alice"):
    return service.create_session(store, owner, SessionRequest(request_id=uuid4(), entry_mode="radio"))


def observation(store, *, revision=2, expired=False):
    identifier = uuid4()
    fire = FireObservation(latitude=47.9, longitude=-121.4, name="Source fixture fire",
                           source_id="esri:source-fixture", source_timestamp="2026-10-26T21:00:00Z")
    request = ExposureAssessmentRequest(
        assessment_id=str(identifier), fire_ref="fixture-source", input_hash="0"*64, fire=fire,
        parameters=ExposureParameters(distance_bands_km=[1.0, 3.0, 5.0], ring_points=64,
                                      wind_shape_scale=0.1, max_wind_stretch=2.0))
    result = build_unavailable_result(request, ["Fixture model unavailable."]).model_dump(mode="json")
    value = record("fire_exposure", identifier, "alice", result=result)
    value["revision"] = revision
    if expired:
        value["expires"] = time.time()-1
    commit(store, [(value, None)])
    return identifier, fire


SOURCES = {
    "reported_address": "600 4th Ave, Seattle, WA 98104",
    "reported_place": "Seattle, WA",
    "coordinates": {"lat": 47.6039, "lon": -122.3293},
    "fire_assessment_id": str(uuid4()),
    "fire_observation_id": str(uuid4()),
    "preset_id": "paradise-2018",
}


@pytest.mark.parametrize("names", [(), *itertools.combinations(SOURCES, 2)])
def test_incident_accepts_exactly_one_location_source(names):
    with pytest.raises(ValidationError):
        IncidentRequest(request_id=uuid4(), session_id=uuid4(), **{name: SOURCES[name] for name in names})


@pytest.mark.parametrize("coordinates", [
    {"lat": True, "lon": -122.3}, {"lat": 47.6, "lon": float("nan")},
    {"lat": 91, "lon": 0}, {"lat": 0, "lon": 181}, {"lat": 47.6, "lon": -122.3, "label": " "},
])
def test_coordinate_inputs_are_strict_finite_wgs84(coordinates):
    with pytest.raises(ValidationError):
        IncidentRequest(request_id=uuid4(), session_id=uuid4(), coordinates=coordinates)


def test_explicit_coordinates_need_no_geocoder_or_confidence_and_replay_is_immutable(tmp_path, monkeypatch):
    store = Store(tmp_path)
    context = session(store)
    monkeypatch.setattr(locations, "resolve", lambda *a, **k: pytest.fail("Explicit point reached geocoder"))
    monkeypatch.setattr(budgets, "spend_daily", lambda *a, **k: pytest.fail("Explicit point charged geocoding"))
    request = IncidentRequest(request_id=uuid4(), session_id=context["session_id"],
                              coordinates={"lat": 0.0, "lon": -122.3293, "label": "Supplied point"})
    first = service.create_incident(store, "alice", request)
    point = first["location"]
    assert (point["lat"], point["lon"], point["score"], point["origin"], point["provider"]) == (
        0.0, -122.3293, None, "user_coordinates", "User input")
    assert service.create_incident(store, "alice", request) == first
    other = IncidentRequest.model_validate({**request.model_dump(), "coordinates": {"lat": 47.6, "lon": -122.3}})
    with pytest.raises(IntegrationError) as error:
        service.create_incident(store, "alice", other)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_reported_fire_uses_owned_source_point_even_when_model_unavailable(tmp_path, monkeypatch):
    store = Store(tmp_path)
    context = session(store)
    identifier, fire = observation(store)
    monkeypatch.setattr(locations, "resolve", lambda *a, **k: pytest.fail("Reported fire reached geocoder"))
    monkeypatch.setattr(budgets, "spend_daily", lambda *a, **k: pytest.fail("Reported fire charged geocoding"))
    request = IncidentRequest(request_id=uuid4(), session_id=context["session_id"], fire_assessment_id=identifier)
    incident = service.create_incident(store, "alice", request)
    point = incident["location"]
    assert (point["lat"], point["lon"]) == (fire.latitude, fire.longitude)
    assert (point["source_id"], point["source_timestamp"], point["source_assessment_id"]) == (
        fire.source_id, fire.source_timestamp, str(identifier))
    assert point["origin"] == "reported_fire" and point["score"] is None
    store.update("fire_exposure:"+str(identifier), expires=time.time()-1)
    assert service.create_incident(store, "alice", request) == incident


@pytest.mark.parametrize("owner,revision,expired,expected", [
    ("bob", 2, False, "NOT_FOUND"), ("alice", 1, False, "SOURCE_NOT_READY"),
    ("alice", 2, True, "NOT_FOUND"),
])
def test_reported_fire_refuses_foreign_pending_or_expired_sources(tmp_path, owner, revision, expired, expected):
    store = Store(tmp_path)
    context = session(store, owner)
    identifier, _ = observation(store, revision=revision, expired=expired)
    request = IncidentRequest(request_id=uuid4(), session_id=context["session_id"], fire_assessment_id=identifier)
    with pytest.raises(IntegrationError) as error:
        service.create_incident(store, owner, request)
    assert error.value.code == expected
    assert store.get("session:"+context["session_id"]).get("incident_id") is None


def test_search_place_passes_its_explicit_precision_mode_to_the_geocoder(tmp_path):
    store = Store(tmp_path)
    context = session(store)
    calls = []

    def resolve(address, *, preset, place):
        calls.append((address, preset, place))
        return locations.select_receipt(address, {
            "provider": "Amazon Location", "resolver_version": "amazon-location-v1",
            "candidates": [{"matched_address": "Seattle, WA, USA", "score": 99, "address_type": "Locality",
                            "country": "USA", "region": "WA", "lat": 47.6039, "lon": -122.3293}],
        }, place=place)

    request = IncidentRequest(request_id=uuid4(), session_id=context["session_id"], reported_place=" Seattle, WA ")
    point = service.create_incident(store, "alice", request, resolver=resolve)["location"]
    assert calls == [("Seattle, WA", False, True)]
    assert point["origin"] == "geocoded_place" and "scenario assumption" in point["meaning"]


def test_legacy_location_receipts_remain_readable_without_new_provenance_fields():
    receipt = LocationReceipt(status="resolved", reported_address="Old scenario", matched_address="Old scenario",
                              lat=39.7, lon=-121.6, address_type="Locality", score=100,
                              resolved_at="2026-09-29T00:00:00Z", source_id="legacy:fixture")
    assert receipt.origin == "geocoded_address" and receipt.source_assessment_id is None
    assert receipt.source_observation_id is None


def saved_source_point(store):
    snapshot = {"type": "structuredResponse", "content": {"mapData": {"locations": [
        {"latitude": 46.9, "longitude": -121.7, "fire": {
            "sourceId": "esri:source:owned", "name": "Owned nearby source",
            "lastUpdated": "2026-10-26T21:00:00Z"}},
    ]}}}
    references, writes = fire_observations.capture_writes(store, "alice", str(uuid4()), str(uuid4()), snapshot)
    commit(store, writes)
    return references[0]["observation_id"]


def test_source_observation_handoff_does_not_require_guidance_weather_or_terrain(tmp_path, monkeypatch):
    store = Store(tmp_path)
    context = session(store)
    identifier = saved_source_point(store)
    monkeypatch.setattr(locations, "resolve", lambda *a, **k: pytest.fail("Source point reached geocoder"))
    monkeypatch.setattr(budgets, "spend_daily", lambda *a, **k: pytest.fail("Source point charged geocoding"))
    request = IncidentRequest(request_id=uuid4(), session_id=context["session_id"], fire_observation_id=identifier)
    incident = service.create_incident(store, "alice", request)
    point = incident["location"]
    assert (point["lat"], point["lon"], point["source_id"], point["source_timestamp"]) == (
        46.9, -121.7, "esri:source:owned", "2026-10-26T21:00:00+00:00")
    assert point["source_observation_id"] == identifier and point["origin"] == "reported_fire"

    store.update("fire_observation:"+identifier, expires=time.time()-1)
    assert service.create_incident(store, "alice", request) == incident, "Idempotent acceptance copies source evidence"
    draft = service.prepare(store, "alice", PrepareRequest(
        request_id=uuid4(), session_id=context["session_id"], incident_id=incident["incident_id"],
        location_revision=1))
    assert (draft["scenario"]["lat"], draft["scenario"]["lon"]) == (46.9, -121.7)
    assert store.get("session:"+context["session_id"])["latest_draft_id"] == draft["draft_id"]


@pytest.mark.parametrize("owner,expired", [("bob", False), ("alice", True)])
def test_source_observation_handoff_refuses_foreign_or_expired_references(tmp_path, owner, expired):
    store = Store(tmp_path)
    context = session(store, owner)
    identifier = saved_source_point(store)
    if expired:
        store.update("fire_observation:"+identifier, expires=time.time()-1)
    with pytest.raises(IntegrationError) as error:
        service.create_incident(store, owner, IncidentRequest(
            request_id=uuid4(), session_id=context["session_id"], fire_observation_id=identifier))
    assert (error.value.code, error.value.status) == ("NOT_FOUND", 404)
    assert store.get("session:"+context["session_id"]).get("incident_id") is None
