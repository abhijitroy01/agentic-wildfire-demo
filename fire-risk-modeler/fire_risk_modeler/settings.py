"""Component settings from the existing deployment/settings.toml authority (D23)."""
import math
import os
import tomllib
from functools import cache
from pathlib import Path

from fire_risk_modeler.models import ExposureParameters

SETTINGS_FILE = Path(__file__).resolve().parents[2] / "deployment" / "settings.toml"
VARIABLES = (
    "FIRE_RISK_INNER_KM", "FIRE_RISK_MIDDLE_KM", "FIRE_RISK_OUTER_KM",
    "FIRE_RISK_RING_POINTS", "FIRE_RISK_WIND_SHAPE_SCALE", "FIRE_RISK_MAX_WIND_STRETCH",
    "FIRE_RISK_REQUEST_BODY_MAX_KIB", "FIRE_RISK_RESPONSE_MAX_KIB", "FIRE_RISK_PERIMETER_MAX_VERTICES",
    "FIRE_RISK_CALCULATIONS_PER_PROCESS", "FIRE_RISK_CALCULATION_TIMEOUT_SECONDS",
    "FIRE_RISK_LANDSCAPE_REQUEST_MAX_KIB", "FIRE_RISK_LANDSCAPE_CALCULATION_TIMEOUT_SECONDS",
    "FIRE_RISK_LANDSCAPE_MAX_CELLS", "FIRE_RISK_LANDSCAPE_DIRECTIONAL_RADIUS",
)


@cache
def operational_settings() -> dict:
    try:
        with SETTINGS_FILE.open("rb") as handle:
            sections = tomllib.load(handle)
    except FileNotFoundError:
        raise RuntimeError("Modeler requires deployment/settings.toml in its source/image") from None
    return {key.upper(): spec for entries in sections.values() for key, spec in entries.items()}


def environment() -> str:
    value = os.getenv("FIRE_RISK_ENVIRONMENT", "local")
    if value not in ("local", "ecs"):
        raise RuntimeError("FIRE_RISK_ENVIRONMENT must be local or ecs")
    return value


def limit(variable: str) -> int | float:
    spec = operational_settings().get(variable)
    if spec is None:
        raise RuntimeError(f"{variable} is not declared in deployment/settings.toml")
    raw = os.getenv(variable)
    if raw is None and environment() == "ecs":
        raise RuntimeError(f"ECS modeler requires {variable}")
    if raw is None:
        return spec["value"]
    kind = float if isinstance(spec["value"], float) else int
    try:
        value = kind(raw)
    except ValueError:
        value = None
    if value is None or not math.isfinite(value) or not spec["min"] <= value <= spec["max"]:
        raise RuntimeError(f"{variable} must be between {spec['min']} and {spec['max']} {spec['unit']}")
    return value


def service_key() -> str:
    value = os.getenv("FIRE_RISK_SERVICE_KEY", "")
    if len(value) < 32 or any(character.isspace() for character in value):
        raise RuntimeError("FIRE_RISK_SERVICE_KEY must have at least 32 characters and no whitespace")
    return value


def default_parameters() -> ExposureParameters:
    return ExposureParameters(
        distance_bands_km=[
            float(limit("FIRE_RISK_INNER_KM")), float(limit("FIRE_RISK_MIDDLE_KM")),
            float(limit("FIRE_RISK_OUTER_KM")),
        ],
        ring_points=limit("FIRE_RISK_RING_POINTS"), wind_shape_scale=limit("FIRE_RISK_WIND_SHAPE_SCALE"),
        max_wind_stretch=limit("FIRE_RISK_MAX_WIND_STRETCH"),
    )


def validate() -> None:
    environment()
    service_key()
    for variable in VARIABLES:
        limit(variable)
    default_parameters()
