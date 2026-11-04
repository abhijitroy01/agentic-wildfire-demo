"""Fire modeler reference calculator output and directional, cumulative scenario geometry."""
import pyrothermel
import pytest
from fire_risk_modeler.geometry import projection
from fire_risk_modeler.models import SURFACE_WIND_ASSUMPTION
from fire_risk_modeler.surface_fire import assess_surface_fire, directional_rates
from shapely.geometry import shape
from shapely.ops import transform


def scenario():
    return {
        "fuel_model": 1, "moisture_profile": "moderate", "slope_degrees": 0.0,
        "wind_height_assumption": SURFACE_WIND_ASSUMPTION,
        "initialization": "reported point treated as demo ignition", "horizons_minutes": [15.0, 30.0, 60.0],
    }


def test_official_readme_reference_output():
    # Official Pyrothermel README: TL8, low dead/moderate live moisture, no wind, stated canopy inputs.
    fuel = pyrothermel.FuelModel.from_existing("TL8")
    moisture = pyrothermel.MoistureScenario.from_existing("low", "moderate")
    run = pyrothermel.PyrothermelRun(
        fuel, moisture, 0.0, wind_input_mode="ten_meter", canopy_base_height=2.5,
        canopy_bulk_density=0.1, canopy_cover=0.5, canopy_height=20.0, canopy_ratio=0.6,
    )
    result = run.run_surface_fire_in_direction_of_max_spread()
    assert result["spread_rate"] == pytest.approx(0.035297, abs=0.0000005)
    assert result["flame_length"] == pytest.approx(0.850504, abs=0.0000005)


def test_native_wind_from_convention_and_units(make_request):
    request = make_request(mode="surface_fire", scenario=scenario())
    rates = dict(directional_rates(request))
    assert rates[180.0] == pytest.approx(6.510120889882055 / 3.6, rel=1e-8)
    assert rates[180.0] > rates[90.0] > rates[0.0]
    opposite = request.model_copy(update={"weather": request.weather.model_copy(
        update={"wind_from_degrees": 180.0}
    )})
    reversed_rates = dict(directional_rates(opposite))
    assert reversed_rates[0.0] == pytest.approx(rates[180.0], rel=1e-8)


@pytest.mark.parametrize("latitude,longitude", [
    (0.0, 0.0), (47.6, -122.3), (0.0, 179.99), (89.99, 0.0), (-90.0, 0.0),
])
def test_scenario_outlines_are_valid_and_cumulative(latitude, longitude, make_request):
    request = make_request(latitude=latitude, longitude=longitude, mode="surface_fire", scenario=scenario())
    result = assess_surface_fire(request)
    assert result.availability == "available" and result.mode == "surface_fire"
    forward, _ = projection(request.fire)
    previous = None
    for feature in result.geojson.features:
        geometry = shape(feature.geometry.model_dump(mode="json"))
        assert geometry.is_valid
        # Compare in the physical coordinate frame, not longitude-degree area near the antimeridian.
        metric = transform(forward.transform, geometry)
        if previous is not None:
            assert metric.area > previous.area
        previous = metric
    assert result.scenario == request.scenario
    assert any("not measured local vegetation" in assumption for assumption in result.assumptions)


def test_missing_weather_returns_typed_unavailable_scenario(make_request):
    result = assess_surface_fire(make_request(weather=False, mode="surface_fire", scenario=scenario()))
    assert result.availability == "unavailable" and not result.geojson.features
    assert result.warnings
