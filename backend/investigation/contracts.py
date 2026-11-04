"""Canonical public contracts; importing models performs no I/O."""
from typing import Literal
from uuid import uuid4

from pydantic import Field, JsonValue

from backend.api_contracts import Contract, Identifier


class InvestigationRequest(Contract):
    request_id: Identifier = Field(default_factory=uuid4)
    thread_id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=4000)
    context_envelope: dict | None = None


class InvestigationTurnError(Contract):
    code: str
    message: str


class InvestigationTurnResponse(Contract):
    """A read-only receipt for one accepted chat request; reading never repeats paid work."""
    request_id: Identifier
    thread_id: str
    attempt_id: Identifier
    status: Literal["running", "completed", "failed", "interrupted"]
    created_at: float
    completed_at: float | None = None
    lease_until: float
    snapshot: dict[str, JsonValue] | None = None
    error: InvestigationTurnError | None = None


class IncidentInvestigationRequest(Contract):
    """Start or explicitly retry one saved investigation at an owned location."""
    run_id: Identifier | None = None
    retry: bool = False


class AgentActivity(Contract):
    tool_call_id: str
    name: str
    status: Literal["running", "awaiting_result", "result_received", "unverified", "error"]
    started_at: float
    ended_at: float | None = None


class ReportedFireObservation(Contract):
    """An owned immutable source point; independent of guidance and spread-model availability."""
    observation_id: Identifier
    source_id: str = Field(min_length=1, max_length=300)
    name: str | None = Field(default=None, max_length=300)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    source_timestamp: str | None = Field(default=None, max_length=60)


class IncidentInvestigationResponse(Contract):
    task_id: str
    incident_id: str
    location_revision: int
    run_id: str | None
    stage: Literal["initial", "simulation"]
    status: Literal["running", "completed", "failed", "interrupted", "cancelled"]
    created_at: float
    completed_at: float | None = None
    activity: list[AgentActivity]
    snapshot: dict[str, JsonValue] | None = None
    error: str | None = None
    input_bundle_id: str | None = None
    input_bundle_sha256: str | None = None
    fire_observations: list[ReportedFireObservation] = Field(default_factory=list)
