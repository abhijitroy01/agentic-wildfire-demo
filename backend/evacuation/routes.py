"""Owner-scoped evacuation v1 API, independent of the application startup."""
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from traffic_simulator.errors import IntegrationError

from backend.api_contracts import ErrorResponse
from backend.dependencies import store_for
from backend.evacuation.capabilities import capabilities
from backend.evacuation.contracts import (
    CancelRequest,
    CapabilityRegistry,
    DraftResponse,
    EvidenceRequest,
    EvidenceResponse,
    IncidentRequest,
    IncidentResponse,
    PrepareRequest,
    RunResponse,
    SessionPatch,
    SessionRequest,
    SessionResponse,
    SubmitRequest,
    VariantRequest,
)
from backend.evacuation.evidence import build_evidence
from backend.simulation_artifacts import artifact_for, result_for


def create_router(owner_dependency):
    """Dependency injection lets the offline exporter inspect the exact production routes."""
    router = APIRouter(
        prefix="/api/v1", tags=["Evacuation & Crew Access"],
        responses={status: {"model": ErrorResponse} for status in (401, 404, 409, 410, 422, 503)},
    )
    authenticated_owner = Annotated[str, Depends(owner_dependency)]
    storage = Annotated[Any, Depends(store_for)]

    @router.get("/presets", operation_id="simulation_presets")
    def presets(user: authenticated_owner):
        from backend.evacuation.locations import PRESETS
        return [{"preset_id": key, **value, "input_mode": "current",
                 "meaning": "Historical reference, current inputs; representative scenario point."}
                for key, value in PRESETS.items()]

    @router.post("/sessions", response_model=SessionResponse, operation_id="create_simulation_session")
    def create_session(body: SessionRequest, store: storage, user: authenticated_owner):
        from backend.evacuation.service import create_session
        return create_session(store, user, body)

    @router.get("/sessions/{session_id}", response_model=SessionResponse, operation_id="get_simulation_session")
    def get_session(session_id: UUID, store: storage, user: authenticated_owner):
        from traffic_simulator.persistence import owned

        from backend.evacuation.service import session_view
        return session_view(owned(store, "session", session_id, user))

    @router.patch("/sessions/{session_id}", response_model=SessionResponse, operation_id="update_simulation_session")
    def update_session(session_id: UUID, body: SessionPatch, store: storage, user: authenticated_owner):
        from backend.evacuation.service import patch_session
        return patch_session(store, user, session_id, body)

    @router.post("/incidents", response_model=IncidentResponse, operation_id="resolve_simulation_incident")
    def create_incident(body: IncidentRequest, store: storage, user: authenticated_owner):
        from backend.evacuation.service import create_incident
        return create_incident(store, user, body)

    @router.get("/incidents/{incident_id}", response_model=IncidentResponse, operation_id="get_simulation_incident")
    def get_incident(incident_id: UUID, store: storage, user: authenticated_owner):
        from traffic_simulator.persistence import owned
        value = owned(store, "incident", incident_id, user)
        return {k: value[k] for k in IncidentResponse.model_fields}

    @router.post("/scenario-drafts", response_model=DraftResponse, operation_id="prepare_simulation")
    def prepare(body: PrepareRequest, store: storage, user: authenticated_owner):
        from backend.evacuation.service import prepare
        return prepare(store, user, body)

    @router.get("/scenario-drafts/{draft_id}", response_model=DraftResponse, operation_id="get_simulation_draft")
    def get_draft(draft_id: UUID, store: storage, user: authenticated_owner):
        from traffic_simulator.persistence import owned

        from backend.evacuation.service import draft_view
        value = owned(store, "draft", draft_id, user)
        return draft_view(store, value)

    @router.get("/scenario-drafts/{draft_id}/network", operation_id="get_prepared_simulation_network")
    def get_draft_network(draft_id: UUID, store: storage, user: authenticated_owner):
        from traffic_simulator.persistence import owned

        from backend.evacuation.service import draft_view
        value = draft_view(store, owned(store, "draft", draft_id, user))
        if value["status"] != "ready" or not value["preparation_id"]:
            raise IntegrationError("NETWORK_NOT_READY", "Road-network validation is not complete.", 409)
        from backend.simulation_artifacts import retained_artifact
        return retained_artifact(store, value["preparation_id"], "network")

    @router.post("/scenario-drafts/{draft_id}/cancel", response_model=DraftResponse,
                 operation_id="cancel_simulation_preparation")
    def cancel_draft(draft_id: UUID, body: CancelRequest, store: storage, user: authenticated_owner):
        from traffic_simulator.persistence import owned

        from backend.evacuation.service import draft_view
        value = owned(store, "draft", draft_id, user)
        if value.get("preparation_id") and not value.get("run_id"):
            store.cancel_run(value["preparation_id"])
        return draft_view(store, value)

    @router.post("/runs", response_model=RunResponse, status_code=202, operation_id="submit_simulation")
    def submit(body: SubmitRequest, store: storage, user: authenticated_owner):
        from backend.evacuation.service import submit
        return submit(store, user, body)

    @router.get("/runs", response_model=list[RunResponse], operation_id="list_simulation_runs")
    def list_runs(store: storage, user: authenticated_owner):
        from backend.evacuation.service import run_view
        return [run_view(j) for j in store.list(user) if j.get("session_id")]

    @router.get("/runs/{run_id}", response_model=RunResponse, operation_id="get_simulation_status")
    def get_run(run_id: UUID, store: storage, user: authenticated_owner):
        from backend.evacuation.service import run_view
        from backend.simulation_jobs import owned_job
        return run_view(owned_job(store, run_id, user, "run"))

    @router.post("/runs/{run_id}/cancel", response_model=RunResponse, operation_id="cancel_simulation")
    def cancel_run(run_id: UUID, body: CancelRequest, store: storage, user: authenticated_owner):
        from backend.evacuation.service import run_view
        from backend.simulation_jobs import owned_job
        owned_job(store, run_id, user, "run")
        return run_view(store.cancel_run(str(run_id)))

    @router.get("/runs/{run_id}/artifacts/{artifact}", operation_id="get_simulation_artifact")
    def artifact(run_id: UUID, artifact: str, store: storage, user: authenticated_owner):
        from backend.simulation_jobs import owned_job
        return artifact_for(store, owned_job(store, run_id, user, "run"), artifact)

    @router.post("/variants", response_model=DraftResponse, operation_id="create_simulation_variant")
    def variant(body: VariantRequest, store: storage, user: authenticated_owner):
        from backend.evacuation.service import variant
        return variant(store, user, body)

    @router.get("/capabilities", response_model=CapabilityRegistry, operation_id="global_capabilities")
    def global_capabilities():
        return capabilities()

    @router.get("/runs/{run_id}/capabilities", response_model=CapabilityRegistry,
                operation_id="run_capabilities")
    def run_capabilities(run_id: UUID, store: storage, user: authenticated_owner):
        from backend.simulation_jobs import owned_job
        job = owned_job(store, run_id, user, "run")
        result = None
        if job["status"] == "completed":
            try:
                result = result_for(store, job)
            except IntegrationError as exc:
                if exc.status != 410:
                    raise
        return capabilities(result, job_status=job["status"])

    @router.post("/evidence", response_model=EvidenceResponse, operation_id="get_simulation_evidence")
    def evidence(query: EvidenceRequest, store: storage, user: authenticated_owner):
        from backend.simulation_jobs import owned_job

        # Authorize every run before reading any artifacts or disclosing a result's readiness.
        jobs = [owned_job(store, run_id, user, "run") for run_id in query.run_ids]
        return build_evidence(query, {job["id"]: result_for(store, job) for job in jobs})

    return router
