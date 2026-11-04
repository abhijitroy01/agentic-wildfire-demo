"""U.S. location provenance, precision, ambiguity and geocoder failure handling."""
import io
import json
from unittest.mock import MagicMock

import pytest
from traffic_simulator.errors import IntegrationError

from backend.evacuation import locations
from backend.evacuation.locations import select_receipt


def test_amazon_location_requires_a_specific_unambiguous_address():
    data = {"provider": "Amazon Location", "resolver_version": "amazon-location-v1",
            "candidates": [{"score": 100, "matched_address": "Example", "lon": -118.5, "lat": 34.1,
                            "address_type": "Locality", "region": "CA", "country": "USA"}]}
    with pytest.raises(IntegrationError) as error:
        select_receipt("Los Angeles", data)
    assert error.value.code == "LOCATION_UNRESOLVED"
    assert select_receipt("Los Angeles", data, preset=True)["lat"] == 34.1
    data["candidates"].append({**data["candidates"][0], "lon": -119.5, "lat": 35.1})
    with pytest.raises(IntegrationError) as error:
        select_receipt("Los Angeles", data, preset=True)
    assert error.value.code == "LOCATION_AMBIGUOUS"


def test_seattle_address_and_explicit_search_area_have_separate_precision_rules():
    address = "600 4th Ave, Seattle, WA 98104, USA"
    candidate = {"score": 99, "matched_address": address, "lon": -122.3293, "lat": 47.6039,
                 "address_type": "PointAddress", "region": "WA", "country": "USA"}
    data = {"provider": "Amazon Location", "resolver_version": "amazon-location-v1",
            "candidates": [candidate]}
    receipt = select_receipt(address, data)
    assert (receipt["lat"], receipt["lon"], receipt["origin"]) == (
        candidate["lat"], candidate["lon"], "geocoded_address")
    data["candidates"] = [{**candidate, "address_type": "Locality", "matched_address": "Seattle, WA, USA"}]
    with pytest.raises(IntegrationError) as error:
        select_receipt("Seattle", data)
    assert error.value.code == "LOCATION_UNRESOLVED"
    assert "California" not in error.value.message
    area = select_receipt("Seattle", data, place=True)
    assert area["origin"] == "geocoded_place" and "not confirmation" in area["meaning"]


@pytest.mark.parametrize("changes", [
    {"country": "CAN"}, {"score": 89}, {"score": True}, {"lat": True},
    {"lat": float("nan")}, {"lon": float("inf")}, {"lat": 91},
    {"matched_address": " "}, {"address_type": "Unknown"},
])
def test_invalid_or_non_us_matches_never_supply_a_fallback_point(changes):
    candidate = {"score": 99, "matched_address": "Seattle, WA, USA", "lon": -122.3293, "lat": 47.6039,
                 "address_type": "PointAddress", "region": "WA", "country": "USA"}
    data = {"provider": "Amazon Location", "resolver_version": "amazon-location-v1",
            "candidates": [{**candidate, **changes}]}
    with pytest.raises(IntegrationError) as error:
        select_receipt("Unresolved address", data)
    assert error.value.code == "LOCATION_UNRESOLVED"


def test_geocoder_transport_errors_never_become_resolved_locations(monkeypatch):
    monkeypatch.setenv("GEOCODER_FUNCTION_ARN", "arn:fixture")
    client = MagicMock()
    monkeypatch.setattr(locations.boto3, "client", lambda *a, **kw: client)
    for payload, extra in [
        ([], {}), ({"error": {"code": "denied"}}, {}),
        ({"candidates": []}, {"FunctionError": "Unhandled"}),
        ({"provider": "Amazon Location", "resolver_version": "amazon-location-v1", "candidates": None}, {}),
    ]:
        stream = io.BytesIO(json.dumps(payload).encode())
        client.invoke.return_value = {"Payload": stream, **extra}
        with pytest.raises(IntegrationError) as error:
            locations.resolve("Example address")
        assert error.value.code == "GEOCODER_UNAVAILABLE"
        assert stream.closed
