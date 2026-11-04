"""Shared SUMO job ownership, admission, dispatch and discovery bounds."""
import logging
import threading
import time
from functools import cache

from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import digest, record

logger = logging.getLogger(__name__)

ACTIVE = {"queued", "running", "cancelling"}


STALE_JOB_SECONDS = 3600


ALL_VISITORS = "all-visitors"


BUSY_RETRY_SECONDS = 60


def is_research_job(job):
    """True for Traffic Lab runs; v1 session runs and network preparations belong to the v1 API."""
    return not job.get("session_id") and job.get("kind", "research") == "research"


def owned_job(store, job_id, owner, kind):
    """Return the owner's job of one kind, else 404 RUN_NOT_FOUND, whether it is missing, another's or another kind.

    kind "run" is a v1 session run and "research" a Traffic Lab run; neither reads the other's jobs or a network
    preparation. The log-only reason is missing, other_owner or other_kind.
    """
    job = store.get(str(job_id))
    if not job:
        reason = "missing"
    elif job.get("owner") != owner:
        reason = "other_owner"
    elif not (job.get("session_id") if kind == "run" else is_research_job(job)):
        reason = "other_kind"
    else:
        return job
    raise IntegrationError("RUN_NOT_FOUND", "Run not found.", 404, reason=reason)


def holds_admission(job):
    """Return whether a job still holds an active-job place: queued, running or cancelling, and updated recently.

    A job that no worker has updated for STALE_JOB_SECONDS, such as a dead-lettered or orphaned one, stops counting.
    """
    return bool(job) and job.get("status") in ACTIVE and job.get("updated", 0) >= time.time() - STALE_JOB_SECONDS


def active_jobs(store, admission):
    """Return the job IDs an admission record lists that still hold an active-job place (holds_admission)."""
    return [rid for rid in (admission or {}).get("active_run_ids", []) if holds_admission(store.get(rid))]


def admit(store, owner, job_id):
    """Admit a background job under the session's and all visitors' active-job limits; return both admission writes.

    Callers commit the writes with the job, so the two admission revisions serialize concurrent admissions:
    429 ACTIVE_RUN_LIMIT past ACTIVE_JOBS_PER_SESSION, and 503 SIMULATOR_BUSY with Retry-After past
    ACTIVE_JOBS_ALL_VISITORS (runs and road-network preparations of every visitor together). Only jobs that
    still hold a place (holds_admission) count, and the new admission records list only those.
    """
    from backend import api_settings as settings
    per_session, all_visitors = settings.active_jobs_per_session(), settings.active_jobs_all_visitors()
    owner_key = digest(owner)
    mine = store.get(f"admission:{owner_key}")
    active = sorted(set(active_jobs(store, mine) + [j["id"] for j in store.list(owner)
                        if j.get("kind") != "fire_risk_prepare" and holds_admission(j)]))
    if len(active) >= per_session:
        raise IntegrationError("ACTIVE_RUN_LIMIT", f"This session has reached its limit of {per_session} active "
                               "jobs. Wait for one to finish or cancel one.", 429)
    everyone = store.get(f"admission:{ALL_VISITORS}")
    running = active_jobs(store, everyone)
    if len(running) >= all_visitors:
        raise IntegrationError("SIMULATOR_BUSY", "The simulator is busy with other visitors' runs. Try again in a "
                               "minute.", 503, retryable=True, retry_after=BUSY_RETRY_SECONDS)
    writes = []
    for saved, key, subject, ids in ((mine, owner_key, owner, active), (everyone, ALL_VISITORS, ALL_VISITORS, running)):
        previous = saved["revision"] if saved else None
        admission = record("admission", key, subject, active_run_ids=[*ids, job_id])
        admission["revision"] = (previous or 0)+1
        writes.append((admission, previous))
    return writes


def check_radius(radius_km):
    """Refuse a study area wider than MAX_STUDY_RADIUS_KM (simulation.max_study_radius_km) with 422."""
    from backend import api_settings as settings
    largest = settings.max_study_radius_km()
    if radius_km > largest:
        raise IntegrationError("RADIUS_TOO_LARGE", f"Choose a study radius of at most {largest:g} km "
                               f"({largest / 1.609344:.1f} mi).", 422)


def dispatch(store, job_id):
    """Send one committed job's outbox entry to the run queue by key; never scans the shared table.

    The DynamoDB-stream and scheduled outbox dispatcher deliver the same entry if this send fails,
    and replays whose job is no longer pending send nothing.
    """
    if not store.cloud:
        return
    try:
        store.dispatch_outbox(job_id)
    except Exception as exc:  # noqa: BLE001 — committed outbox is retried independently
        logger.warning("Outbox dispatch deferred to the scheduled dispatcher (%s)", type(exc).__name__)


@cache
def discovery_slots():
    """Return this API process's bounded slots for Traffic Lab's OSM corridor discovery."""
    from backend import api_settings as settings
    return threading.BoundedSemaphore(settings.discoveries_per_api_process())


def discover_highways(area, discoverer=None):
    """Run bounded OSM corridor discovery in a free process slot, else raise 503 DISCOVERY_BUSY."""
    from traffic_simulator.network import discover
    slots = discovery_slots()
    if not slots.acquire(blocking=False):
        raise IntegrationError("DISCOVERY_BUSY", "Road corridor discovery is busy. Retry shortly.", 503,
                               retryable=True)
    try:
        return (discoverer or discover)(area)
    finally:
        slots.release()
