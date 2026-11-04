"""Request storage and explicit read-only dependencies for saved investigations."""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Request


@dataclass(frozen=True, slots=True)
class InvestigationReaders:
    """Owned context and evidence readers supplied by the API composition root."""
    context: Callable[..., tuple]
    owned_job: Callable[..., dict]
    build_evidence: Callable[..., Any]
    planning_summary: Callable[[list], str]


def store_for(request: Request):
    return request.app.state.integration_store()
