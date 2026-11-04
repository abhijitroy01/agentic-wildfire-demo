"""Traffic Lab job creation over shared SUMO admission and dispatch."""
import time
from uuid import uuid4

from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import expires_at, mutate

from backend import simulation_jobs


def create_research_job(store, owner, scenario, *, kind="research", request_id=None):
    """Queue a Traffic Lab scenario run under the session's and all visitors' active-job limits."""
    now = time.time()
    job = {"id": str(uuid4()), "owner": owner, "kind": kind, "status": "queued", "created": now,
           "updated": now, "progress": 0.0, "phase": "Queued", "scenario": scenario, "cancel": False,
           "outbox_pending": True, "expires": expires_at(now)}
    def build():
        return {"id": job["id"]}, [(job, None), *simulation_jobs.admit(store, owner, job["id"])]
    operation, request_id = f"create_{kind}", request_id or uuid4()
    try:
        receipt = mutate(store, owner, operation, request_id, scenario, build)
    except IntegrationError as exc:
        if exc.code not in ("ACTIVE_RUN_LIMIT", "SIMULATOR_BUSY"):
            raise
        # An identical request can commit after the receipt read and consume the last slot. Rejoin
        # that receipt once, preserving its body check; without a receipt keep the original refusal.
        def refused(failure=exc):
            raise failure
        receipt = mutate(store, owner, operation, request_id, scenario, refused)
    simulation_jobs.dispatch(store, receipt["id"])
    return store.get(receipt["id"])
