"""Traffic Lab API: free-form corridors, sources and direct scenario runs.

These routes back the Traffic Lab view in the FireGUard UI. They accept arbitrary coordinates and
start SUMO work directly. Callers are the same demo-session (or local-developer) owners as the v1
API: runs share the owner's active-job budget and the artifact reader with it. The routes are
excluded from the published simulation contract.
"""
import json
import time
from functools import cache
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from traffic_simulator import settings as simulator_settings
from traffic_simulator.corridor import rank_highways
from traffic_simulator.errors import IntegrationError, provider_failure
from traffic_simulator.examples import EXAMPLES
from traffic_simulator.feeds import fetch_sources
from traffic_simulator.models import Area, Scenario
from traffic_simulator.persistence import public
from traffic_simulator.traffic import Readings
from traffic_simulator.traffic import configured as traffic_configured

from backend import simulation_jobs
from backend.dependencies import store_for
from backend.simulation_artifacts import artifact_for, retained_artifact
from backend.traffic_lab import service

# Worker bookkeeping that persistence.public keeps; the Traffic Lab never reads it.
WORKER_FIELDS = {"cancel", "input_attempt", "artifact_attempt"}


class DiscoveryRequest(Area):
    """An explicit road discovery retains its identity across lost acknowledgements."""
    request_id: UUID


class ResearchRequest(Scenario):
    """One research submission; a deliberate new experiment has a new request ID."""
    request_id: UUID


@cache
def source_readings(store) -> Readings:
    """Return this API process's TomTom budget and reuse cache for source lookups against one job store."""
    return Readings(store)


def job_view(job: dict) -> dict:
    """Return a Traffic Lab job for the browser without owner, lease, outbox or attempt internals."""
    return {k: v for k, v in public(job).items() if k not in WORKER_FIELDS}




def create_research_router(owner_dependency) -> APIRouter:
    """Build the Traffic Lab routes; owner_dependency resolves the caller (demo_access.workspace_owner)."""
    router = APIRouter(prefix="/api/v1/research", tags=["Traffic Lab"], include_in_schema=False)
    user_id = Annotated[str, Depends(owner_dependency)]
    storage = Annotated[Any, Depends(store_for)]

    @router.get("/config")
    def config(_: user_id):
        """Report whether traffic-speed runs are available and which deployment serves the lab."""
        return {"traffic_key_configured": traffic_configured(),
                "deployment": "AWS" if simulator_settings.CLOUD else "local",
                "purpose": "Research demo; not operational evacuation or dispatch guidance"}

    @router.post("/discover", status_code=202)
    def discover_area(area: DiscoveryRequest, store: storage, user: user_id):
        """Queue regional OSM extraction in the SUMO worker; HTTP never downloads a PBF or runs Osmium."""
        simulation_jobs.check_radius(area.radius_km)
        return job_view(service.create_research_job(
            store, user, area.model_dump(exclude={"request_id"}), kind="road_discovery",
            request_id=area.request_id))

    @router.get("/discover/{job_id}")
    def discovered_area(job_id: UUID, store: storage, user: user_id):
        """Read an owned discovery's progress and its frozen result, with no provider side effects."""
        job = store.get(str(job_id))
        if (not job or job.get("kind") != "road_discovery" or job.get("owner") != user
                or job.get("expires", 0) <= time.time()):
            raise IntegrationError("NOT_FOUND", "Road discovery not found.", 404)
        view = job_view(job)
        if job["status"] == "completed":
            view["result"] = retained_artifact(store, str(job_id), "result")
        return view

    @router.post("/discover/{job_id}/cancel")
    def cancel_discovery(job_id: UUID, store: storage, user: user_id):
        discovered_area(job_id, store, user)
        store.cancel_run(str(job_id))
        return discovered_area(job_id, store, user)

    @router.get("/examples/{name}")
    def example(name: str, _: user_id):
        """Load a saved example area with its Caltrans highway geometry from package data."""
        if name not in EXAMPLES:
            raise IntegrationError("EXAMPLE_NOT_FOUND", "Unknown example.", 404)
        lat, lon, radius = EXAMPLES[name]
        geometry = json.loads((simulator_settings.PACKAGE / "data" / f"{name}-highways.json").read_text())
        return {"geometry": geometry, "highways": rank_highways(geometry, lat, lon, radius),
                "area": {"lat": lat, "lon": lon, "radius_km": radius},
                "source_mode": "Saved Caltrans geometry, retrieved September 16, 2026"}

    @router.post("/sources")
    async def sources(area: Area, store: storage, _: user_id):
        """Collect public fire, road, weather and traffic feed coverage; TomTom spends the shared daily budget."""
        simulation_jobs.check_radius(area.radius_km)
        try:
            # Optional observations do not depend on road discovery or a California district.
            return await fetch_sources(area, (), source_readings(store))
        except IntegrationError:
            raise
        except Exception as exc:  # noqa: BLE001 — route boundary: provider_failure logs and re-raises
            # Feed requests carry provider keys, so only the exception type is logged.
            raise provider_failure(exc, "Feed coverage discovery", "SOURCES_UNAVAILABLE",
                                   "SOURCES_FAILED") from None

    @router.get("/jobs")
    def jobs(store: storage, user: user_id):
        """List the caller's recent Traffic Lab runs, newest first."""
        return [job_view(j) for j in store.list(user) if simulation_jobs.is_research_job(j)]

    @router.post("/jobs", status_code=202)
    def create(scenario: ResearchRequest, store: storage, user: user_id):
        """Queue a free-form scenario run: 429 ACTIVE_RUN_LIMIT for the session, 503 SIMULATOR_BUSY for everyone."""
        simulation_jobs.check_radius(scenario.radius_km)
        if scenario.traffic_mode == "provider_speed_limits" and not traffic_configured():
            raise IntegrationError("TRAFFIC_KEY_REQUIRED",
                                   "Configure a TomTom key before choosing traffic-speed mode.", 422)
        return job_view(service.create_research_job(
            store, user, scenario.model_dump(exclude={"request_id"}), request_id=scenario.request_id))

    @router.get("/jobs/{job_id}")
    def job(job_id: UUID, store: storage, user: user_id):
        """Read one Traffic Lab run's status, phase and progress."""
        return job_view(simulation_jobs.owned_job(store, job_id, user, "research"))

    @router.post("/jobs/{job_id}/cancel")
    def cancel(job_id: UUID, store: storage, user: user_id):
        """Request cancellation; the conditional update never overwrites a run that finished meanwhile."""
        simulation_jobs.owned_job(store, job_id, user, "research")
        return job_view(store.cancel_run(str(job_id)))

    @router.get("/jobs/{job_id}/artifacts/{name}")
    def artifact(job_id: UUID, name: str, store: storage, user: user_id):
        """Read a completed run's result, network, sources or manifest with the v1 artifact errors."""
        return artifact_for(store, simulation_jobs.owned_job(store, job_id, user, "research"), name)

    return router
