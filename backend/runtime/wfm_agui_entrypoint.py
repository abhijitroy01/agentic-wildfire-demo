"""
AG-UI entrypoint for the Wildfire Data Fabric supervisor.

Implements the AG-UI protocol contract for AgentCore Runtime:
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-agui-protocol-contract.html

Endpoints
---------
- GET  /ping        — healthcheck (returns {"status":"Healthy"} per AWS contract)
- POST /invocations — accepts AG-UI RunAgentInput, streams SSE events back

Streaming protocol
------------------
Events emitted (subset of AG-UI spec):
  RUN_STARTED → TOOL_CALL_* / TEXT_MESSAGE_* → STATE_SNAPSHOT → RUN_FINISHED

The final STATE_SNAPSHOT carries the validated `UniversalResponse` Pydantic
model so the React UI can fall back to a single-shot read if it ignores the
streaming events.

Memory & guardrails
-------------------
- AgentCoreMemorySessionManager is bound per AG-UI thread via
  `StrandsAgentConfig.session_manager_provider` (one fresh manager per
  thread_id). This is the AWS-recommended pattern. The actor and session come
  only from the broker's signed service context; unsigned requests get no memory.
- Every investigation uses the public supervisor toolset; there is no signed-in
  mode. Report and simulation turns require their own signed contexts.
- Every agent model (supervisor, specialists, simulation and report agents)
  uses the published Bedrock guardrail; see utils.model_config.GUARDRAIL.

Errors and logs
---------------
Clients receive stable RUN_ERROR text per code (RUN_ERROR_MESSAGES), never
exception text. Logs carry ids, sizes, safe failure codes and
error types, never the prompt, context envelope or model output.

Serving
-------
A simulation turn starts and stops its MCP client in a worker thread, so /ping
and other requests are never stalled behind it. Local runs bind 127.0.0.1
unless AGENT_HOST is set (bind_host); the Runtime image binds every interface.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from copy import deepcopy

# CRITICAL: env_loader must run before anything that reads env at import time.
import utils.env_loader  # noqa: F401  (side-effect import)
import uvicorn
from ag_ui.core import (
    CustomEvent,
    EventType,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    StateSnapshotEvent,
    UserMessage,
)
from ag_ui.encoder import EventEncoder
from ag_ui_strands import StrandsAgent, StrandsAgentConfig
from agents.supervisor_agent import create_supervisor_agent, select_investigation_topics
from anyio import CancelScope, to_thread
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from schemas.generated_models import UniversalResponse
from utils.aws_clients import deployment_region
from utils.input_validator import sanitize_input, validate_input_format
from utils.limits import INVESTIGATION_TOOL_CALLS, MEMORY_MESSAGES
from utils.log_safety import error_summary
from utils.model_config import settings
from utils.structured_response import (
    AnswerResponse,
    PublicAnswerResponse,
    StructuredResponseCapture,
    ToolExecutionCapture,
    bind_guidance_citations,
    format_context,
    investigation_memory_context,
    observation_outcomes,
)

logger = logging.getLogger("wfm_agui")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

# Stable client text for each RUN_ERROR code; logs retain safe error types/codes.
RUN_ERROR_MESSAGES = {
    "WFM_INVALID_RESPONSE": "The agent did not return a valid answer. Please retry.",
    "WFM_RUNTIME_ERROR": "The agent could not complete this request. Please retry.",
}


# ----------------------------------------------------------------------------
# Configuration (validated at startup, see lifespan below)
# ----------------------------------------------------------------------------

# The serving settings the lifespan checks itself. utils.model_config.settings() checks the model settings and
# utils.aws_clients.deployment_region() the Region.
REQUIRED_ENV_VARS = (
    "DEPLOYED_MemoryId",
    "DEPLOYED_GatewayUrl",
)


def _missing_env_vars() -> list[str]:
    return [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]


def _run_error(code: str) -> RunErrorEvent:
    """Build a RUN_ERROR event carrying the stable public message for a failure code, never exception text."""
    return RunErrorEvent(type=EventType.RUN_ERROR, message=RUN_ERROR_MESSAGES[code], code=code)


def _context_unavailable(exc: RuntimeError) -> JSONResponse:
    """Answer 503 when a signed service context cannot be checked because the signing secret is unavailable."""
    logger.error("signed service context check unavailable (SIMULATION_CONTEXT_SECRET_ARN): %s",
                 error_summary(exc.__cause__ or exc))
    return JSONResponse({"error": "service_context_unavailable"}, status_code=503)


def bind_host() -> str:
    """Return the interface the AG-UI server binds: AGENT_HOST, default 127.0.0.1 (this machine only).

    The Runtime image sets AGENT_HOST to every interface, because AgentCore reaches the container on port 8080.
    """
    return os.getenv("AGENT_HOST", "127.0.0.1")


async def _close_off_loop(resources: ExitStack) -> None:
    """Close a request's resources, such as its MCP client, in a worker thread, even if the stream was cancelled.

    MCPClient.stop() joins the client's session thread, which on the event loop would stall /ping and
    every other request. The shield keeps a client disconnect, which cancels the stream, from skipping it.
    """
    with CancelScope(shield=True):
        await to_thread.run_sync(resources.close)


# ----------------------------------------------------------------------------
# FastAPI app with startup validation
# ----------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Validate the Runtime's settings and pre-instantiate the supervisor template once."""
    try:
        missing = _missing_env_vars()
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
        region = deployment_region()
        guardrail = settings().guardrail  # validates every agent's model settings
    except RuntimeError as exc:
        # Fail loudly so AgentCore marks the runtime version unhealthy.
        logger.error("Runtime settings are invalid: %s", exc)
        raise

    memory_id = os.environ["DEPLOYED_MemoryId"]
    gateway_url = os.environ["DEPLOYED_GatewayUrl"]

    logger.info("🚀 AG-UI runtime starting")
    logger.info("   Region:    %s", region)
    logger.info("   Memory ID: %s", memory_id)
    logger.info("   Gateway:   %s", gateway_url)
    logger.info("   Guardrail: %s", "{guardrail_id} version {guardrail_version}".format(**guardrail)
                if guardrail else "none (local run)")

    # Pre-build the supervisor template once. ag_ui_strands clones it per
    # thread, so this also surfaces any agent-construction error at startup
    # rather than on the first invocation.
    try:
        # Investigations always use the public toolset, so that is the template worth checking.
        create_supervisor_agent(session_manager=None, public_demo=True, guidance_citations=[])
        logger.info(
            "✅ Supervisor template constructed (per-thread memory bound at runtime)")
    except Exception as exc:
        logger.error("Failed to construct supervisor template: %s", error_summary(exc))
        raise

    yield


app = FastAPI(title="WFM AG-UI Runtime", lifespan=_lifespan)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def _build_session_manager(session_id: str, actor_id: str) -> AgentCoreMemorySessionManager:
    return AgentCoreMemorySessionManager(
        agentcore_memory_config=AgentCoreMemoryConfig(
            memory_id=os.environ["DEPLOYED_MemoryId"],
            session_id=session_id,
            actor_id=actor_id,
        ),
        region_name=deployment_region(),
    )


# ----------------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------------

@app.get("/ping")
def ping() -> dict:
    """AgentCore Runtime healthcheck endpoint (per AG-UI protocol contract)."""
    import time
    return {"status": "Healthy", "time_of_last_update": int(time.time())}


@app.post("/invocations")
async def invocations(request: Request) -> StreamingResponse:
    """AG-UI invocation endpoint. Streams typed events while the supervisor runs.

    NOTE: We bind the raw `Request` and parse RunAgentInput by hand. The
    AgentCore proxy occasionally forwards the body without a parsable
    Content-Type, in which case FastAPI's automatic body binding sees the
    payload as raw `bytes` and rejects it with a 422 (which then gets turned
    into a 500 by JSON serialization of the bytes). Doing it manually keeps
    the stream alive and lets us emit a `RUN_ERROR` SSE frame on bad input.
    """
    accept_header = request.headers.get("accept", "text/event-stream")
    encoder = EventEncoder(accept=accept_header)

    raw_body = await request.body()
    try:
        body_text = raw_body.decode("utf-8") if raw_body else ""
        body_json = json.loads(body_text) if body_text else {}
        if not isinstance(body_json, dict):
            raise TypeError("body must be a JSON object")
        input_data = RunAgentInput.model_validate(body_json)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        # pydantic's ValidationError is a ValueError; its text quotes the submitted values.
        logger.warning("invalid /invocations body: %s", error_summary(exc))
        return JSONResponse(status_code=400, content={"error": "invalid_body"})
    except Exception as exc:  # noqa: BLE001  (any other RunAgentInput construction failure)
        logger.warning("RunAgentInput validation failed: %s", error_summary(exc))
        return JSONResponse(status_code=400, content={"error": "invalid_run_agent_input"})

    # forwarded_props is our extension surface beyond the canonical AG-UI envelope: the prompt,
    # the UI context envelope and the broker's signed service contexts. The toolset, memory actor and
    # session come only from a verified signed context; unsigned mode, actor, session or "trusted"
    # fields carry no authority. An unsigned request gets the public toolset and no AgentCore Memory.
    fwd = input_data.forwarded_props or {}
    if not isinstance(fwd, dict):
        fwd = {}
    identity = None  # (session_id, actor_id) for AgentCore Memory, from a verified context only

    # Each signed context verifies only with its own purpose's key (utils.simulation_context). A malformed
    # or invalid one answers 401; a signing secret that cannot be read answers 503.
    report_context = None
    if fwd.get("firetruck_report_context") is not None:
        from agents.firetruck_report_agent import validate_report_context
        from utils.simulation_context import verify
        try:
            claims = await to_thread.run_sync(verify, fwd.get("firetruck_report_token", ""), "firetruck_report")
            report_context = validate_report_context(fwd["firetruck_report_context"], claims, input_data.thread_id)
            fwd = {"prompt": report_context["question"], "context_envelope": report_context}
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "invalid_firetruck_report_context"}, status_code=401)
        except RuntimeError as exc:
            return _context_unavailable(exc)

    simulation_context = None
    simulation_selection = None
    if fwd.get("simulation_context"):
        from utils.simulation_context import verify
        try:
            simulation_context = await to_thread.run_sync(verify, fwd["simulation_context"], "simulation")
            simulation_context["token"] = fwd["simulation_context"]
            # Untrusted UI data for the model; tools are authorized by the signed context alone.
            simulation_selection = fwd.get("simulation_selection", {})
            if input_data.thread_id != simulation_context["session_id"]:
                raise ValueError("Session mismatch")
            identity = (simulation_context["session_id"],
                        hashlib.sha256(simulation_context["sub"].encode()).hexdigest())
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "invalid_simulation_context"}, status_code=401)
        except RuntimeError as exc:
            return _context_unavailable(exc)

    if report_context is None and simulation_context is None:
        from utils.simulation_context import investigation_identity
        try:
            identity = await to_thread.run_sync(investigation_identity, fwd, input_data.thread_id)
        except ValueError:
            return JSONResponse({"error": "invalid_investigation_context"}, status_code=401)
        except RuntimeError as exc:
            return _context_unavailable(exc)

    session_id = identity[0] if identity else input_data.thread_id
    context_envelope = fwd.get(
        "context_envelope") or fwd.get("contextEnvelope")
    prompt: str | None = fwd.get("prompt")

    if not prompt and input_data.messages:
        for msg in reversed(input_data.messages):
            if getattr(msg, "role", None) == "user":
                content = getattr(msg, "content", None)
                if isinstance(content, str) and content.strip():
                    prompt = content
                    break

    if not prompt:
        return JSONResponse({"error": "missing_prompt"}, status_code=400)

    ok, err = validate_input_format(prompt)
    if not ok:
        return JSONResponse({"error": f"input_format: {err}"}, status_code=400)
    prompt = sanitize_input(prompt)

    # Metadata only: the prompt and context carry addresses and incident details.
    logger.info(
        "invocation thread=%s session=%s build=%s prompt_chars=%d context=%s",
        input_data.thread_id, session_id, os.getenv("DEPLOYED_RuntimeBuild", "development"),
        len(prompt), bool(context_envelope),
    )

    async def event_generator() -> AsyncIterator[bytes]:
        resources = ExitStack()
        try:
            # Per-thread provider: ag_ui_strands invokes this once per thread_id and caches the
            # resulting SessionManager. Memory binds only to a signed actor and session; reports and
            # unsigned requests get none.
            session_manager = None

            def _provide_session_manager(_inp: RunAgentInput):
                if report_context is not None or identity is None:
                    return None
                return session_manager

            if report_context is None and identity is not None:
                session_manager = await to_thread.run_sync(_build_session_manager, *identity)

            # TEMPLATE supervisor — ag_ui_strands clones it per thread.
            hooks = []
            guidance_citations = []
            guidance_assessments = []
            guidance_failures = []
            observations = {}
            briefings = []
            communications = []
            restored_message_count = 0
            # There is no signed-in mode: every investigation, signed or not, gets the public toolset.
            public_investigation = report_context is None and simulation_context is None
            planning_context = (context_envelope if public_investigation
                                and isinstance(context_envelope, dict)
                                and context_envelope.get("run_id") and context_envelope.get("evidence") else None)
            if report_context is not None:
                from agents.firetruck_report_agent import build_report_agent
                supervisor = build_report_agent()
            elif simulation_context is not None:
                from agents.simulation_agent.simulation_agent import build_simulation_agent
                from utils.mcp_client import create_mcp_client
                # MCPClient.start() blocks until its session is ready, so it runs in a worker thread
                # while the event loop keeps serving. A cancelled stream still waits for the start to
                # finish, so the client is always registered for the close in the finally block.
                client = await to_thread.run_sync(lambda: resources.enter_context(create_mcp_client()))
                supervisor, hooks = build_simulation_agent(simulation_context, simulation_selection, client)
            else:
                # Same verified actor/session as execution. Read bounded native Memory messages
                # before task selection; no browser history or second conversation store.
                messages = (await to_thread.run_sync(lambda: [
                    item.to_message() for item in session_manager.list_messages(
                        session_manager.session_id, "default", limit=MEMORY_MESSAGES)])
                    if session_manager is not None and planning_context is None else [])
                restored_message_count = len(messages)
                source_location = {}
                topics = ({"guidance"} if planning_context is not None else await to_thread.run_sync(lambda:
                    select_investigation_topics(
                        prompt, (context_envelope or {}).get("previous_observations"), messages,
                        source_location=source_location)))
                logger.info("investigation task scope thread=%s topics=%s", input_data.thread_id, sorted(topics))
                supervisor = create_supervisor_agent(
                    session_manager=None, public_demo=True, guidance_citations=guidance_citations,
                    guidance_failures=guidance_failures,
                    guidance_assessments=guidance_assessments,
                    observations=observations,
                    planning_context=planning_context,
                    previous_observations=(context_envelope or {}).get("previous_observations"),
                    requested_topics=topics,
                    memory_context=investigation_memory_context(messages),
                    hooks=hooks,
                    source_location=source_location,
                    user_question=prompt,
                    briefings=briefings,
                    communications=communications,
                    owner=(identity[1], identity[0]) if identity else None,
                )

            structured = StructuredResponseCapture()
            hooks.append(structured)
            execution = ToolExecutionCapture(max_calls=INVESTIGATION_TOOL_CALLS if public_investigation else None)
            hooks.append(execution)
            output_model = PublicAnswerResponse if public_investigation else AnswerResponse
            agui = StrandsAgent(
                agent=supervisor,
                hooks=hooks,
                name="wfm_supervisor",
                description="Wildfire supervisor (AG-UI streaming)",
                config=StrandsAgentConfig(
                    thread_agent_kwargs=lambda _input: {"structured_output_model": output_model,
                        **({"tool_executor": supervisor.tool_executor} if public_investigation else {})},
                    session_manager_provider=_provide_session_manager,
                    # We re-emit our own STATE_SNAPSHOT after Pydantic
                    # validation; suppress the wrapper's MessagesSnapshot frames.
                    emit_messages_snapshot=False,
                ),
            )

            # Preserve the question for public investigations; other capabilities retain
            # their context envelope. Evidence remains bound to the selected tool in code.
            # Public questions stay separate from server reference records. Native Memory and
            # invocation-bound tools provide history/evidence; the default UI map cannot set intent.
            routing_context = None if public_investigation else context_envelope
            enriched = format_context(prompt, routing_context)
            patched_messages = list(input_data.messages or [])
            if patched_messages:
                for i in range(len(patched_messages) - 1, -1, -1):
                    if getattr(patched_messages[i], "role", None) == "user":
                        patched_messages[i] = patched_messages[i].model_copy(
                            update={"content": enriched}
                        )
                        break
            else:
                patched_messages = [UserMessage(
                    id="u1", role="user", content=enriched)]

            patched_input = input_data.model_copy(
                update={"messages": patched_messages, "forwarded_props": {}})

            published_observations = {}
            async for event in agui.run(patched_input):
                etype = getattr(event, "type", None)
                # Suppress the wrapper's STATE_SNAPSHOT and RUN_FINISHED so
                # ours (with the validated UniversalResponse) come last.
                if etype in (EventType.STATE_SNAPSHOT, EventType.RUN_FINISHED):
                    continue
                if etype == EventType.RUN_ERROR:
                    # The adapter puts str(exception) in its RUN_ERROR and has already
                    # printed the traceback; clients get the stable text and the run ends.
                    logger.error("AG-UI adapter reported RUN_ERROR code=%s", getattr(event, "code", None))
                    yield encoder.encode(_run_error("WFM_RUNTIME_ERROR"))
                    return
                if etype == EventType.TOOL_CALL_RESULT:
                    event = event.model_copy(update={"content": execution.envelope(
                        event.tool_call_id, event.content)})
                yield encoder.encode(event)
                if etype == EventType.RUN_STARTED:
                    yield encoder.encode(CustomEvent(type=EventType.CUSTOM, name="runtime_execution", value={
                        "build_id": os.getenv("DEPLOYED_RuntimeBuild", "development"),
                        "conversation_id": input_data.thread_id,
                        "memory_session_id": session_manager.session_id if session_manager is not None else None,
                        "restored_messages": restored_message_count,
                    }))
                if (public_investigation and etype == EventType.TOOL_CALL_RESULT
                        and observations and observations != published_observations):
                    # Publish actual completed provider receipts while guidance continues.
                    # This is an in-progress snapshot, never a terminal planning response.
                    partial = bind_guidance_citations(
                        {"type": "structuredResponse", "content": {"response":
                            "Source observations received; the requested investigation is still running."}},
                        [], observations=observations)
                    partial = UniversalResponse.model_validate(partial).model_dump(mode="json")
                    published_observations = deepcopy(observations)
                    yield encoder.encode(StateSnapshotEvent(
                        type=EventType.STATE_SNAPSHOT, snapshot=partial))

            # Strands validates its final structured-output tool. Progress
            # narration and model-written JSON are not authoritative output.
            try:
                if structured.value is None:
                    raise ValueError("Agent returned no validated structured response")
                final_payload = UniversalResponse.model_validate(structured.value).model_dump(mode="json")
                if public_investigation:
                    completion = final_payload["content"]["response"]
                    if completion == "location_required" and topics:
                        raise ValueError("Selected investigation tasks cannot complete as clarification")
                    if completion != "location_required" and not topics:
                        raise ValueError("Ambiguous investigation requires clarification")
                    if (completion == "source_results_ready"
                            and not (topics-{"guidance", "communication"}) <= observation_outcomes(observations)):
                        raise ValueError("Requested observations have no source receipts")
                    if completion == "guidance_unavailable" and not guidance_failures:
                        raise ValueError("Guidance failure has no specialist receipt")
                    if (completion == "source_results_ready" and "guidance" in topics
                            and not (guidance_assessments or guidance_failures)):
                        raise ValueError("Requested guidance has no specialist receipt")
                    if completion != "location_required" and "communication" in topics and not communications:
                        raise ValueError("Requested communication has no specialist receipt")
                    final_payload = UniversalResponse.model_validate(
                        bind_guidance_citations(final_payload, guidance_citations,
                                               guidance_failed=bool(guidance_failures),
                                               observations=observations,
                                               assessments=guidance_assessments,
                                               briefings=briefings, communications=communications)
                    ).model_dump(mode="json")
            except ValueError as exc:
                logger.error("Schema validation failed for agent output: %s", error_summary(exc))
                yield encoder.encode(_run_error("WFM_INVALID_RESPONSE"))
                return

            yield encoder.encode(
                StateSnapshotEvent(
                    type=EventType.STATE_SNAPSHOT, snapshot=final_payload)
            )
            yield encoder.encode(
                RunFinishedEvent(
                    type=EventType.RUN_FINISHED,
                    thread_id=patched_input.thread_id,
                    run_id=patched_input.run_id,
                )
            )

        except Exception as exc:  # noqa: BLE001 — the client receives one stable Runtime failure
            logger.error("AG-UI run failed: thread=%s error=%s", input_data.thread_id, error_summary(exc))
            try:
                yield encoder.encode(_run_error("WFM_RUNTIME_ERROR"))
            except Exception:  # noqa: BLE001
                # Last-ditch raw fallback if encoder itself fails.
                fallback = {"type": "RUN_ERROR", "code": "WFM_RUNTIME_ERROR",
                            "message": RUN_ERROR_MESSAGES["WFM_RUNTIME_ERROR"]}
                yield f"data: {json.dumps(fallback)}\n\n".encode()

        finally:
            await _close_off_loop(resources)

    return StreamingResponse(event_generator(), media_type=encoder.get_content_type())


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=bind_host(),
        port=int(os.getenv("AGENT_PORT", "8080")),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
