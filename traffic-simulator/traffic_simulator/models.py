import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

HIGHWAY_PATTERN = r"^(?:(?:State|US|Interstate) [0-9]{1,3}|osm:(?:ref|name):[0-9a-f]{20}|osm:way:[0-9]+)$"


class Area(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    lat: float = Field(39.7596, ge=-90, le=90)
    lon: float = Field(-121.6219, ge=-180, le=180)
    radius_km: float = Field(8, ge=2, le=15)


class Scenario(Area):
    highway: str = Field("State 191", pattern=HIGHWAY_PATTERN)
    vehicles: int = Field(400, ge=20, le=1500)
    people_per_vehicle: int = Field(2, ge=1, le=8)
    crews: int = Field(4, ge=0, le=16)
    duration_minutes: int = Field(30, ge=5, le=60)
    departure_minutes: int = Field(10, ge=1, le=45)
    seed: int = Field(42, ge=0, le=2147483647)
    exit_direction: Literal["south", "north", "east", "west"] = "south"
    hazard_radius_m: float = Field(150, ge=0, le=3000)
    hazard_growth_m_min: float = Field(10, ge=0, le=100)
    hazard_start_minutes: int = Field(5, ge=0, le=45)
    background_speed_factor: float = Field(1.0, ge=0.3, le=1.0)
    traffic_mode: Literal["synthetic", "provider_speed_limits"] = "synthetic"
    policies: list[Literal["baseline", "coordinated", "phased"]] = Field(
        default_factory=lambda: ["baseline", "coordinated", "phased"], min_length=1, max_length=3
    )

    @model_validator(mode="after")
    def validate_scenario(self):
        """Reject repeated policies, a too-long departure window, an oversized hazard or bad coordinates."""
        if len(set(self.policies)) != len(self.policies):
            raise ValueError("Choose each policy only once")
        if self.departure_minutes > self.duration_minutes:
            raise ValueError("Departure window must fit inside simulation duration")
        if self.hazard_radius_m + self.hazard_growth_m_min * self.duration_minutes > self.radius_km * 900:
            raise ValueError("Hazard scenario exceeds the modeled area; reduce growth or increase area")
        if not math.isfinite(self.lat + self.lon):
            raise ValueError("Coordinates must be finite")
        return self


class PreparationScenario(Scenario):
    """Only a preparation job may leave its road corridor and direction unresolved."""

    highway: Literal["auto"] = "auto"
    exit_direction: Literal["auto"] = "auto"
