"""Saved, bounded incident investigations independent of a browser connection.

Location preparation and investigation use separate Runtime sessions. A process loss
leaves an explicit interrupted receipt; retry is a user action, never a page-load effect.
"""
import json
import logging
import threading
import time
from copy import deepcopy
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, Request
from pydantic import TypeAdapter
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import commit, digest, owned, record

from backend import runtime_client, trusted_context
from backend.dependencies import InvestigationReaders, store_for
from backend.evacuation.contracts import EvidenceRequest
from backend.investigation import fire_observations
from backend.investigation.admission import investigation_slots
from backend.investigation.contracts import IncidentInvestigationRequest, IncidentInvestigationResponse
from backend.simulation_artifacts import result_for

logger = logging.getLogger(__name__)
DEADLINE_SECONDS = 420
MAX_RECEIPT_BYTES = 192 * 1024
# The context the API builds for an updated plan (verified location, run evidence, saved initial plan), a
# protocol bound (D38). Even if every byte were guarded text, 192 KiB (196,608 characters) stays under the
# guardrail's lowest per-call input limit: 200 text units (200,000 characters), the Classic-tier topic policy's.
MAX_PLANNING_CONTEXT_BYTES = 192 * 1024


def execute_reserved(store, identifier, owner, attempt, incident, context, slots, readers):
    try:
        execute(store, identifier, owner, attempt, incident, context, readers)
    finally:
        slots.release()


def task_id(incident, run_id=None):
    return str(uuid5(NAMESPACE_URL, digest([
        "incident-investigation-v1", incident["incident_id"], incident["location_revision"], run_id,
    ])))


def view(value):
    return IncidentInvestigationResponse.model_validate(
        {key: value[key] for key in IncidentInvestigationResponse.model_fields if key in value}
    ).model_dump(mode="json")


def update(store, identifier, owner, attempt, *, observation_snapshot=None, **changes):
    """Fence every event and final publication against cancellation or a replacement attempt."""
    value = owned(store, "incident_investigation", identifier, owner)
    if value["attempt_id"] != attempt or value["status"] != "running":
        raise IntegrationError("INVESTIGATION_ENDED", "This investigation attempt has ended.", 409)
    observation_writes = []
    if observation_snapshot is not None:
        captured, observation_writes = fire_observations.capture_writes(
            store, owner, identifier, attempt, observation_snapshot)
        observations = {item["observation_id"]: item for item in value.get("fire_observations", [])}
        observations.update({item["observation_id"]: item for item in captured})
        changes["fire_observations"] = list(observations.values())
    revision = value["revision"]
    value.update(changes, revision=revision + 1)
    if value["status"] != "running":
        for item in value["activity"]:
            if item["status"] in ("running", "awaiting_result"):
                item.update(status="unverified", ended_at=time.time())
    if len(json.dumps(value).encode()) > MAX_RECEIPT_BYTES:
        raise IntegrationError("INVESTIGATION_TOO_LARGE", "Investigation exceeded its evidence limit.", 409)
    writes = [(value, revision), *observation_writes]
    if value["status"] != "running":
        lease = store.get(value["owner_lease_id"])
        if lease and lease.get("attempt_id") == attempt:
            old_revision = lease["revision"]
            lease.update(lease_until=0, revision=old_revision+1)
            writes.append((lease, old_revision))
    # DynamoDB permits at most 100 operations in one transaction; source references never publish partially.
    if len(writes) > 100:
        raise IntegrationError("INVESTIGATION_TOO_LARGE", "Investigation exceeded its source receipt limit.", 409)
    commit(store, writes)
    return value


def evidence_context(store, job, readers: InvestigationReaders):
    """Read complete bounded evidence pages; the model cannot supply its own run telemetry."""
    return evidence_context_for_result(job["id"], result_for(store, job), readers)


def evidence_context_for_result(run_id, result, readers: InvestigationReaders):
    """Build the same bounded planning context from an already-authorized retained artifact."""
    pages, seen_facts = [], {}
    for selection in ("outcomes", "crew", "comparison", "sources", "assumptions"):
        cursor = None
        for _ in range(12):
            query = TypeAdapter(EvidenceRequest).validate_python(
                {"view": selection, "run_ids": [run_id], "cursor": cursor})
            response = readers.build_evidence(query, {run_id: result})
            page = response.model_dump(mode="json", exclude_defaults=True)
            # Pagination is consumed here. Share identical facts across views by
            # their stable identity, retaining every measurement, null and reason.
            shared, unique = [], []
            for fact in page["facts"]:
                key = fact["fact_id"]
                if key in seen_facts and seen_facts[key] == fact:
                    shared.append(key)
                else:
                    seen_facts[key] = fact
                    unique.append(fact)
            page["facts"] = unique
            page["shared_fact_ids"] = shared
            page["pagination"] = response.pagination.model_dump(mode="json")
            pages.append(page)
            cursor = page["pagination"]["next_cursor"]
            if not cursor:
                break
        if cursor:
            raise IntegrationError("EVIDENCE_LIMIT", "The plan requires a narrower evidence scope.", 409)
    if len(json.dumps(pages, separators=(",", ":")).encode()) > MAX_PLANNING_CONTEXT_BYTES:
        raise IntegrationError("EVIDENCE_LIMIT", "The plan requires a narrower evidence scope.", 409)
    return {"run_id": run_id, "input_bundle_id": result.get("input_bundle_id"),
            "input_bundle_sha256": result.get("input_bundle_sha256"), "evidence": pages}


def initial_plan_context(initial):
    """Link the full saved receipt; keep prior observations and identities without repeated retrieval text."""
    snapshot = deepcopy(initial["snapshot"]) if initial and initial["status"] == "completed" else None
    context = {"initial_plan": snapshot}
    if snapshot:
        # Fresh guidance retrieves its own passages. Keep the original receipt intact.
        citations = snapshot["content"].get("knowledgeBaseCitations")
        if citations:
            snapshot["content"]["knowledgeBaseCitations"] = [
                {key: value for key, value in citation.items() if key != "excerpt"} for citation in citations
            ]
        context["initial_plan_task_id"] = initial["task_id"]
    return context


def execute(store, identifier, owner, attempt, incident, context, readers):
    """Consume real AG-UI tool events and require final snapshot plus successful termination."""
    try:
        current = owned(store, "incident_investigation", identifier, owner)
        session_id = str(uuid5(NAMESPACE_URL, digest([owner, identifier, attempt])))
        signed = trusted_context.sign(
            {"sub": digest(owner), "session_id": session_id, "allowed_operations": []}, "investigation")
        point = incident["location"]
        if current["stage"] == "initial":
            question = (
                f"At verified scenario coordinates "
                f"({point['lat']}, {point['lon']}), {point['matched_address']}. "
                "What nearby reported fires within 50 miles, current point weather and 2-day "
                "forecast are available? How should published guidance inform resident evacuation, "
                "crew access, staging, communication and the remaining information gaps? "
                "This prerecorded-radio planning scenario is not confirmation of a fire at the "
                "scenario point. Road preparation runs separately; no simulation results are available yet."
            )
        else:
            question = (
                "What do the completed planning simulations show about resident exits, unresolved "
                "demand, exposure, crew waiting and crews that have not arrived across every policy? "
                "How does that evidence change the saved initial assessment under published guidance, "
                "and which assumptions and information gaps still need review?"
            )
        payload = runtime_client.agui_payload(session_id, attempt, attempt, question, {
            "prompt": question, "context_envelope": context, "investigation_context": signed})
        snapshot, finished = None, False
        # runtime_events enforces the lease deadline and byte budget, and raises on RUN_ERROR or an unsuccessful
        # RUN_FINISHED, so a failed run never publishes a plan.
        for event in runtime_client.runtime_events(payload, current["lease_until"]):
            current = owned(store, "incident_investigation", identifier, owner)
            if current["attempt_id"] != attempt or current["status"] != "running":
                return
            kind = event.get("type")
            activity = current["activity"]
            call_id = str(event.get("toolCallId", event.get("tool_call_id", "")))[:200]
            if kind == "TOOL_CALL_START" and call_id and len(activity) < 64:
                if not any(item["tool_call_id"] == call_id for item in activity):
                    activity.append({"tool_call_id": call_id,
                                     "name": str(event.get("toolCallName", event.get("tool_call_name", "Agent tool")))[:200],
                                     "status": "running", "started_at": time.time(), "ended_at": None})
                update(store, identifier, owner, attempt, activity=activity)
            elif kind in ("TOOL_CALL_END", "TOOL_CALL_RESULT"):
                for item in activity:
                    if item["tool_call_id"] == call_id:
                        item["status"] = "awaiting_result" if kind == "TOOL_CALL_END" else "result_received"
                        if kind == "TOOL_CALL_RESULT":
                            item["ended_at"] = time.time()
                update(store, identifier, owner, attempt, activity=activity)
            elif kind == "STATE_SNAPSHOT":
                candidate = event.get("snapshot")
                if (isinstance(candidate, dict) and candidate.get("type") == "structuredResponse"
                        and isinstance(candidate.get("content"), dict)):
                    changes = {}
                    if (isinstance(candidate["content"].get("response"), str)
                            and candidate["content"]["response"].strip()):
                        snapshot = candidate
                        partial = deepcopy(candidate)
                        partial["content"]["response"] = (
                            "Source observations received; the planning assessment is still running.")
                        partial["content"]["knowledgeBaseCitations"] = None
                        changes["snapshot"] = partial
                    update(store, identifier, owner, attempt, observation_snapshot=candidate, **changes)
            elif kind == "RUN_FINISHED":
                finished = True
        if not snapshot or not finished:
            raise RuntimeError("Missing successful final response")
        for item in current["activity"]:
            if item["status"] in ("running", "awaiting_result"):
                item.update(status="unverified", ended_at=time.time())
        if not snapshot["content"].get("knowledgeBaseCitations"):
            snapshot["content"]["response"] = (
                "The guidance agent could not produce a validated, cited planning response. "
                "Separately retrieved fire and weather observations remain available where supplied."
            )
            update(store, identifier, owner, attempt, status="failed", snapshot=snapshot,
                   completed_at=time.time(), activity=current["activity"],
                   error="Guidance is unavailable. Retry this step; saved road and simulation work is unaffected.")
            return
        if current["stage"] == "simulation":
            summary = readers.planning_summary(context["evidence"])
            snapshot["content"]["response"] = summary + "\n\n" + snapshot["content"]["response"]
        update(store, identifier, owner, attempt, status="completed", snapshot=snapshot,
               completed_at=time.time(), activity=current["activity"])
    except IntegrationError as exc:
        if exc.code != "INVESTIGATION_ENDED":
            logger.warning("Incident investigation did not complete: task=%s code=%s", identifier, exc.code)
            fail(store, identifier, owner, attempt)
    except Exception as exc:  # noqa: BLE001 — never publish provider details or signed context
        logger.warning("Incident investigation did not complete: task=%s reason=%s", identifier, type(exc).__name__)
        fail(store, identifier, owner, attempt)


def fail(store, identifier, owner, attempt):
    try:
        value = owned(store, "incident_investigation", identifier, owner)
        for item in value["activity"]:
            if item["status"] in ("running", "awaiting_result"):
                item.update(status="error", ended_at=time.time())
        if value.get("snapshot"):
            value["snapshot"]["content"]["response"] = (
                "Retrieved observations remain available; the planning assessment did not complete.")
        update(store, identifier, owner, attempt, status="failed", activity=value["activity"],
               snapshot=value.get("snapshot"), completed_at=time.time(),
               error="Investigation could not complete. Saved simulation work is unaffected. Retry this step.")
    except IntegrationError:
        pass


def read(store, incident, owner, run_id=None):
    value = store.get(f"incident_investigation:{task_id(incident, run_id)}")
    if not value:
        return None
    if value.get("subject") != owner:
        raise IntegrationError("NOT_FOUND", "Investigation not found.", 404)
    if value["status"] == "running" and value["lease_until"] < time.time():
        value = update(store, value["task_id"], owner, value["attempt_id"], status="interrupted",
                       completed_at=time.time(), error="Investigation was interrupted. Retry this step to continue.")
    return view(value)


def migrate_fire_observations(store, identifier, owner):
    """An explicit rejoin can seal pre-release source snapshots without another Runtime request."""
    value = owned(store, "incident_investigation", identifier, owner)
    if value.get("expires", 0) <= time.time():
        raise IntegrationError("NOT_FOUND", "Investigation source observations expired.", 404)
    if value.get("fire_observations") or not value.get("snapshot"):
        return view(value)
    attempt = value.get("attempt_id")
    if not isinstance(attempt, str) or not attempt:
        raise IntegrationError("SOURCE_INVALID", "The saved investigation has no source attempt identity.", 409)
    captured, writes = fire_observations.capture_writes(
        store, owner, identifier, attempt, value["snapshot"])
    if not captured:
        return view(value)
    revision = value["revision"]
    value.update(fire_observations=captured, revision=revision+1)
    if len(json.dumps(value).encode()) > MAX_RECEIPT_BYTES or len(writes)+1 > 100:
        raise IntegrationError("INVESTIGATION_TOO_LARGE", "Investigation exceeded its source receipt limit.", 409)
    commit(store, [(value, revision), *writes])
    return view(value)


def start(store, owner, incident, body, readers: InvestigationReaders):
    identifier, run_id = task_id(incident, str(body.run_id) if body.run_id else None), None
    previous = read(store, incident, owner, str(body.run_id) if body.run_id else None)
    if previous and previous["status"] != "running" and not previous["fire_observations"]:
        previous = migrate_fire_observations(store, identifier, owner)
    if previous and (previous["status"] in ("running", "completed") or not body.retry):
        return previous
    context = {"verified_location": incident["location"]}
    if body.run_id:
        job = readers.owned_job(store, str(body.run_id), owner, "run")
        if job.get("incident_id") != incident["incident_id"] or job.get("location_revision") != incident["location_revision"]:
            raise IntegrationError("CONTEXT_MISMATCH", "Choose a run at this incident revision.", 409)
        run_id = job["id"]
        context.update(evidence_context(store, job, readers))
        initial = read(store, incident, owner)
        if initial and initial["status"] == "running":
            raise IntegrationError("INITIAL_PLAN_PENDING", "The initial investigation is still running.", 409)
        context.update(initial_plan_context(initial))
    if len(json.dumps(context, separators=(",", ":")).encode()) > MAX_PLANNING_CONTEXT_BYTES:
        raise IntegrationError("EVIDENCE_LIMIT", "The plan requires a narrower evidence scope.", 409)
    old = store.get(f"incident_investigation:{identifier}")
    attempt = str(uuid4())
    lease_id = digest([owner, "simulation" if run_id else "initial"])
    lease_key = f"incident_investigation_lease:{lease_id}"
    lease = store.get(lease_key)
    if lease and lease.get("lease_until", 0) > time.time():
        raise IntegrationError("INVESTIGATION_IN_PROGRESS", "Wait for the current incident investigation.", 409)
    reservation = record("incident_investigation_lease", lease_id, owner,
                         attempt_id=attempt, lease_until=time.time()+DEADLINE_SECONDS)
    reservation["revision"] = lease["revision"]+1 if lease else 1
    value = record("incident_investigation", identifier, owner, task_id=identifier,
                   incident_id=incident["incident_id"], location_revision=incident["location_revision"],
                   run_id=run_id, stage="simulation" if run_id else "initial", status="running",
                   activity=[], snapshot=None, fire_observations=[], error=None,
                   attempt_id=attempt, owner_lease_id=lease_key,
                   lease_until=reservation["lease_until"],
                   input_bundle_id=context.get("input_bundle_id"), input_bundle_sha256=context.get("input_bundle_sha256"))
    value["revision"] = old["revision"]+1 if old else 1
    slots = investigation_slots()
    if not slots.acquire(blocking=False):
        raise IntegrationError("INVESTIGATION_BUSY", "Investigation capacity is busy. Retry shortly.", 503,
                               retryable=True, retry_after=30)
    committed = False
    try:
        commit(store, [(value, old["revision"] if old else None), (reservation, lease["revision"] if lease else None)])
        committed = True
        runtime_client.spend_agent_request(store)
        threading.Thread(target=execute_reserved,
                         args=(store, identifier, owner, attempt, incident, context, slots, readers), daemon=True).start()
    except Exception:
        if committed:
            fail(store, identifier, owner, attempt)
        slots.release()
        raise
    return view(value)


def create_incident_investigation_router(owner_dependency, readers: InvestigationReaders):
    router = APIRouter()

    @router.get("/api/v1/incidents/{incident_id}/investigation",
                response_model=IncidentInvestigationResponse | None)
    def get_investigation(incident_id: UUID, request: Request, run_id: UUID | None = None,
                          user: str = Depends(owner_dependency)):
        store = store_for(request)
        incident = owned(store, "incident", incident_id, user)
        if run_id:
            job = readers.owned_job(store, str(run_id), user, "run")
            if job.get("incident_id") != str(incident_id):
                raise IntegrationError("CONTEXT_MISMATCH", "Choose a run at this incident.", 409)
        return read(store, incident, user, str(run_id) if run_id else None)

    @router.post("/api/v1/incidents/{incident_id}/investigation", status_code=202,
                 response_model=IncidentInvestigationResponse)
    def start_investigation(incident_id: UUID, body: IncidentInvestigationRequest, request: Request,
                            user: str = Depends(owner_dependency)):
        store = store_for(request)
        incident = owned(store, "incident", incident_id, user)
        readers.context(store, user, incident["session_id"], incident_id, incident["location_revision"])
        return start(store, user, incident, body, readers)

    @router.post("/api/v1/incidents/{incident_id}/investigation/cancel",
                 response_model=IncidentInvestigationResponse)
    def cancel_investigation(incident_id: UUID, body: IncidentInvestigationRequest, request: Request,
                             user: str = Depends(owner_dependency)):
        store = store_for(request)
        incident = owned(store, "incident", incident_id, user)
        identifier = task_id(incident, str(body.run_id) if body.run_id else None)
        value = owned(store, "incident_investigation", identifier, user)
        if value["status"] == "running":
            value = update(store, identifier, user, value["attempt_id"], status="cancelled",
                           completed_at=time.time())
        return view(value)
    return router
