"""Canonical public contracts; importing models performs no I/O."""
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator
from traffic_simulator.models import HIGHWAY_PATTERN, Scenario
from traffic_simulator.models import PreparationScenario as PendingScenario

from backend.api_contracts import API_VERSION, CAPABILITY_VERSION, Contract, Identifier

PolicyId = Literal["baseline", "coordinated", "phased"]


Availability = Literal["available", "unavailable", "invalid", "not_reached"]


Seconds = Annotated[float, Field(ge=0, le=3600)]


class SimulationScenario(Scenario):
    """The integration boundary requires explicit geographic/profile choices."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_km: float = Field(ge=2, le=15)
    highway: str = Field(pattern=HIGHWAY_PATTERN)
    exit_direction: Literal["south", "north", "east", "west"]
    traffic_mode: Literal["synthetic", "provider_speed_limits"] = "synthetic"


class PreparationScenario(PendingScenario):
    """An explicit point awaiting the worker's corridor and exit selection."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_km: float = Field(ge=2, le=15)
    traffic_mode: Literal["synthetic", "provider_speed_limits"] = "synthetic"


class FinalSelection(Contract):
    kind: Literal["final"] = "final"


class ReplaySelection(Contract):
    kind: Literal["replay"]
    requested_time_s: Seconds


class FinalTime(Contract):
    kind: Literal["final"] = "final"
    effective_time_s: Seconds | None


class ReplayTime(Contract):
    kind: Literal["replay"] = "replay"
    requested_time_s: Seconds
    effective_time_s: Seconds | None


TimeSelection = Annotated[FinalSelection | ReplaySelection, Field(discriminator="kind")]


EvidenceTime = Annotated[FinalTime | ReplayTime, Field(discriminator="kind")]


class EvidenceQuery(Contract):
    run_ids: list[Identifier] = Field(min_length=1, max_length=2)
    cursor: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def distinct_runs(self):
        """Reject an evidence request that names the same run twice."""
        if len(set(self.run_ids)) != len(self.run_ids):
            raise ValueError("Choose each run only once")
        return self


class PolicyQuery(EvidenceQuery):
    policy_ids: list[PolicyId] = Field(
        default_factory=lambda: ["baseline", "coordinated", "phased"], min_length=1, max_length=3
    )

    @model_validator(mode="after")
    def distinct_policies(self):
        """Reject an evidence request that names the same policy twice."""
        if len(set(self.policy_ids)) != len(self.policy_ids):
            raise ValueError("Choose each policy only once")
        return self


class OutcomesRequest(PolicyQuery):
    view: Literal["outcomes"]
    time_scope: FinalSelection = Field(default_factory=FinalSelection)


class CrewRequest(PolicyQuery):
    view: Literal["crew"]
    time_scope: FinalSelection = Field(default_factory=FinalSelection)
    crew_id: str | None = Field(default=None, pattern=r"^crew-[0-9]{1,3}$")


class SourcesRequest(EvidenceQuery):
    view: Literal["sources"]


class AssumptionsRequest(EvidenceQuery):
    view: Literal["assumptions"]


class ReplayRequest(PolicyQuery):
    view: Literal["replay"]
    time_scope: ReplaySelection


class ComparisonRequest(PolicyQuery):
    view: Literal["comparison"]
    time_scope: TimeSelection = Field(default_factory=FinalSelection)

    @model_validator(mode="after")
    def has_comparison(self):
        """Require two policies of one run, or two runs, before comparing."""
        if len(self.run_ids) == 1 and len(self.policy_ids) < 2:
            raise ValueError("Choose two policies or two runs to compare")
        return self


EvidenceRequest = Annotated[
    OutcomesRequest | CrewRequest | SourcesRequest | AssumptionsRequest | ReplayRequest | ComparisonRequest,
    Field(discriminator="view"),
]


class EvidenceFact(Contract):
    fact_id: str
    run_id: Identifier
    metric: str
    value: JsonValue
    unit: str | None = None
    availability: Availability
    reason_code: str | None = None
    time_scope: EvidenceTime | None = None
    policy_id: PolicyId | None = None
    crew_id: str | None = None
    derivation: str
    source_fields: list[str]
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def absent_values_are_explicit(self):
        """Enforce that unavailable facts carry null plus a reason and available facts carry a value."""
        if self.availability != "available" and (self.value is not None or not self.reason_code):
            raise ValueError("Nonavailable facts require a null value and a reason")
        if self.availability == "available" and self.value is None:
            raise ValueError("An available fact requires a value")
        return self


class EvidenceComparison(Contract):
    comparison_id: str
    reference_fact_id: str
    alternative_fact_id: str
    metric: str
    delta: float | None
    unit: str | None
    availability: Availability
    reason_code: str | None = None
    comparison_kind: Literal["policy", "controlled_variant", "changed_conditions", "incompatible"]
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_delta(self):
        """Allow a delta only on available comparisons; others need a reason code."""
        if (self.availability == "available") != (self.delta is not None):
            raise ValueError("Only available comparisons can have a delta")
        if self.availability != "available" and not self.reason_code:
            raise ValueError("Nonavailable comparisons require a reason")
        return self


class ArtifactReference(Contract):
    run_id: Identifier
    artifact: Literal["result"] = "result"
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    hash_encoding: Literal["canonical_json_v1"] = "canonical_json_v1"
    artifact_schema_version: int | None


class EvidencePage(Contract):
    next_cursor: str | None
    has_more: bool
    total_items: int
    returned_items: int
    offset: int
    byte_limit: int


class EvidenceResponse(Contract):
    api_version: Literal["1"] = API_VERSION
    capability_version: Literal["1.0.0"] = CAPABILITY_VERSION
    evidence_id: Identifier
    view: Literal["outcomes", "crew", "sources", "assumptions", "replay", "comparison"]
    artifact_refs: list[ArtifactReference]
    availability: Availability
    facts: list[EvidenceFact]
    comparisons: list[EvidenceComparison]
    missing_reasons: list[str]
    pagination: EvidencePage
    notes: list[str] = Field(default_factory=list)


class CapabilityOperation(Contract):
    id: str
    enabled: bool
    reason_code: str | None = None
    effect: Literal["read", "create_run", "cancel"]
    required_job_states: list[str]
    required_evidence: list[str] = Field(default_factory=list)
    supported_time_scopes: list[Literal["final", "replay"]] = Field(default_factory=list)
    tool: str | None = None
    parameter: str | None = None
    parameter_schema: dict[str, JsonValue] | None = None
    suggested_question: str | None = None


class CapabilityRegistry(Contract):
    api_version: Literal["1"] = API_VERSION
    capability_version: Literal["1.0.0"] = CAPABILITY_VERSION
    artifact_versions_readable: list[int]
    operations: list[CapabilityOperation]
    scenario_schema: dict[str, JsonValue]
    cross_field_constraints: list[str]


class VariantChanges(Contract):
    departure_minutes: int | None = Field(default=None, ge=1, le=45)
    vehicles: int | None = Field(default=None, ge=20, le=1500)

    @model_validator(mode="after")
    def some_change(self):
        """Require at least one supported, non-null change for a run variant."""
        if not self.model_fields_set or any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Supply at least one supported non-null change")
        return self


class VariantRequest(Contract):
    parent_run_id: Identifier
    changes: VariantChanges
    input_mode: Literal["frozen_parent"] = "frozen_parent"
    request_id: Identifier
    capability_version: str


class SubmitRequest(Contract):
    draft_id: Identifier
    draft_revision: int = Field(ge=1)
    request_id: Identifier
    capability_version: str


class SessionRequest(Contract):
    request_id: Identifier
    entry_mode: Literal["radio", "lab"]


class SessionPatch(Contract):
    request_id: Identifier
    expected_revision: int = Field(ge=1)
    selected_run_id: Identifier | None = None
    policy_id: PolicyId = "baseline"
    replay_time_s: Seconds = 0.0


class IncidentCoordinates(Contract):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    label: str | None = Field(default=None, min_length=1, max_length=300)

    @field_validator("label", mode="before")
    @classmethod
    def strip_label(cls, value):
        return value.strip() if isinstance(value, str) else value


class IncidentRequest(Contract):
    request_id: Identifier
    session_id: Identifier
    reported_address: str | None = Field(default=None, min_length=5, max_length=500)
    reported_place: str | None = Field(default=None, min_length=3, max_length=500)
    coordinates: IncidentCoordinates | None = None
    fire_assessment_id: Identifier | None = None
    fire_observation_id: Identifier | None = None
    preset_id: Literal["paradise-2018", "palisades-2025"] | None = None

    @field_validator("reported_address", "reported_place", mode="before")
    @classmethod
    def strip_location(cls, value):
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def one_location_source(self):
        """Keep address, search area, explicit point and reported-fire provenance separate."""
        sources = (self.reported_address, self.reported_place, self.coordinates,
                   self.fire_assessment_id, self.fire_observation_id, self.preset_id)
        if sum(value is not None for value in sources) != 1:
            raise ValueError("Supply exactly one address, place, coordinate point, reported fire or preset")
        return self


class PrepareRequest(Contract):
    request_id: Identifier
    session_id: Identifier
    incident_id: Identifier
    location_revision: int = Field(ge=1)
    # Overrides are explicit assumptions; absent geography is discovered from the incident.
    vehicles: int = Field(default=400, ge=20, le=1500)
    departure_minutes: int = Field(default=10, ge=1, le=30)
    radius_km: float | None = Field(default=None, ge=2, le=15)
    traffic_mode: Literal["synthetic", "provider_speed_limits"] = "synthetic"


class CancelRequest(Contract):
    request_id: Identifier


class StatusRequest(Contract):
    run_id: Identifier


class TurnRequest(Contract):
    request_id: Identifier
    session_id: Identifier
    incident_id: Identifier
    location_revision: int = Field(ge=1)
    question: str = Field(min_length=1, max_length=4000)
    run_id: Identifier | None = None
    policy_id: PolicyId = "baseline"
    replay_time_s: Seconds = 0.0
    intent: Literal["question", "prepare", "submit", "analyze"] = "question"
    draft_id: Identifier | None = None


class SessionResponse(Contract):
    session_id: Identifier
    revision: int
    entry_mode: Literal["radio", "lab"]
    incident_id: Identifier | None = None
    selected_run_id: Identifier | None = None
    latest_draft_id: Identifier | None = None
    policy_id: PolicyId = "baseline"
    replay_time_s: Seconds = 0.0


class LocationReceipt(Contract):
    status: Literal["resolved"]
    reported_address: str
    matched_address: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    coordinate_system: Literal["WGS84"] = "WGS84"
    address_type: str
    provider: Literal["Esri", "Amazon Location", "User input"] = "Esri"
    score: float | None = Field(default=None, ge=0, le=100)
    score_meaning: str | None = None
    resolved_at: str
    resolver_version: Literal[
        "esri-v1", "amazon-location-v1", "coordinates-v1", "fire-observation-v1"
    ] = "esri-v1"
    source_id: str
    origin: Literal[
        "geocoded_address", "geocoded_place", "user_coordinates", "reported_fire"
    ] = "geocoded_address"
    source_timestamp: str | None = None
    source_assessment_id: Identifier | None = None
    source_observation_id: Identifier | None = None
    meaning: str = "Scenario location; not confirmation of an active fire or exact historical ignition."


class IncidentResponse(Contract):
    incident_id: Identifier
    session_id: Identifier
    location_revision: int
    preset_id: str | None
    location: LocationReceipt


class RoadCoverage(Contract):
    """Completed road-coverage evidence, separate from provider or native-operation failures."""
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False, strict=True)
    reason_code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)
    extent_mode: Literal["auto", "fixed"]
    requested_radius_km: float | None = Field(default=None, ge=2, le=15)
    searched_radius_km: float = Field(ge=2, le=15)
    study_bounds: list[Annotated[list[float], Field(min_length=4, max_length=4)]] = Field(
        default_factory=list, max_length=2)
    attempts: list[dict[str, JsonValue]] = Field(default_factory=list)
    source_snapshot: dict[str, JsonValue] | None = None

    @model_validator(mode="after")
    def bounded_geography(self):
        for west, south, east, north in self.study_bounds:
            if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
                raise ValueError("Study bounds must be split WGS84 west, south, east, north boxes")
        return self


class NetworkValidation(Contract):
    requested_radius_km: float
    effective_radius_km: float
    edge_count: int = Field(ge=10, le=14000)
    highway_edge_count: int = Field(ge=1)
    gate_count: int = Field(ge=1, le=3)
    network_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checked_at: str
    adjusted: bool
    attempts: list[dict[str, JsonValue]]
    gates: list[dict[str, JsonValue]] = Field(default_factory=list)
    corridor_label: str | None = Field(default=None, max_length=300)
    study_bounds: list[Annotated[list[float], Field(min_length=4, max_length=4)]] = Field(
        default_factory=list, max_length=2)
    extent_mode: Literal["auto", "fixed"] = "fixed"
    crew_staging_distance_km: float | None = Field(default=None, ge=0)
    coverage: RoadCoverage | None = None

    @model_validator(mode="after")
    def bounded_geography(self):
        """Seam-split WGS84 boxes describe the worker's actual prepared extent."""
        for west, south, east, north in self.study_bounds:
            if not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
                raise ValueError("Study bounds must be split WGS84 west, south, east, north boxes")
        return self


class DraftResponse(Contract):
    draft_id: Identifier
    draft_revision: int
    status: Literal["preparing", "ready", "unavailable", "failed"] = "ready"
    session_id: Identifier
    incident_id: Identifier
    location_revision: int
    scenario: SimulationScenario | PreparationScenario
    scenario_hash: str
    parent_run_id: Identifier | None = None
    input_mode: Literal["current", "frozen_parent"]
    changes: dict[str, JsonValue] = Field(default_factory=dict)
    preparation_id: Identifier | None = None
    phase: str | None = None
    error: str | None = None
    error_code: str | None = None
    road_coverage: RoadCoverage | None = None
    network_validation: NetworkValidation | None = None

    @model_validator(mode="after")
    def ready_scenario_is_concrete(self):
        """A pending corridor or exit can never be reviewed or submitted as a ready scenario."""
        if self.status == "ready":
            self.scenario = SimulationScenario.model_validate(self.scenario.model_dump())
        return self


class RunResponse(Contract):
    run_id: Identifier
    session_id: Identifier
    incident_id: Identifier
    location_revision: int
    status: Literal["queued", "running", "cancelling", "cancelled", "completed", "failed"]
    progress: float
    phase: str
    scenario: SimulationScenario
    created_at: str
    error: str | None = None
    error_code: str | None = None
    parent_run_id: Identifier | None = None
    draft_id: Identifier


class UiAction(Contract):
    type: Literal["select_policy", "seek_time", "focus_crew", "open_panel",
                  "open_comparison", "inspect_source", "show_variant_draft"]
    policy_id: PolicyId | None = None
    time_s: Seconds | None = None
    crew_id: str | None = None
    panel: Literal["overview", "bottlenecks", "crews", "evidence"] | None = None
    draft_id: Identifier | None = None


class SimulationOutput(Contract):
    schema_version: Literal[1] = 1
    session_id: Identifier
    incident_id: Identifier
    location_revision: int
    turn_id: Identifier
    status: Literal["submitted", "answer", "needs_input", "unsupported", "error"]
    question: str
    answer: str
    run: RunResponse | None = None
    draft: DraftResponse | None = None
    evidence: list[EvidenceResponse] = Field(default_factory=list)
    ui_actions: list[UiAction] = Field(default_factory=list)
    capability_version: str = CAPABILITY_VERSION
    warnings: list[str] = Field(default_factory=list)
