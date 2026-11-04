"""Pyrothermel/Behave point-ignition demo with explicit uniform-fuel assumptions."""
import math
from datetime import UTC, datetime
from importlib.metadata import version

from shapely.geometry import Polygon

from fire_risk_modeler.exposure import suitable_wind
from fire_risk_modeler.geometry import to_geojson
from fire_risk_modeler.models import (
    SURFACE_FIRE_MODEL_VERSION,
    AssessmentResult,
    ExposureAssessmentRequest,
    ExposureFeature,
    FeatureCollection,
    FeatureProperties,
    build_unavailable_result,
)


def directional_rates(request: ExposureAssessmentRequest) -> list[tuple[float, float]]:
    """Return ignition-centred rates (m/s) for each geographic bearing, from the native calculator."""
    import pyrothermel
    if version("pyrothermel") != "0.1.4":
        raise ValueError("Unsupported native calculator version")
    weather, scenario = request.weather, request.scenario
    fuel = pyrothermel.FuelModel.from_existing(scenario.fuel_model, units_preset="metric")
    moisture = pyrothermel.MoistureScenario.from_existing(
        dead_fuel_moisture_class=scenario.moisture_profile,
        live_fuel_moisture_class=scenario.moisture_profile,
    )
    run = pyrothermel.PyrothermelRun(
        fuel, moisture, weather.wind_speed_mps * 3.6, units_preset="metric", wind_input_mode="ten_meter",
        # Behave interprets this as meteorological FROM relative to north. Adding 180 here would reverse growth.
        wind_direction=weather.wind_from_degrees if weather.wind_speed_mps else 0.0, slope=0.0,
        canopy_cover=0.0, canopy_height=0.0, canopy_ratio=0.0,
    )
    run.run_surface_fire_in_direction_of_max_spread()
    rates = []
    for index in range(request.parameters.ring_points):
        bearing = index * 360 / request.parameters.ring_points
        output = run.run_surface_fire_in_direction_of_interest(bearing)
        speed = output["spread_rate"] / 3.6  # The metric preset returns kilometres/hour.
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError("Selected assumptions do not yield positive surface-fire spread")
        rates.append((bearing, speed))
    return rates


def assess_surface_fire(request: ExposureAssessmentRequest) -> AssessmentResult:
    if not suitable_wind(request):
        return build_unavailable_result(
            request, ["Surface-fire scenario requires suitable current wind; exposure remains separate."]
        )
    try:
        rates = directional_rates(request)
    except (ImportError, ValueError):
        return build_unavailable_result(
            request, ["Native surface-fire calculator or selected assumptions are unavailable."]
        )
    features = []
    for minutes in request.scenario.horizons_minutes:
        if max(speed for _, speed in rates) * minutes * 60 > 200_000:
            return build_unavailable_result(
                request, ["Scenario footprint exceeds the supported local geometry extent."]
            )
        positions = [
            (speed * minutes * 60 * math.sin(math.radians(bearing)),
             speed * minutes * 60 * math.cos(math.radians(bearing)))
            for bearing, speed in rates
        ]
        features.append(ExposureFeature(
            geometry=to_geojson(Polygon(positions), request.fire),
            properties=FeatureProperties(
                kind="surface_fire", label=f"{minutes:g} minutes — surface-fire demo scenario",
                elapsed_minutes=minutes, wind_from_degrees=request.weather.wind_from_degrees,
            ),
        ))
    assumptions = [
        "Illustrative homogeneous surface-fire scenario; not an evacuation forecast or burn probability.",
        f"Uniform standard fuel model {request.scenario.fuel_model}; not measured local vegetation.",
        f"Assumed {request.scenario.moisture_profile} dead/live moisture profile; not derived from humidity.",
        "Flat slope, no canopy shelter, and constant selected DTN wind.",
        request.scenario.wind_height_assumption,
        "Reported point treated as demo ignition; outlines are elapsed scenario time, not future arrival.",
        "Reported perimeter, suppression, spotting, crown fire and changing weather are not propagated.",
        "Locations outside the simulated footprint are not established as safe.",
    ]
    return AssessmentResult(
        assessment_id=request.assessment_id, fire_ref=request.fire_ref, input_hash=request.input_hash,
        model_version=SURFACE_FIRE_MODEL_VERSION, availability="available", mode="surface_fire",
        calculated_at=datetime.now(UTC).isoformat(), fire=request.fire, weather=request.weather,
        geojson=FeatureCollection(features=features), assumptions=assumptions, scenario=request.scenario,
    )
