"""Owned Investigation turns and retained source observations."""
import asyncio
import json
import logging
import queue
import threading
import time
from copy import deepcopy
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import commit, digest, owned, record

from backend import leases, runtime_client, trusted_context
from backend.dependencies import store_for
from backend.investigation.admission import investigation_slots
from backend.investigation.contracts import InvestigationRequest, InvestigationTurnResponse
from backend.streaming import relay, sse

logger = logging.getLogger(__name__)

INVESTIGATION_POLL_SECONDS = 1
DELIVERY_POLL_SECONDS = 0.1
TURN_SECONDS = 210
MAX_TURN_RECEIPT_BYTES = 192*1024
TURN_FAILED = {"code": "INVESTIGATION_FAILED", "message": "Investigation could not complete. Please retry."}
TURN_INTERRUPTED = {"code": "INVESTIGATION_INTERRUPTED",
                    "message": "Investigation was interrupted. Start an explicit new attempt to retry."}


def turn_id(user, request_id):
    return digest(["investigation-turn-v1", user, str(request_id)])


def turn_view(value):
    return InvestigationTurnResponse.model_validate({
        key: value[key] for key in InvestigationTurnResponse.model_fields if key in value
    }).model_dump(mode="json")


def finish_turn(store, user, identifier, attempt, *, status, snapshot=None, error=None, observations_write=None):
    """Publish the terminal receipt atomically, fenced against expiry or a replacement attempt."""
    value = owned(store, "investigation_turn", identifier, user)
    if value["attempt_id"] != attempt or value["status"] != "running":
        raise IntegrationError("INVESTIGATION_ENDED", "This investigation attempt has ended.", 409)
    revision = value["revision"]
    if status == "completed" and value["lease_until"] <= time.time():
        status, snapshot, error, observations_write = "interrupted", None, TURN_INTERRUPTED, None
    value.update(status=status, snapshot=snapshot, error=error, completed_at=time.time(), revision=revision+1)
    if len(json.dumps(value).encode()) > MAX_TURN_RECEIPT_BYTES:
        raise IntegrationError("INVESTIGATION_TOO_LARGE", "Investigation exceeded its evidence limit.", 409)
    writes = [(value, revision)]
    lease = store.get(f"investigation_lease:{digest(user)}")
    if lease and lease.get("attempt_id") == attempt:
        old_revision = lease["revision"]
        lease.update(lease_until=0, revision=old_revision+1)
        writes.append((lease, old_revision))
    if observations_write is not None:
        writes.append(observations_write)
    commit(store, writes)
    return value


def read_turn(store, user, request_id):
    identifier = turn_id(user, request_id)
    value = owned(store, "investigation_turn", identifier, user)
    if value.get("expires", 0) <= time.time():
        raise IntegrationError("NOT_FOUND", "Investigation turn expired.", 404)
    if value["status"] == "running" and value["lease_until"] <= time.time():
        try:
            value = finish_turn(store, user, identifier, value["attempt_id"],
                                status="interrupted", error=TURN_INTERRUPTED)
        except IntegrationError as exc:
            if exc.code not in {"REVISION_CONFLICT", "INVESTIGATION_ENDED"}:
                raise
            value = owned(store, "investigation_turn", identifier, user)
    return value


def replay_turn(store, user, request_id):
    """A duplicate accepted request rejoins its receipt without invoking or charging again."""
    async def stream():
        while True:
            value = read_turn(store, user, request_id)
            if value["status"] == "completed":
                yield sse({"type": "STATE_SNAPSHOT", "snapshot": value["snapshot"]})
                yield sse({"type": "RUN_FINISHED", "runId": value["attempt_id"]})
                return
            if value["status"] != "running":
                yield sse({"type": "RUN_ERROR", "message": (value["error"] or TURN_FAILED)["message"]})
                return
            yield ": heartbeat\n\n"
            await asyncio.sleep(INVESTIGATION_POLL_SECONDS)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


def retain_investigation_observations(previous, content):
    """Keep source receipts for one conversation; a new search point replaces the old area."""
    current = {key: deepcopy(content[key]) for key in ("mapData", "forecastData", "location")
               if content.get(key) is not None}
    if not current:
        return deepcopy(previous)
    old_point = previous.get("location") or (previous.get("mapData") or {}).get("summary", {}).get("location")
    new_point = current.get("location") or (current.get("mapData") or {}).get("summary", {}).get("location")
    if not old_point or not new_point or any(
            old_point.get(key) != new_point.get(key) for key in ("latitude", "longitude")):
        return current
    merged = {**deepcopy(previous), **current}
    if (current.get("mapData") and previous.get("mapData")
            and (current["mapData"].get("summary") or {}).get("totalActiveFires") is None):
        data = merged["mapData"]
        if not data.get("summary"):
            data["summary"] = deepcopy(previous["mapData"].get("summary"))
        locations = {(loc["latitude"], loc["longitude"]): deepcopy(loc)
                     for loc in previous["mapData"].get("locations", [])}
        for loc in data.get("locations", []):
            point = (loc["latitude"], loc["longitude"])
            locations[point] = {**locations.get(point, {}), **loc}
        data["locations"] = list(locations.values())
    return merged


async def investigation_turn(store, user, body):
    """Start one owner-scoped investigation turn and return its server-sent event stream.

    The Runtime gets the question, the observations retained from this conversation's completed turns and a context
    signed for the investigation purpose. The owner's investigation lease is committed before the daily agent
    budget is spent, and a failed turn sends one sanitized RUN_ERROR frame.
    """
    from backend import api_settings as settings
    identifier = turn_id(user, body.request_id)
    request_hash = digest([body.thread_id, body.question])
    previous = store.get(f"investigation_turn:{identifier}")
    if previous:
        if previous["request_hash"] != request_hash:
            raise IntegrationError("IDEMPOTENCY_CONFLICT", "Investigation request ID was reused.", 409)
        read_turn(store, user, body.request_id)
        return replay_turn(store, user, body.request_id)
    if len(json.dumps(body.context_envelope).encode()) > 128*1024:
        raise IntegrationError("CONTEXT_TOO_LARGE", "Reduce the investigation context.", 413)
    if not settings.runtime_arn():
        raise IntegrationError("AGENT_NOT_CONFIGURED", "Configure the FireGUard AgentCore runtime ARN.", 503)
    # The client supplies only a local conversation key; owner identity scopes the actual Runtime session.
    session_id = str(uuid5(NAMESPACE_URL, digest(["investigation", user, body.thread_id])))
    try:
        # The Runtime binds AgentCore Memory only to this signed actor and session and gives every
        # investigation the public toolset; unsigned forwarded fields carry no authority.
        context = await asyncio.to_thread(trusted_context.sign, {"sub": digest(user), "session_id": session_id,
                                        "allowed_operations": []}, "investigation")
    except RuntimeError:
        raise IntegrationError("AGENT_NOT_CONFIGURED", "Configure simulation service context.", 503) from None
    retained = store.get(f"investigation_observations:{session_id}")
    if retained and retained.get("expires", 0) <= time.time():
        # DynamoDB TTL deletion is asynchronous; expired evidence is not usable context.
        previous_observations = {}
    else:
        previous_observations = (retained or {}).get("observations", {})
    # Facts for follow-ups come only from completed Runtime receipts. No client envelope key reaches
    # the Runtime: there, run_id with evidence would select the planning branch.
    envelope = {"previous_observations": previous_observations}
    if len(json.dumps(envelope).encode()) > 128*1024:
        raise IntegrationError("CONTEXT_TOO_LARGE", "Reduce the investigation context.", 413)
    lease_id = digest(user)
    slots = investigation_slots()
    if not slots.acquire(blocking=False):
        raise IntegrationError("INVESTIGATION_BUSY", "Investigation capacity is busy. Retry shortly.", 503,
                               retryable=True, retry_after=30)
    attempt = str(uuid4())
    committed = False
    try:
        old_lease = store.get(f"investigation_lease:{lease_id}")
        if old_lease and old_lease.get("lease_until", 0) > time.time():
            raise IntegrationError("TURN_IN_PROGRESS", "Wait for the current investigation.", 409)
        lease = record("investigation_lease", lease_id, user, attempt_id=attempt,
                       lease_until=time.time()+TURN_SECONDS)
        lease["revision"] = (old_lease or {}).get("revision", 0)+1
        receipt = record("investigation_turn", identifier, user, request_id=str(body.request_id),
                         request_hash=request_hash, thread_id=body.thread_id, attempt_id=attempt,
                         status="running", lease_until=lease["lease_until"], snapshot=None, error=None)
        commit(store, [(receipt, None), (lease, old_lease["revision"] if old_lease else None)])
        committed = True
        runtime_client.spend_agent_request(store)
    except Exception as exc:
        try:
            if committed:
                finish_turn(store, user, identifier, attempt, status="failed",
                            error={"code": getattr(exc, "code", "INVESTIGATION_FAILED"),
                                   "message": exc.message if isinstance(exc, IntegrationError) else TURN_FAILED["message"]})
        except Exception as publication_error:  # noqa: BLE001 — expiry remains the recovery fence
            logger.warning("Investigation setup receipt unavailable: attempt=%s reason=%s",
                           attempt, type(publication_error).__name__)
        finally:
            if committed:
                leases.release(store, "investigation_lease", lease_id, user, attempt)
            slots.release()
        raise
    payload = runtime_client.agui_payload(session_id, attempt, attempt, body.question, {
        "prompt": body.question, "context_envelope": envelope, "investigation_context": context})
    channel = queue.Queue(maxsize=64)
    disconnected = threading.Event()

    def deliver(event):
        # A connected stream must preserve tool starts, argument fragments, results and terminal frames.
        # Bounded backpressure replaces eviction; disconnect/deadline only retires delivery, not paid work.
        while not disconnected.is_set() and time.time() < lease["lease_until"]:
            try:
                channel.put(event, timeout=DELIVERY_POLL_SECONDS)
                return
            except queue.Full:
                pass

    def work():
        snapshot, finished = None, False
        try:
            reserve = settings.fire_risk_limit("postprocessing_seconds")
            for event in runtime_client.runtime_events(payload, lease["lease_until"]-reserve):
                if event.get("type") == "STATE_SNAPSHOT":
                    snapshot = deepcopy(event.get("snapshot"))
                    # Only the broker can attach assessments; tool evidence is retained independently.
                    if snapshot and isinstance(snapshot.get("content"), dict):
                        snapshot["content"].pop("fireExposureData", None)
                        snapshot["content"].pop("fireExposureJobs", None)
                        snapshot["content"].pop("fireExposureError", None)
                        event = {**event, "snapshot": snapshot}
                elif event.get("type") == "RUN_FINISHED" and snapshot:
                    from backend.fire_risk_broker import enrich_answer
                    try:
                        content = enrich_answer(store, user, session_id, attempt,
                                                snapshot.get("content") or {}, lease["lease_until"]-1)
                        snapshot = {**snapshot, "content": content}
                    except Exception as exc:  # noqa: BLE001 — completed source evidence survives enrichment failure
                        from backend.fire_risk_contracts import FireRiskPreparationError
                        logger.warning("Fire exposure enrichment unavailable: reason=%s", type(exc).__name__)
                        snapshot = {**snapshot, "content": {
                            **(snapshot.get("content") or {}), "fireExposureData": [], "fireExposureJobs": [],
                            "fireExposureError": FireRiskPreparationError.unavailable().model_dump(mode="json")}}
                    observations = retain_investigation_observations(
                        previous_observations, snapshot.get("content") or {})
                    if len(json.dumps(observations).encode()) > 128*1024:
                        raise ValueError("Investigation observations exceeded the context bound")
                    receipt = record("investigation_observations", session_id, user, observations=observations)
                    receipt["revision"] = (retained or {}).get("revision", 0)+1
                    terminal = finish_turn(store, user, identifier, attempt, status="completed", snapshot=snapshot,
                                           observations_write=(receipt, retained["revision"] if retained else None))
                    if terminal["status"] != "completed":
                        raise IntegrationError("INVESTIGATION_ENDED", "This investigation attempt has ended.", 409)
                    finished = True
                    # The browser takes the final snapshot before RUN_FINISHED as this answer's authority.
                    deliver({"type": "STATE_SNAPSHOT", "snapshot": snapshot})
                deliver(event)
            if not finished:
                raise ValueError("Investigation returned no successful final snapshot")
        except Exception as exc:  # noqa: BLE001 — never expose SDK details or credentials
            # A Runtime RUN_ERROR raises in runtime_events too, so this is the turn's only error frame.
            logger.warning("Investigation turn did not complete: attempt=%s reason=%s", attempt,
                           type(exc).__name__)
            try:
                finish_turn(store, user, identifier, attempt, status="failed", error=TURN_FAILED)
            except IntegrationError as ended:
                if ended.code not in {"INVESTIGATION_ENDED", "REVISION_CONFLICT"}:
                    logger.warning("Investigation failure receipt unavailable: attempt=%s code=%s", attempt, ended.code)
            except Exception as publication_error:  # noqa: BLE001 — safe log, expiry still settles the receipt
                logger.warning("Investigation failure receipt unavailable: attempt=%s reason=%s",
                               attempt, type(publication_error).__name__)
            deliver({"type": "RUN_ERROR", "message": TURN_FAILED["message"]})
        finally:
            try:
                leases.release(store, "investigation_lease", lease_id, user, attempt)
            finally:
                slots.release()
                deliver(None)
    try:
        threading.Thread(target=work, daemon=True).start()
    except Exception:
        try:
            finish_turn(store, user, identifier, attempt, status="failed", error=TURN_FAILED)
        except Exception as publication_error:  # noqa: BLE001 — retain the original start failure; expiry fences rejoin
            logger.warning("Investigation start receipt unavailable: attempt=%s reason=%s",
                           attempt, type(publication_error).__name__)
        finally:
            try:
                leases.release(store, "investigation_lease", lease_id, user, attempt)
            finally:
                slots.release()
        raise

    async def deadline_reached():
        """Return the frame that ends a turn still running at its lease deadline."""
        return ['data: {"type":"RUN_ERROR","message":"Investigation deadline reached."}\n\n']
    async def stream():
        try:
            async for frame in relay(channel, lease["lease_until"], INVESTIGATION_POLL_SECONDS, deadline_reached):
                yield frame
        finally:
            disconnected.set()
    return StreamingResponse(stream(),
                             media_type="text/event-stream", headers={"Cache-Control": "no-store"})


def create_investigation_router(owner_dependency):
    router = APIRouter()

    @router.post("/api/v1/investigation/turns")
    async def investigation(body: InvestigationRequest, request: Request, user: str = Depends(owner_dependency)):
        """Server-authorized transport for the existing general Investigation UI."""
        return await investigation_turn(store_for(request), user, body)

    @router.get("/api/v1/investigation/turns/{request_id}", response_model=InvestigationTurnResponse)
    def get_turn(request_id: UUID, request: Request, user: str = Depends(owner_dependency)):
        return turn_view(read_turn(store_for(request), user, request_id))

    return router
