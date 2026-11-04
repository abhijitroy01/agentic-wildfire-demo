"""Illustrative proximity and downwind context; these distances are not spread rates."""
from datetime import UTC, datetime

from shapely.geometry import Point

from fire_risk_modeler.geometry import distance_band, ellipse, source_geometry, to_geojson
from fire_risk_modeler.models import (
    EXPOSURE_MODEL_VERSION,
    AssessmentResult,
    ExposureAssessmentRequest,
    ExposureFeature,
    FeatureCollection,
    FeatureProperties,
)


def suitable_wind(request: ExposureAssessmentRequest) -> bool:
    weather = request.weather
    return bool(weather and weather.availability == "available" and weather.observed_at
                and weather.wind_speed_mps is not None
                and (weather.wind_speed_mps == 0 or weather.wind_from_degrees is not None))


def assess_exposure(request: ExposureAssessmentRequest) -> AssessmentResult:
    """Keep proximity context when directional weather is missing, stale or calm."""
    parameters = request.parameters
    warnings = []
    try:
        source = source_geometry(request.fire)
    except ValueError:
        source = Point(0, 0)
        warnings.append("Reported perimeter is invalid; distance context uses the reported fire point.")
    features, inner = [], 0.0
    for outer in parameters.distance_bands_km:
        band = distance_band(source, inner, outer, parameters.ring_points)
        features.append(ExposureFeature(
            geometry=to_geojson(band, request.fire),
            properties=FeatureProperties(
                kind="proximity", label=f"{inner:g}–{outer:g} km distance context",
                inner_km=inner, outer_km=outer,
            ),
        ))
        inner = outer
    directional = suitable_wind(request) and request.weather.wind_speed_mps > 0
    if directional:
        weather = request.weather
        radius = parameters.distance_bands_km[-1] * 1000
        stretch = min(parameters.max_wind_stretch, 1 + weather.wind_speed_mps * parameters.wind_shape_scale)
        outline = ellipse(
            radius * stretch, radius / stretch, radius, (weather.wind_from_degrees + 180) % 360,
            parameters.ring_points,
        )
        # With a supplied perimeter the outer proximity context already reflects that observed area. The
        # directional layer remains explicitly point-anchored; it is not an inferred active perimeter edge.
        features.append(ExposureFeature(
            geometry=to_geojson(outline, request.fire),
            properties=FeatureProperties(kind="downwind", label="Illustrative downwind exposure",
                                         wind_from_degrees=weather.wind_from_degrees),
        ))
    elif not suitable_wind(request):
        warnings.append("Suitable current wind is unavailable; showing distance context.")
    else:
        warnings.append("Reported wind is calm; showing distance context.")
    assumptions = [
        "Illustrative fire exposure; not a fire-spread forecast or burn probability.",
        "Distance bands and wind shaping are documented demo settings, not spread rates.",
        "Temperature and humidity are weather context; humidity is not measured fuel moisture.",
        "Locations outside this context are not established as safe.",
    ]
    if request.fire.perimeter is not None:
        assumptions.append("Reported perimeter used for proximity context; its edge is not assumed active.")
    return AssessmentResult(
        assessment_id=request.assessment_id, fire_ref=request.fire_ref, input_hash=request.input_hash,
        model_version=EXPOSURE_MODEL_VERSION, availability="available",
        mode="wind_informed" if directional else "proximity", calculated_at=datetime.now(UTC).isoformat(),
        fire=request.fire, weather=request.weather, geojson=FeatureCollection(features=features),
        assumptions=assumptions, warnings=warnings,
    )
