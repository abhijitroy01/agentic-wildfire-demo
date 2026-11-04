"""Canonical public contracts; importing models performs no I/O."""
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

API_VERSION = "1"


CAPABILITY_VERSION = "1.0.0"


Identifier = Annotated[UUID, Field(strict=False)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


class ErrorDetails(Contract):
    code: str
    message: str
    retryable: bool = False
    request_id: Identifier | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)


class ErrorResponse(Contract):
    error: ErrorDetails
