"""Canonical model contracts. Importing these definitions performs no I/O."""
import hashlib
import json
from datetime import UTC, datetime
from itertools import pairwise
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EXPOSURE_MODEL_VERSION = "wind-exposure-1.0.0"
SURFACE_FIRE_MODEL_VERSION = "surface-fire-1.0.0"
LANDSCAPE_MODEL_VERSION = "landscape-surface-fire-1.0.0"
SURFACE_WIND_ASSUMPTION = "DTN wind treated as 10 m above ground; open unsheltered fuel assumed"
Position = Annotated[list[float], Field(min_length=2, max_length=2)]
Ring = Annotated[list[Position], Field(min_length=4)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


def timestamp(value: str | None) -> str | None:
    """Require an explicit timezone and normalize equivalent provider times to UTC."""
    if value is not None:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("Timestamp must include its timezone")
        return parsed.astimezone(UTC).isoformat()
    return value


def validate_rings(rings: list[list[list[float]]]) -> None:
    for ring in rings:
        if ring[0] != ring[-1]:
            raise ValueError("GeoJSON rings must be closed")
        if any(not -180 <= position[0] <= 180 or not -90 <= position[1] <= 90 for position in ring):
            raise ValueError("GeoJSON positions must use globally valid longitude, latitude")


class PolygonGeometry(Contract):
    type: Literal["Polygon"] = "Polygon"
    coordinates: list[Ring] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_positions(self):
        validate_rings(self.coordinates)
        return self


class MultiPolygonGeometry(Contract):
    type: Literal["MultiPolygon"] = "MultiPolygon"
    coordinates: list[Annotated[list[Ring], Field(min_length=1)]] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_positions(self):
        for polygon in self.coordinates:
            validate_rings(polygon)
        return self


Geometry = Annotated[PolygonGeometry | MultiPolygonGeometry, Field(discriminator="type")]


class FireObservation(Contract):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    name: str | None = Field(default=None, max_length=300)
    source_id: str | None = Field(default=None, max_length=300)
    source_timestamp: str | None = Field(default=None, max_length=60)
    perimeter: Geometry | None = None

    _timestamp = field_validator("source_timestamp")(timestamp)


class WeatherObservation(Contract):
    query_latitude: float = Field(ge=-90, le=90)
    query_longitude: float = Field(ge=-180, le=180)
    observed_at: str | None = Field(default=None, max_length=60)
    availability: Literal["available", "unavailable", "stale"]
    wind_speed_mps: float | None = Field(default=None, ge=0, le=150)
    wind_from_degrees: float | None = Field(default=None, ge=0, le=360)
    source: Literal["DTN"] = "DTN"
    temperature_c: float | None = Field(default=None, ge=-100, le=100)
    relative_humidity_percent: float | None = Field(default=None, ge=0, le=100)

    _timestamp = field_validator("observed_at")(timestamp)


class ExposureParameters(Contract):
    distance_bands_km: list[float] = Field(min_length=1, max_length=6)
    ring_points: int = Field(ge=32, le=360)
    wind_shape_scale: float = Field(ge=0, le=1)
    max_wind_stretch: float = Field(ge=1, le=3)

    @model_validator(mode="after")
    def increasing_distances(self):
        distances = self.distance_bands_km
        if any(not 0 < distance <= 200 for distance in distances):
            raise ValueError("Distances must be greater than zero and at most 200 km")
        if any(first >= second for first, second in pairwise(distances)):
            raise ValueError("Distance bands must increase strictly")
        return self


class SurfaceFireScenario(Contract):
    engine: Literal["Pyrothermel/Behave"] = "Pyrothermel/Behave"
    fuel_model: int = Field(ge=1, le=13)
    moisture_profile: Literal["low", "moderate", "high"]
    slope_degrees: float = Field(ge=0, le=0)
    wind_height_assumption: Literal[
        "DTN wind treated as 10 m above ground; open unsheltered fuel assumed"
    ]
    initialization: Literal["reported point treated as demo ignition"]
    horizons_minutes: list[float] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def increasing_horizons(self):
        horizons = self.horizons_minutes
        if any(not 0 < horizon <= 120 for horizon in horizons):
            raise ValueError("Scenario horizons must be greater than zero and at most 120 minutes")
        if any(first >= second for first, second in pairwise(horizons)):
            raise ValueError("Scenario horizons must increase strictly")
        return self


class ExposureAssessmentRequest(Contract):
    schema_version: Literal["1"] = "1"
    assessment_id: str = Field(min_length=36, max_length=36)
    fire_ref: str = Field(min_length=1, max_length=512)
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    fire: FireObservation
    weather: WeatherObservation | None = None
    mode: Literal["exposure", "surface_fire"] = "exposure"
    parameters: ExposureParameters
    scenario: SurfaceFireScenario | None = None

    @field_validator("assessment_id")
    @classmethod
    def valid_id(cls, value):
        UUID(value)
        return value

    @model_validator(mode="after")
    def scenario_matches_mode(self):
        if (self.mode == "surface_fire") != (self.scenario is not None):
            raise ValueError("Only surface_fire mode requires scenario assumptions")
        return self


class LandscapeManifest(Contract):
    """A sealed numerical crop; regional routing never changes the declared products."""
    region: Literal["CONUS", "AK", "HI"]
    fuel_product: Literal["LANDFIRE LF2024 FBFM40"] = "LANDFIRE LF2024 FBFM40"
    elevation_product: Literal["LANDFIRE LF2020 Elev"] = "LANDFIRE LF2020 Elev"
    fuel_source: str = Field(max_length=200)
    elevation_source: str = Field(max_length=200)
    captured_at: str = Field(max_length=60)
    crs: Literal["EPSG:5070", "EPSG:3338", "ESRI:102007"]
    width: int = Field(ge=5, le=1000)
    height: int = Field(ge=5, le=1000)
    resolution_m: float = Field(ge=30, le=30)
    west: float
    north: float
    nodata: Literal[-9999] = -9999
    elevation_units: Literal["metres"] = "metres"
    raster_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    fuel_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    elevation_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    _timestamp = field_validator("captured_at")(timestamp)

    @model_validator(mode="after")
    def declared_sources(self):
        base = "https://lfps.usgs.gov/arcgis/rest/services/"
        if self.fuel_source != f"{base}Landfire_LF2024/LF2024_FBFM40_{self.region}/ImageServer":
            raise ValueError("Landscape fuel source must be its canonical regional LANDFIRE product")
        if self.elevation_source != f"{base}Landfire_Topo/LF2020_Elev_{self.region}/ImageServer":
            raise ValueError("Landscape elevation source must be its canonical regional LANDFIRE product")
        expected = {"CONUS": "EPSG:5070", "AK": "EPSG:3338", "HI": "ESRI:102007"}
        if self.crs != expected[self.region] or self.width * self.height > 250_000:
            raise ValueError("Landscape CRS or cell count exceeds its declared contract")
        return self


class LandscapeInput(Contract):
    manifest: LandscapeManifest
    encoding: Literal["zlib-base64-int16-float32-le"] = "zlib-base64-int16-float32-le"
    # Fuel row-major int16 followed by elevation row-major float32; compressed, sealed, bounded.
    raster_data: str = Field(min_length=1, max_length=2_100_000)


class LandscapeScenario(Contract):
    engine: Literal["Pyrothermel/Behave directional arrival grid"] = "Pyrothermel/Behave directional arrival grid"
    moisture_profile: Literal["low", "moderate", "high"]
    wind_height_assumption: Literal[
        "DTN wind treated as 10 m above ground; open unsheltered fuel assumed"
    ]
    initialization: Literal["reported point treated as demo ignition"]
    horizons_minutes: list[float] = Field(min_length=1, max_length=6)
    directional_radius: int = Field(ge=1, le=5)

    _horizons = model_validator(mode="after")(SurfaceFireScenario.increasing_horizons)


class LandscapeAssessmentRequest(Contract):
    schema_version: Literal["2"] = "2"
    mode: Literal["landscape"] = "landscape"
    assessment_id: str = Field(min_length=36, max_length=36)
    fire_ref: str = Field(min_length=1, max_length=512)
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    fire: FireObservation
    weather: WeatherObservation
    scenario: LandscapeScenario
    landscape: LandscapeInput

    _id = field_validator("assessment_id")(ExposureAssessmentRequest.valid_id.__func__)


class AssessmentFailure(Contract):
    code: Literal[
        "DTN_WIND_UNUSABLE", "LANDSCAPE_INPUT_INVALID", "LANDSCAPE_COVERAGE_MISSING", "LANDSCAPE_REQUEST_FAILED",
        "LANDSCAPE_NODATA", "IGNITION_DATA_CONFLICT", "FUEL_CODE_UNSUPPORTED",
        "DOMAIN_TRUNCATED", "CALCULATION_FAILED", "CALCULATION_LIMIT",
    ]
    message: str = Field(min_length=1, max_length=300)


class LandscapeSummary(Contract):
    manifest: LandscapeManifest
    solver: Literal["directed-cell-arrival-1"] = "directed-cell-arrival-1"
    directional_radius: int = Field(ge=1, le=5)
    ignition_row: int = Field(ge=0)
    ignition_column: int = Field(ge=0)
    ignition_fuel_model: int
    ignition_slope_degrees: float = Field(ge=0, le=90)
    ignition_aspect_degrees: float = Field(ge=0, le=360)
    reached_cells: int = Field(ge=0)
    spread_cells: int = Field(ge=0)
    modeled_cells: int = Field(ge=0)
    nonburnable_cells: int = Field(ge=0)
    nodata_cells: int = Field(ge=0)
    native_states: int = Field(ge=0)
    maximum_arrival_minutes: float = Field(ge=0)


class FeatureProperties(Contract):
    kind: Literal["proximity", "downwind", "surface_fire", "landscape"]
    label: str = Field(min_length=1, max_length=300)
    inner_km: float | None = Field(default=None, ge=0, le=200)
    outer_km: float | None = Field(default=None, gt=0, le=200)
    elapsed_minutes: float | None = Field(default=None, gt=0, le=120)
    wind_from_degrees: float | None = Field(default=None, ge=0, le=360)


class ExposureFeature(Contract):
    type: Literal["Feature"] = "Feature"
    geometry: Geometry
    properties: FeatureProperties


class FeatureCollection(Contract):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[ExposureFeature] = Field(default_factory=list, max_length=12)


class AssessmentResult(Contract):
    schema_version: Literal["1", "2"] = "1"
    assessment_id: str = Field(min_length=36, max_length=36)
    fire_ref: str = Field(min_length=1, max_length=512)
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    model_version: str = Field(min_length=1, max_length=80)
    availability: Literal["available", "unavailable"]
    mode: Literal["proximity", "wind_informed", "surface_fire", "landscape"]
    calculated_at: str = Field(max_length=60)
    fire: FireObservation
    weather: WeatherObservation | None = None
    geojson: FeatureCollection
    assumptions: list[str] = Field(max_length=20)
    warnings: list[str] = Field(default_factory=list, max_length=20)
    scenario: SurfaceFireScenario | LandscapeScenario | None = None
    landscape: LandscapeSummary | None = None
    failure: AssessmentFailure | None = None

    _timestamp = field_validator("calculated_at")(timestamp)
    _id = field_validator("assessment_id")(ExposureAssessmentRequest.valid_id.__func__)

    @model_validator(mode="after")
    def landscape_outcome(self):
        if self.schema_version == "2" and self.mode != "landscape":
            raise ValueError("Schema-2 results require the declared landscape mode")
        if self.mode == "landscape":
            if self.schema_version != "2" or self.model_version != LANDSCAPE_MODEL_VERSION:
                raise ValueError("Landscape results require their canonical schema and model identity")
            if self.scenario is not None and not isinstance(self.scenario, LandscapeScenario):
                raise ValueError("Landscape mode cannot use a homogeneous scenario")
            if self.availability == "available":
                if (self.failure is not None or self.landscape is None or self.scenario is None
                        or not self.geojson.features):
                    raise ValueError("Successful landscape output requires measured grid provenance and geometry")
                if any(feature.properties.kind != "landscape" for feature in self.geojson.features):
                    raise ValueError("Landscape mode cannot substitute distance context or homogeneous geometry")
            elif self.failure is None or self.geojson.features:
                raise ValueError("Failed landscape output requires a typed reason and empty geometry")
        return self


def model_version(mode: str) -> str:
    if mode == "landscape":
        return LANDSCAPE_MODEL_VERSION
    return SURFACE_FIRE_MODEL_VERSION if mode == "surface_fire" else EXPOSURE_MODEL_VERSION


def model_input_hash(request: ExposureAssessmentRequest | LandscapeAssessmentRequest) -> str:
    """Hash effective immutable inputs; intent identity and calculation time are separate."""
    values = request.model_dump(mode="json", exclude={"assessment_id", "input_hash"})
    values["model_version"] = model_version(request.mode)
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_unavailable_result(request: ExposureAssessmentRequest, warnings: list[str]) -> AssessmentResult:
    """Create the same typed failure representation without importing model or service code."""
    return AssessmentResult(
        assessment_id=request.assessment_id, fire_ref=request.fire_ref, input_hash=request.input_hash,
        model_version=model_version(request.mode), availability="unavailable",
        mode="surface_fire" if request.mode == "surface_fire" else "proximity",
        calculated_at=datetime.now(UTC).isoformat(), fire=request.fire, weather=request.weather,
        geojson=FeatureCollection(), assumptions=["Illustrative fire exposure; not a fire-spread forecast."],
        warnings=warnings, scenario=request.scenario,
    )
