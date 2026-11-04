"""Owned evacuation agent turns and signed Gateway simulation operations."""
import asyncio
import json
import logging
import queue
import threading
import time
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import TypeAdapter
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import commit, digest, owned, record

from backend import leases, runtime_client, security_events, simulation_jobs, trusted_context
from backend.api_contracts import CAPABILITY_VERSION
from backend.dependencies import store_for
from backend.evacuation import service
from backend.evacuation.contracts import (
    EvidenceRequest,
    PrepareRequest,
    SimulationOutput,
    StatusRequest,
    SubmitRequest,
    TurnRequest,
    VariantRequest,
)
from backend.evacuation.evidence import build_evidence
from backend.simulation_artifacts import result_for
from backend.streaming import relay, sse

logger = logging.getLogger(__name__)

MAX_EFFECT_BYTES = 256 * 1024


MAX_TOOL_CALLS_PER_TURN = 12


TURN_POLL_SECONDS = 2


def execute_tool(store, operation, arguments, claims):
    """Run one Gateway tool call for a signed agent turn, checking its allowed operations and lease."""
    if operation not in claims["allowed_operations"]:
        raise IntegrationError("OPERATION_NOT_AUTHORIZED", "This turn does not authorize that operation.", 403)
    owner = claims["sub"]
    service.context(store, owner, claims["session_id"], claims["incident_id"], claims["location_revision"])
    turn = owned(store, "turn", claims["turn_id"], owner)
    if (turn.get("status") != "running" or turn["lease_until"] < time.time()
            or claims.get("attempt_id") != turn.get("attempt_id")):
        raise IntegrationError("TURN_EXPIRED", "The reasoning turn has ended.", 409)
    effects = list(turn.get("effects", []))
    if len(effects) >= MAX_TOOL_CALLS_PER_TURN:
        raise IntegrationError("TOOL_BUDGET_EXCEEDED", "This turn has used its tool budget.", 409)
    arguments = dict(arguments)
    arguments["request_id"] = str(uuid5(UUID(claims["turn_id"]), operation + digest(arguments)))

    def bind_run(run_id):
        job = simulation_jobs.owned_job(store, run_id, owner, "run")
        if (job["session_id"] != claims["session_id"] or job["incident_id"] != claims["incident_id"]
                or job["location_revision"] != claims["location_revision"]):
            raise IntegrationError("CONTEXT_MISMATCH", "Run does not belong to the active location.", 403)
        return job

    if operation == "prepare_simulation":
        arguments.update(session_id=claims["session_id"], incident_id=claims["incident_id"],
                         location_revision=claims["location_revision"])
        result = service.prepare(store, owner, PrepareRequest.model_validate(arguments))
    elif operation == "submit_simulation":
        # User selected the draft in a separate explicit submission action.
        if arguments.get("draft_id") != claims.get("draft_id"):
            raise IntegrationError("DRAFT_NOT_AUTHORIZED", "Submit the draft selected by the user.", 403)
        if any(e["operation"] == operation for e in effects):
            return next(e["result"] for e in effects if e["operation"] == operation)
        arguments["capability_version"] = CAPABILITY_VERSION
        result = service.submit(store, owner, SubmitRequest.model_validate(arguments))
    elif operation == "get_simulation_status":
        arguments.pop("request_id")
        query = StatusRequest.model_validate(arguments)
        result = service.run_view(bind_run(str(query.run_id)))
    elif operation == "get_simulation_evidence":
        arguments.pop("request_id")
        query = TypeAdapter(EvidenceRequest).validate_python(arguments)
        jobs = [bind_run(run_id) for run_id in query.run_ids]
        result = build_evidence(query, {j["id"]: result_for(store, j) for j in jobs}).model_dump(mode="json")
    elif operation == "create_simulation_variant":
        bind_run(arguments["parent_run_id"])
        arguments["capability_version"] = CAPABILITY_VERSION
        result = service.variant(store, owner, VariantRequest.model_validate(arguments))
    else:
        raise IntegrationError("UNKNOWN_TOOL", "Unknown simulation operation.", 404)
    # Save authoritative receipts/evidence before returning them to the model.
    # Tool calls are serialized by the specialist, so one revision fences late results.
    previous = turn["revision"]
    selection = {k: v for k, v in arguments.items() if k in {"view", "policy_ids", "crew_id", "time_scope"}}
    effects.append({"operation": operation, "result": result, "selection": selection})
    if (operation == "get_simulation_evidence"
            and len(json.dumps(effects, ensure_ascii=False).encode()) > MAX_EFFECT_BYTES):
        raise IntegrationError("TOOL_BUDGET_EXCEEDED",
                               "This answer has reached its evidence limit. Ask a narrower follow-up.", 409)
    turn.update(revision=previous+1, effects=effects)
    commit(store, [(turn, previous)])
    return result


def complete_turn(store, owner, turn_id, answer, *, error=False, attempt_id=None):
    """Store an agent turn's final answer or error once, for the attempt that still owns it."""
    turn = owned(store, "turn", turn_id, owner)
    if attempt_id is not None and turn.get("attempt_id") != attempt_id:
        raise IntegrationError("TURN_EXPIRED", "A newer attempt owns this turn.", 409)
    if turn.get("output"):
        return turn["output"]
    request = turn["request"]
    effects = turn.get("effects", [])
    run = next((e["result"] for e in reversed(effects) if e["operation"] == "submit_simulation"), None)
    draft = next((e["result"] for e in reversed(effects)
                  if e["operation"] in ("prepare_simulation", "create_simulation_variant")), None)
    evidence = [e["result"] for e in effects if e["operation"] == "get_simulation_evidence"]
    actions = [{"type": "show_variant_draft", "draft_id": draft["draft_id"]}] if draft else []
    selected = next((e for e in reversed(effects) if e["operation"] == "get_simulation_evidence"), None)
    if selected:
        view = selected["result"]["view"]
        actions.append({"type": "open_panel", "panel": {
            "crew": "crews", "sources": "evidence", "assumptions": "evidence",
        }.get(view, "overview")})
        selection = selected.get("selection", {})
        if len(selection.get("policy_ids", [])) == 1:
            actions.append({"type": "select_policy", "policy_id": selection["policy_ids"][0]})
        if view == "replay":
            samples = [f.get("time_scope", {}).get("effective_time_s") for f in selected["result"]["facts"]
                       if f.get("time_scope")]
            samples = [value for value in samples if value is not None]
            if samples:
                actions.append({"type": "seek_time", "time_s": min(samples)})
        if selection.get("crew_id") and any(
            f.get("crew_id") == selection["crew_id"] for f in selected["result"]["facts"]
        ):
            actions.append({"type": "focus_crew", "crew_id": selection["crew_id"]})
    output = SimulationOutput(
        session_id=request["session_id"], incident_id=request["incident_id"],
        location_revision=request["location_revision"], turn_id=turn_id,
        status="submitted" if run else "error" if error else "needs_input" if draft else "answer",
        question=request["question"], answer=answer, run=run, draft=draft, evidence=evidence, ui_actions=actions,
    ).model_dump(mode="json")
    revision = turn["revision"]
    # Evidence already lives in output. Keep the audit order/selection and an index
    # instead of storing a second copy of every page in the same DynamoDB item.
    evidence_index = 0
    completed_effects = []
    for effect in effects:
        if effect["operation"] == "get_simulation_evidence":
            completed_effects.append({k: v for k, v in effect.items() if k != "result"} |
                                     {"output_evidence_index": evidence_index})
            evidence_index += 1
        else:
            completed_effects.append(effect)
    turn.update(revision=revision+1, status="completed", output=output, effects=completed_effects)
    commit(store, [(turn, revision)])
    return output


async def agent_turn(store, user, body):
    """Start or replay an owned simulation agent turn and return its server-sent event stream.

    The Runtime gets a context signed for simulation that names the turn's allowed tools. The turn record is the
    question's lease: it is committed with the session's turn list before the daily agent budget is spent.
    """
    session, _ = service.context(store, user, body.session_id, body.incident_id, body.location_revision)
    if body.run_id:
        job = simulation_jobs.owned_job(store, body.run_id, user, "run")
        if job["incident_id"] != str(body.incident_id):
            raise IntegrationError("CONTEXT_MISMATCH", "Choose a run for this incident.", 409)
    if body.intent == "analyze":
        if not body.run_id:
            raise IntegrationError("RUN_REQUIRED", "Select a completed run for analysis.")
        result = result_for(store, job)
        body = body.model_copy(update={
            "request_id": uuid5(NAMESPACE_URL, digest([user, str(body.run_id), digest(result), "analysis-v1"])),
            "question": "Compare resident evacuation and crew access across all policies, including unresolved demand, waiting, exposure and nonarrivals. Cite the retained evidence.",
            "policy_id": "baseline", "replay_time_s": 0.0, "draft_id": None})
    draft = None
    if body.draft_id:
        draft = service.draft_view(store, owned(store, "draft", body.draft_id, user))
        if draft["incident_id"] != str(body.incident_id):
            raise IntegrationError("CONTEXT_MISMATCH", "Choose a draft for this incident.", 409)
    if body.intent == "submit":
        if not draft:
            raise IntegrationError("DRAFT_REQUIRED", "Select the validated draft to run.", 409)
        if draft["status"] != "ready":
            raise IntegrationError("NETWORK_NOT_READY", "Wait for road-network validation before running.", 409)
    turn_id = str(uuid5(NAMESPACE_URL, digest([user, str(body.request_id)])))
    previous = store.get(f"turn:{turn_id}")
    body_json = body.model_dump(mode="json")
    if previous and previous["request"] != body_json:
        raise IntegrationError("IDEMPOTENCY_CONFLICT", "Request ID was reused with a different turn.", 409)
    if previous and previous.get("output"):
        async def replay():
            yield sse({"type": "STATE_SNAPSHOT", "snapshot": universal(previous["output"])})
            yield 'data: {"type":"RUN_FINISHED"}\n\n'
        return StreamingResponse(replay(), media_type="text/event-stream")
    if previous and previous.get("lease_until", 0) > time.time():
        raise IntegrationError("TURN_IN_PROGRESS", "This question is already being processed.", 409)
    # One active turn per session ensures ordered tool effects and bounded model cost.
    for tid in session.get("turn_ids", [])[-2:]:
        active = store.get(f"turn:{tid}")
        if active and active["status"] == "running" and active["lease_until"] > time.time():
            raise IntegrationError("TURN_IN_PROGRESS", "Wait for the current answer.", 409)
    attempt_id = str(uuid4())
    turn = record("turn", turn_id, user, request=body_json, effects=(previous or {}).get("effects", []),
                  status="running", lease_until=time.time()+210, attempt_id=attempt_id)
    turn["revision"] = (previous or {}).get("revision", 0)+1
    revision = session["revision"]
    session.update(revision=revision+1, turn_ids=list(dict.fromkeys([*session.get("turn_ids", []), turn_id]))[-100:])
    commit(store, [(turn, previous["revision"] if previous else None), (session, revision)])
    # A concurrent turn that lost this commit spent nothing. A refused spend ends the turn as failed with the
    # refusal's code and releases its lease, so the turn routes report it ended and the same question can be asked
    # again.
    leases.spend_or_release(
        store, "turn", turn_id, user, attempt_id, runtime_client.spend_agent_request,
        ended=lambda exc: {"status": "failed",
                           **({"error_code": exc.code} if isinstance(exc, IntegrationError) else {})})
    allowed = ["get_simulation_status", "get_simulation_evidence", "create_simulation_variant"]
    if body.intent == "prepare":
        allowed.append("prepare_simulation")
    if body.intent == "submit":
        allowed.append("submit_simulation")
    claims = {"sub": user, "session_id": str(body.session_id), "incident_id": str(body.incident_id),
              "location_revision": body.location_revision, "turn_id": turn_id, "attempt_id": attempt_id,
              "allowed_operations": allowed, "draft_id": str(body.draft_id) if body.draft_id else None}
    try:
        token = await asyncio.to_thread(trusted_context.sign, claims, "simulation")
    except RuntimeError:
        complete_turn(store, user, turn_id, "Simulation service context is not configured.",
                      error=True, attempt_id=attempt_id)
        raise IntegrationError("AGENT_NOT_CONFIGURED", "Configure simulation service context.", 503) from None
    # The Runtime takes the actor, session and allowed tools only from the signed context; the selection
    # is untrusted data for the model.
    payload = runtime_client.agui_payload(str(body.session_id), turn_id, str(body.request_id), body.question, {
        "simulation_context": token, "simulation_selection": {
            **body_json, "draft_revision": draft["draft_revision"] if draft else None, "draft": draft}})
    channel = queue.Queue()

    def work():
        answer = None
        try:
            failed = False
            try:
                for event in runtime_client.runtime_events(payload, turn["lease_until"]):
                    if event.get("type") == "STATE_SNAPSHOT":
                        snapshot = event.get("snapshot", {})
                        answer = snapshot.get("content", {}).get("response")
                    elif event.get("type") in ("TOOL_CALL_START", "TOOL_CALL_END", "TOOL_CALL_RESULT"):
                        # Emit activity names/status only. Gateway arguments contain service context.
                        channel.put({"type": "CUSTOM", "name": "simulation_activity",
                                     "value": {"event": event["type"], "tool_name": event.get("toolCallName"),
                                               "tool_call_id": event.get("toolCallId")}})
            except Exception as exc:  # noqa: BLE001 — recover saved effects after remote failures
                logger.warning("Agent turn %s lost its Runtime stream: reason=%s", turn_id, type(exc).__name__)
                failed = True
                answer = "The agent connection failed. Saved run receipts remain available; retry the question."
            output = complete_turn(store, user, turn_id, answer or "The agent returned no answer.",
                                   error=failed or not bool(answer), attempt_id=attempt_id)
            channel.put({"type": "STATE_SNAPSHOT", "snapshot": universal(output)})
            channel.put({"type": "RUN_FINISHED"})
        except Exception as exc:  # noqa: BLE001 — storage/attempt failures must close the browser stream
            logger.warning("Agent turn %s answer was not saved: reason=%s", turn_id, type(exc).__name__)
            channel.put({"type": "RUN_ERROR", "message": "Could not save this answer. Refresh to recover saved receipts."})
        finally:
            channel.put(None)
    threading.Thread(target=work, daemon=True).start()
    receipts_seen = 0

    async def receipts():
        """Return one frame for each tool result the turn has saved since the previous poll."""
        nonlocal receipts_seen
        saved = await asyncio.to_thread(owned, store, "turn", turn_id, user)
        effects = saved.get("effects", [])
        frames = [sse({"type": "CUSTOM", "name": "simulation_tool_result",
                       "value": {"tool_name": effect["operation"], "status": "completed"}})
                  for effect in effects[receipts_seen:]]
        receipts_seen = len(effects)
        return frames

    async def deadline_answer():
        """Save the deadline answer for a turn still running at its lease deadline and return its frames."""
        try:
            output = await asyncio.to_thread(
                complete_turn, store, user, turn_id,
                "The reasoning deadline was reached. Saved run receipts remain available.",
                error=True, attempt_id=attempt_id)
        except Exception as exc:  # noqa: BLE001 — no endless heartbeat on a storage outage
            logger.warning("Agent turn %s deadline answer was not saved: reason=%s", turn_id, type(exc).__name__)
            return ['data: {"type":"RUN_ERROR","message":"Refresh to recover saved receipts."}\n\n']
        return [sse({"type": "STATE_SNAPSHOT", "snapshot": universal(output)}), 'data: {"type":"RUN_FINISHED"}\n\n']

    async def stream():
        yield 'data: {"type":"RUN_STARTED"}\n\n'
        async for frame in relay(channel, turn["lease_until"], TURN_POLL_SECONDS, deadline_answer, receipts):
            yield frame
    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def turn_view(turn, turn_id):
    """An owned agent turn as its routes report it: the saved output, else its status and any refusal code."""
    if "output" in turn:
        return turn["output"]
    return {"turn_id": str(turn_id), "status": turn["status"],
            **({"error_code": turn["error_code"]} if turn.get("error_code") else {})}


def create_broker_router(owner_dependency):
    """Build the Investigation and simulation agent turn routes for the resolved visitor."""
    router = APIRouter()

    @router.post("/api/internal/v1/simulations/{operation}", include_in_schema=False)
    async def internal(operation: str, request: Request):
        """Run one Gateway adapter tool call under its signed turn context; each completed call is audited."""
        try:
            # Only a context signed for the simulation purpose authorizes a tool call.
            claims = await asyncio.to_thread(trusted_context.verify,
                                             request.headers.get("x-simulation-context", ""), "simulation")
        except (ValueError, trusted_context.jwt.PyJWTError) as exc:
            # The security event log keeps the failure type (such as ExpiredSignatureError), never the token.
            raise IntegrationError("INVALID_SERVICE_CONTEXT", "Invalid service context.", 401,
                                   reason=type(exc).__name__) from None
        except RuntimeError:
            # The API cannot read its signing secret: a server fault, not the caller's.
            raise IntegrationError("AGENT_NOT_CONFIGURED", "Configure simulation service context.", 503) from None
        security_events.bind_owner(request, claims["sub"])
        body = await request.json()
        result = await asyncio.to_thread(execute_tool, store_for(request), operation, body, claims)
        # The model acted with the visitor's authority, so each completed operation is a security event.
        security_events.allowed("agent_tool_call", request, operation=operation, turn_id=claims["turn_id"])
        return result

    @router.get("/api/v1/agent/turns/{turn_id}", response_model=SimulationOutput | dict)
    def get_turn(turn_id: UUID, request: Request, user: str = Depends(owner_dependency)):
        """Read an owned simulation agent turn: its final output, or its status while it runs."""
        return turn_view(owned(store_for(request), "turn", turn_id, user), turn_id)

    @router.get("/api/v1/sessions/{session_id}/turns")
    def list_turns(session_id: UUID, request: Request, user: str = Depends(owner_dependency)):
        """List the simulation agent turns of an owned session, in the order they were started."""
        session = owned(store_for(request), "session", session_id, user)
        return [turn_view(owned(store_for(request), "turn", tid, user), tid)
                for tid in session.get("turn_ids", [])]

    @router.post("/api/v1/agent/turns")
    async def create_turn(body: TurnRequest, request: Request, user: str = Depends(owner_dependency)):
        """Start or replay an owned simulation agent turn; the Runtime gets a context signed for simulation."""
        return await agent_turn(store_for(request), user, body)
    return router


def universal(output):
    return {"type": "structuredResponse", "content": {"response": output["answer"], "simulationData": output}}
