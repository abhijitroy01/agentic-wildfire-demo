"""Public background fire-risk receipts; source inputs remain private, immutable evidence."""
from typing import Literal
from uuid import UUID

from fire_risk_modeler.models import AssessmentResult, Contract
from pydantic import Field, field_validator


class FireRiskPreparationError(Contract):
    """A failed answer-to-job handoff; provider/SDK error details never become public content."""
    code: Literal["RISK_PREPARATION_FAILED"]
    message: Literal["Terrain assessment preparation could not be queued."]

    @classmethod
    def unavailable(cls):
        return cls(code="RISK_PREPARATION_FAILED",
                   message="Terrain assessment preparation could not be queued.")


class FireRiskJobReference(Contract):
    job_id: str = Field(min_length=36, max_length=36)
    fire_refs: list[str] = Field(min_length=1, max_length=4)

    @field_validator("job_id")
    @classmethod
    def valid_id(cls, value):
        UUID(value)
        return value


class FireRiskJobResponse(FireRiskJobReference):
    status: Literal["queued", "running", "completed", "failed", "cancelling", "cancelled"]
    phase: str = Field(min_length=1, max_length=300)
    progress: float = Field(ge=0, le=1)
    assessments: list[AssessmentResult] = Field(default_factory=list, max_length=4)
    error: str | None = Field(default=None, max_length=300)


class FireRiskRetryRequest(Contract):
    request_id: UUID = Field(strict=False)
