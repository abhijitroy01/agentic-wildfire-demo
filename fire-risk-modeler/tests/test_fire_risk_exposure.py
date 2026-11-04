"""Fire exposure wind conventions, source identity and conservative missing-weather behavior."""
from uuid import uuid4

import pytest
from fire_risk_modeler.exposure import assess_exposure
from fire_risk_modeler.geometry import projection
from fire_risk_modeler.models import FireObservation, model_input_hash
from shapely.geometry import shape
from shapely.ops import transform


def test_meteorological_from_wind_extends_downwind_and_reverses(make_request):
    request = make_request()
    forward, _ = projection(request.fire)
    north_wind = assess_exposure(request)
    geometry = shape(north_wind.geojson.features[-1].geometry.model_dump(mode="json"))
    metric = transform(forward.transform, geometry)
    assert abs(metric.bounds[1]) > abs(metric.bounds[3])
    south_weather = request.weather.model_copy(update={"wind_from_degrees": 180.0})
    reversed_result = assess_exposure(request.model_copy(update={"weather": south_weather}))
    reversed_geometry = shape(reversed_result.geojson.features[-1].geometry.model_dump(mode="json"))
    reversed_metric = transform(forward.transform, reversed_geometry)
    assert abs(reversed_metric.bounds[3]) > abs(reversed_metric.bounds[1])
    assert north_wind.mode == reversed_result.mode == "wind_informed"


@pytest.mark.parametrize("weather_change", [{"availability": "stale"}, {"wind_speed_mps": None},
                                          {"wind_from_degrees": None}, {"observed_at": None},
                                          {"wind_speed_mps": 0.0}])
def test_missing_stale_or_calm_wind_keeps_distance_context(weather_change, make_request):
    request = make_request()
    weather = request.weather.model_copy(update=weather_change)
    result = assess_exposure(request.model_copy(update={"weather": weather}))
    assert result.availability == "available"
    assert result.mode == "proximity"
    assert all(feature.properties.kind == "proximity" for feature in result.geojson.features)
    assert result.warnings


def test_result_identity_and_heuristic_labels(make_request):
    request = make_request()
    result = assess_exposure(request)
    assert (result.assessment_id, result.fire_ref, result.input_hash) == (
        request.assessment_id, request.fire_ref, request.input_hash,
    )
    assert result.fire == request.fire and result.weather == request.weather
    assert any("not a fire-spread forecast" in assumption for assumption in result.assumptions)
    assert all(feature.properties.elapsed_minutes is None for feature in result.geojson.features)


def test_input_hash_freezes_evidence_but_separates_request_intent(make_request):
    request = make_request()
    assert model_input_hash(request) == model_input_hash(request.model_copy(update={"assessment_id": str(uuid4())}))
    weather = request.weather.model_copy(update={"query_longitude": -122.4})
    assert model_input_hash(request) != model_input_hash(request.model_copy(update={"weather": weather}))
    fire = request.fire.model_copy(update={"source_timestamp": "2026-10-26T13:00:00Z"})
    assert model_input_hash(request) != model_input_hash(request.model_copy(update={"fire": fire}))


def test_source_timestamp_is_aware_utc_and_missing_is_preserved():
    fire = FireObservation(latitude=0.0, longitude=0.0, source_timestamp="2026-10-26T05:00:00-07:00")
    assert fire.source_timestamp == "2026-10-26T12:00:00+00:00"
    assert FireObservation(latitude=0.0, longitude=0.0).source_timestamp is None
    with pytest.raises(ValueError, match="timezone"):
        FireObservation(latitude=0.0, longitude=0.0, source_timestamp="2026-10-26T12:00:00")
