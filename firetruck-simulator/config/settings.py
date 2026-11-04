"""
Settings configuration using Pydantic BaseSettings
Loads configuration from .env file with type validation

Environment Detection Strategy:
- ENVIRONMENT selects local or ECS behavior
- Operational limits come from deployment/settings.toml (decision D23): ECS tasks
  receive them as environment variables from the stack, local runs read the file
- No config drift between local and deployed
"""
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# deployment/settings.toml in a repository checkout holds each operational limit's value and allowed range.
# Images do not contain it: ECS tasks receive every limit from the stack as an environment variable instead.
SETTINGS_FILE = Path(__file__).resolve().parents[2] / "deployment" / "settings.toml"
# The service's operational limits (D23): each is a Settings field named after its deployment/settings.toml key and
# read from the upper-case variable. deployment/tests/test_installation.py checks every firetruck_* field is declared.
OPERATIONAL_LIMITS = ("firetruck_evidence_events_max", "firetruck_run_lifetime_minutes", "firetruck_runs_per_task")
# Shortest broker service key the private service accepts (FIRETRUCK_SERVICE_KEY); the stack generates 64 characters.
SERVICE_KEY_MIN_LENGTH = 32


def deployment_setting(key: str) -> dict | None:
    """Return one operational setting's entry (value, unit, min, max) from deployment/settings.toml.

    None when this copy has no settings file (the image) or the file does not define the key.
    """
    try:
        sections = tomllib.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    for entries in sections.values():
        entry = entries.get(key) if isinstance(entries, dict) else None
        if isinstance(entry, dict) and "value" in entry:
            return entry
    return None


class Settings(BaseSettings):
    """Application settings loaded from environment variables

    Configuration Philosophy:
    - ENVIRONMENT is the single source of truth for local vs deployed
    - Domain-specific values (truck specs) are hardcoded defaults
    - Only truly deployment-specific values come from .env
    """

    # ==========================================================================
    # ENVIRONMENT DETECTION - Single source of truth for local vs ECS
    # ==========================================================================
    environment: Literal["local", "ecs"] = "local"

    # ==========================================================================
    # TRUCK SPECIFICATIONS - Tank Capacities (gallons)
    # Note: TruckWater model requires tank_capacity_gal >= 500
    # ==========================================================================
    engine_tank_capacity_gal: int = 750
    tanker_tank_capacity_gal: int = 2500
    brush_tank_capacity_gal: int = 500
    rescue_tank_capacity_gal: int = 500  # Min 500 per TruckWater model
    command_tank_capacity_gal: int = 500  # Min 500 per TruckWater model

    # ==========================================================================
    # TRUCK SPECIFICATIONS - Foam Capacities (gallons)
    # Note: TruckWater model requires foam_capacity_gal >= 20
    # ==========================================================================
    engine_foam_capacity_gal: int = 30
    tanker_foam_capacity_gal: int = 50
    brush_foam_capacity_gal: int = 20
    rescue_foam_capacity_gal: int = 20  # Min 20 per TruckWater model
    command_foam_capacity_gal: int = 20  # Min 20 per TruckWater model

    # ==========================================================================
    # TRUCK SPECIFICATIONS - Crew Capacities
    # ==========================================================================
    engine_crew_capacity: int = 4
    tanker_crew_capacity: int = 2
    brush_crew_capacity: int = 3
    rescue_crew_capacity: int = 6
    command_crew_capacity: int = 4

    # ==========================================================================
    # MOVEMENT PARAMETERS (mph) - Realistic fire truck speeds
    # ==========================================================================
    max_speed_highway_mph: int = 65  # Realistic Code 3 response
    max_speed_offroad_mph: int = 40  # Rough terrain/urban
    max_speed_near_fire_mph: int = 20  # Safety approach
    resupply_speed_mph: int = 60  # Returning to refill

    # ==========================================================================
    # DEPLOYMENT PARAMETERS - DEMO MODE: Start visible distance for EN_ROUTE phase
    # ==========================================================================
    # Start 0.5 miles away (~8 sec travel)
    staging_distance_min_mi: float = 0.5
    staging_distance_max_mi: float = 1.0  # Max 1.0 mile away (~12 sec travel)
    deployment_radius_ft: int = 800  # Spread trucks around fire perimeter
    arrival_threshold_ft: int = 2000  # Arrive when 2000 ft from fire center

    # ==========================================================================
    # OPERATIONAL PARAMETERS - DEMO MODE: Fast cycles
    # ==========================================================================
    resupply_trigger_pct: int = 30  # Trigger resupply at 30%
    resupply_duration_sec: int = 10  # 10 sec to refill
    resupply_travel_phase_sec: int = 5  # 5 sec travel time
    resupply_total_duration_sec: int = 15  # 15 sec total resupply cycle
    fuel_tank_size_gal: int = 30

    # ==========================================================================
    # FATIGUE THRESHOLDS (hours)
    # ==========================================================================
    fatigue_fresh_max_hours: float = 1.0
    fatigue_moderate_max_hours: float = 2.0
    fatigue_high_max_hours: float = 3.0

    # ==========================================================================
    # SAFETY THRESHOLDS (feet)
    # ==========================================================================
    danger_zone_threshold_ft: int = 500
    entrapment_critical_dist_ft: int = 300
    entrapment_high_dist_ft: int = 500
    entrapment_medium_dist_ft: int = 1000

    # ==========================================================================
    # ENVIRONMENTAL DEFAULTS
    # ==========================================================================
    base_temp_f: int = 85
    base_wind_mph: int = 12
    base_humidity_pct: int = 30
    base_visibility_mi: float = 2.0
    base_aqi: int = 150

    # Environmental variation per 1000ft
    temp_increase_per_1000ft: int = 5
    visibility_decrease_per_1000ft: float = 0.3
    aqi_increase_per_1000ft: int = 30

    # ==========================================================================
    # FIRE BEHAVIOR
    # ==========================================================================
    initial_fire_size_min_acres: float = 0.5
    fire_growth_moderate_acres_per_hour: int = 5
    fire_spread_moderate_fph: int = 150

    # ==========================================================================
    # FUEL CONSUMPTION (gallons per hour)
    # ==========================================================================
    fuel_consumption_idle_gph: float = 0.5
    fuel_consumption_driving_gph: float = 3.0
    fuel_consumption_pumping_gph: float = 2.5

    # ==========================================================================
    # PUMP AND FLOW PARAMETERS
    # ==========================================================================
    pump_pressure_suppression_min: int = 150
    pump_pressure_suppression_max: int = 200

    # ==========================================================================
    # VEHICLE DEFAULTS
    # ==========================================================================
    odometer_min_miles: int = 20000
    odometer_max_miles: int = 80000

    # ==========================================================================
    # OPERATIONAL LIMITS - defaults live in deployment/settings.toml (D23), never here
    # ==========================================================================
    # Each is read from its upper-case variable: ECS requires it (the stack sets it from
    # deployment/settings.toml), local runs read the file's value, and tests set the variable.
    # FIRETRUCK_EVIDENCE_EVENTS_MAX: latest timeline events each running evidence snapshot carries (events),
    # so publishing every update stays bounded; the count of earlier events is published instead. A
    # finished run's final snapshot keeps every event (capacity.firetruck_evidence_events_max).
    firetruck_evidence_events_max: int | None = None
    # FIRETRUCK_RUN_LIFETIME_MINUTES: longest a run lasts (minutes) before api/private.py stops it and saves
    # its final evidence (capacity.firetruck_run_lifetime_minutes).
    firetruck_run_lifetime_minutes: int | None = None
    # FIRETRUCK_RUNS_PER_TASK: runs one service task simulates at once (runs); past it, a new run gets 429
    # (capacity.firetruck_runs_per_task).
    firetruck_runs_per_task: int | None = None

    @field_validator(*OPERATIONAL_LIMITS, mode="before")
    @classmethod
    def whole_number(cls, value, info: ValidationInfo):
        """Parse an operational limit (a FIRETRUCK_* variable) as a whole number, naming it when it is not one."""
        if value is None or (type(value) is int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        raise RuntimeError(f"{info.field_name.upper()} must be a whole number, not {value!r}")

    @model_validator(mode="after")
    def operational_limits(self) -> Self:
        """Fill and range-check each operational limit: ECS requires its variable, local runs read the file."""
        for name in OPERATIONAL_LIMITS:
            variable, spec = name.upper(), deployment_setting(name)
            if getattr(self, name) is None:
                if self.environment == "ecs" or spec is None:
                    raise RuntimeError(f"{variable} is required: the stack sets it for ECS tasks and local runs "
                                       f"read {name} from deployment/settings.toml")
                setattr(self, name, spec["value"])
            # The image has no settings file; the deployer already range-checked the value the stack passes.
            low, high = (spec.get("min", 1), spec.get("max")) if spec else (1, None)
            value = getattr(self, name)
            if type(value) is not int or value < low or (high is not None and value > high):
                allowed = f"from {low} to {high}" if high is not None else f"at least {low}"
                raise RuntimeError(f"{variable} must be a whole number {allowed}, not {value!r}")
        return self

    # ==========================================================================
    # AWS CONFIGURATION
    # ==========================================================================
    # AWS_REGION / AWS_DEFAULT_REGION: no default Region. The ECS task sets AWS_REGION, the Region of the
    # private service's evidence store (api/private.py); a local .env may name either.
    aws_region: str | None = None
    aws_default_region: str | None = None

    # ==========================================================================
    # PRIVATE SERVICE WIRING (api/private.py) - ECS requires each one, so a misconfigured task fails to start
    # ==========================================================================
    # FIRETRUCK_TABLE / FIRETRUCK_BUCKET: the run table and the evidence bucket the stack creates. Local tests
    # inject an in-memory store instead.
    evidence_table: str | None = Field(None, validation_alias="FIRETRUCK_TABLE")
    evidence_bucket: str | None = Field(None, validation_alias="FIRETRUCK_BUCKET")
    # FIRETRUCK_SERVICE_KEY: the broker's bearer key, from Secrets Manager on ECS (SERVICE_KEY_MIN_LENGTH+).
    service_key: SecretStr | None = Field(None, validation_alias="FIRETRUCK_SERVICE_KEY")

    @model_validator(mode="after")
    def ecs_service_wiring(self) -> Self:
        """On ECS, require the Region, run table, evidence bucket and a strong service key, naming each one."""
        if self.environment != "ecs":
            return self
        for variable, value in (("AWS_REGION", self.aws_region), ("FIRETRUCK_TABLE", self.evidence_table),
                                ("FIRETRUCK_BUCKET", self.evidence_bucket), ("FIRETRUCK_SERVICE_KEY", self.service_key)):
            if not value:
                raise RuntimeError(f"{variable} is required: the stack sets it for ECS tasks")
        if len(self.service_key.get_secret_value()) < SERVICE_KEY_MIN_LENGTH:
            raise RuntimeError(f"FIRETRUCK_SERVICE_KEY must have at least {SERVICE_KEY_MIN_LENGTH} characters")
        return self

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False)


@lru_cache
def get_settings() -> Settings:
    """
    Get cached settings instance.
    Uses lru_cache to ensure settings are loaded only once.
    """
    return Settings()


# =============================================================================
# HELPER FUNCTIONS - Get capacities by unit type string
# =============================================================================

def get_tank_capacity(unit_type: str) -> int:
    """Get water tank capacity for a unit type.

    Note: TruckWater model requires tank_capacity_gal >= 500,
    so default fallback is 500 (minimum valid value).
    """
    settings = get_settings()
    capacities = {
        "ENGINE": settings.engine_tank_capacity_gal,
        "TANKER": settings.tanker_tank_capacity_gal,
        "BRUSH": settings.brush_tank_capacity_gal,
        "RESCUE": settings.rescue_tank_capacity_gal,
        "COMMAND": settings.command_tank_capacity_gal,
    }
    return capacities.get(unit_type.upper(), 500)


def get_foam_capacity(unit_type: str) -> int:
    """Get foam tank capacity for a unit type.

    Note: TruckWater model requires foam_capacity_gal >= 20,
    so default fallback is 20 (minimum valid value).
    """
    settings = get_settings()
    capacities = {
        "ENGINE": settings.engine_foam_capacity_gal,
        "TANKER": settings.tanker_foam_capacity_gal,
        "BRUSH": settings.brush_foam_capacity_gal,
        "RESCUE": settings.rescue_foam_capacity_gal,
        "COMMAND": settings.command_foam_capacity_gal,
    }
    return capacities.get(unit_type.upper(), 20)


def get_crew_capacity(unit_type: str) -> int:
    """Get crew capacity for a unit type"""
    settings = get_settings()
    capacities = {
        "ENGINE": settings.engine_crew_capacity,
        "TANKER": settings.tanker_crew_capacity,
        "BRUSH": settings.brush_crew_capacity,
        "RESCUE": settings.rescue_crew_capacity,
        "COMMAND": settings.command_crew_capacity,
    }
    return capacities.get(unit_type.upper(), 2)


def get_flow_rate_for_pressure(pressure_psi: int) -> int:
    """Get the pump flow rate (GPM) for a pump pressure, within TruckWater's 1,500 GPM limit"""
    # Illustrative values, not a fleet specification. Suppression pressures (150-200 psi) give 450-500 GPM:
    # a full 750 gal engine tank lasts about 90 seconds, so resupply still appears within a demo.
    if pressure_psi <= 0:
        return 0
    elif pressure_psi <= 50:
        return 250
    elif pressure_psi <= 100:
        return 350
    elif pressure_psi <= 150:
        return 450
    elif pressure_psi <= 200:
        return 500
    else:
        return 550
