"""Exercise actual Runtime startup and MCP client lifecycle without AWS/network access.

The image runs it as a release gate (python /app/check_startup.py); a checkout runs it with
.venv/bin/python backend/runtime/check_startup.py. A checkout has no generated schema models, so
they are generated into a temporary folder first, with the Dockerfile's arguments.
"""
import asyncio
import base64
import hashlib
import importlib.metadata
import io
import json
import logging
import os
import re
import secrets
import sys
import threading
from contextlib import ExitStack, asynccontextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import anyio

HERE = Path(__file__).resolve().parent  # /app in the image; backend/runtime in a checkout
# The image stages the lock and schemas beside this file; a checkout keeps them at the repository root.
SOURCE = HERE if (HERE / "requirements-lock.txt").is_file() else HERE.parents[1]


def check(condition, detail):
    """Fail the startup check with the detail unless the condition holds; unlike assert, python -O keeps it."""
    if not condition:
        raise AssertionError(detail)


from schema_models import generate_models

# Run only as a deployment check, never imported by the server. The fixture credentials are random
# per run and reach nothing: there is no AWS or network call.
os.environ.update({
    "AGENTCORE_RUNTIME": "true", "AWS_EC2_METADATA_DISABLED": "true",
    "AWS_ACCESS_KEY_ID": secrets.token_hex(10), "AWS_SECRET_ACCESS_KEY": secrets.token_urlsafe(30),
    "AWS_DEFAULT_REGION": "us-west-2", "AWS_REGION": "us-west-2",
    "DEPLOYED_DeploymentRegion": "us-west-2", "DEPLOYED_MemoryId": "test-memory",
    "DEPLOYED_GatewayUrl": "https://gateway.invalid/mcp", "MODEL_TEMPERATURE": "0.1",
    # The deployed Runtime refuses to start without a published guardrail version (utils.model_config).
    "DEPLOYED_GuardrailId": "test-guardrail", "DEPLOYED_GuardrailVersion": "1",
})
# No signing-secret ARN is set, so a well-formed signed context cannot be checked here (utils.simulation_context).
os.environ.pop("SIMULATION_CONTEXT_SECRET_ARN", None)
for agent in ("SUPERVISOR", "DETECTION", "RESPONSE", "COMM", "SIMULATION", "REPORT"):
    os.environ[agent + "_MODEL_ID"] = "test-model"
MODELS = generate_models(SOURCE / "schemas/api-schema.yaml")

import wfm_agui_entrypoint as entrypoint
from ag_ui.core import (
    EventType,
    RunErrorEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallResultEvent,
)
from agents.simulation_agent import simulation_agent
from fastapi.testclient import TestClient
from utils import mcp_client, model_config, simulation_context
from wfm_agui_entrypoint import app

guardrail = model_config.settings().guardrail
check((guardrail["guardrail_id"], guardrail["guardrail_version"]) == ("test-guardrail", "1"), guardrail)
check("agents.communication_agent.communication_agent" not in sys.modules,
      "the public runtime must not eagerly load inactive specialists")

# Local runs bind this machine only; the image sets AGENT_HOST so that AgentCore reaches port 8080.
with patch.dict(os.environ):
    os.environ.pop("AGENT_HOST", None)
    check(entrypoint.bind_host() == "127.0.0.1", "a local run must bind 127.0.0.1 unless AGENT_HOST is set")
    os.environ["AGENT_HOST"] = "10.0.0.5"
    check(entrypoint.bind_host() == "10.0.0.5", "AGENT_HOST must choose the interface the server binds")
check(re.search(r"^ENV AGENT_HOST=0\.0\.0\.0$", (HERE / "Dockerfile").read_text(), re.MULTILINE),
      "the Runtime image must set AGENT_HOST so that AgentCore can reach port 8080")

with TestClient(app) as client:
    response = client.get("/ping")
    check(response.status_code == 200, response.text)
    check(response.json()["status"] == "Healthy", response.text)
    # Malformed signed contexts answer 401, never 500, and never reach Secrets Manager.
    well_formed = ".".join(base64.urlsafe_b64encode(part).rstrip(b"=").decode()
                           for part in (b'{"alg":"HS256","typ":"JWT"}', b'{"purpose":"simulation"}', b"x" * 32))
    for props, error in (({"simulation_context": "invalid-test-token"}, "invalid_simulation_context"),
                         ({"simulation_context": {"alg": "none"}}, "invalid_simulation_context"),
                         ({"simulation_context": "e30.W10.c2ln"}, "invalid_simulation_context"),
                         ({"investigation_context": ["not", "a", "token"]}, "invalid_investigation_context"),
                         ({"firetruck_report_context": {}, "firetruck_report_token": 7},
                          "invalid_firetruck_report_context")):
        response = client.post("/invocations", json={
            "threadId": str(uuid4()), "runId": str(uuid4()), "state": {}, "tools": [], "context": [],
            "messages": [], "forwardedProps": props})
        check(response.status_code == 401 and response.json() == {"error": error}, response.text)
    # A well-formed context with no readable signing secret answers 503, naming no detail.
    response = client.post("/invocations", json={
        "threadId": str(uuid4()), "runId": str(uuid4()), "state": {}, "tools": [], "context": [],
        "messages": [], "forwardedProps": {"simulation_context": well_formed}})
    check(response.status_code == 503 and response.json() == {"error": "service_context_unavailable"},
          response.text)

# A missing or invalid setting stops start-up, naming it, before the supervisor template is built (RTGW-8).
for name, value in (("COMM_MODEL_ID", None), ("SIMULATION_MODEL_ID", None), ("MODEL_TEMPERATURE", "warm"),
                    ("DEPLOYED_MemoryId", None)):
    built = AssertionError(f"the supervisor template was built without a valid {name}")
    with patch.dict(os.environ), patch.object(entrypoint, "create_supervisor_agent", side_effect=built):
        os.environ.pop(name, None)
        if value is not None:
            os.environ[name] = value
        model_config.settings.cache_clear()
        try:
            with TestClient(app):
                pass
        except RuntimeError as exc:
            check(name in str(exc), f"start-up failed without naming {name}: {exc}")
        else:
            check(False, f"the Runtime started without a valid {name}")
model_config.settings.cache_clear()


class ScriptedAgent:
    """Exercise the endpoint's typed-output boundary, independent of narration."""
    messages = ()
    typed_output = None
    adapter_error = None  # text the AG-UI adapter would put in its own RUN_ERROR
    failure = None  # text of an exception raised inside the run
    provider_observations = None
    session_manager = None

    def __init__(self, **kwargs):
        self.hooks = kwargs["hooks"]
        self.agent = kwargs["agent"]
        self.output_model = kwargs["config"].thread_agent_kwargs(None)["structured_output_model"]
        ScriptedAgent.session_manager = kwargs["config"].session_manager_provider(None)

    async def run(self, _input):
        """Stand-in agent stream: yield or raise the scripted events."""
        if self.adapter_error is not None:
            yield RunErrorEvent(type=EventType.RUN_ERROR, message=self.adapter_error, code="ADAPTER_BUG")
        if self.failure is not None:
            raise RuntimeError(self.failure)
        if self.provider_observations is not None:
            self.agent.observations.update(self.provider_observations)
            from types import SimpleNamespace
            for hook in self.hooks:
                if isinstance(hook, entrypoint.ToolExecutionCapture):
                    hook.after(SimpleNamespace(tool_use={"toolUseId": "detect"},
                                               result={"status": "success"}))
            yield ToolCallResultEvent(type=EventType.TOOL_CALL_RESULT, message_id=str(uuid4()),
                                      tool_call_id="detect", content="Provider receipt", role="tool")
        for text in self.messages:
            message_id = str(uuid4())
            yield TextMessageStartEvent(type=EventType.TEXT_MESSAGE_START,
                                        message_id=message_id, role="assistant")
            midpoint = len(text) // 2
            for delta in (text[:midpoint], text[midpoint:]):
                yield TextMessageContentEvent(type=EventType.TEXT_MESSAGE_CONTENT,
                                              message_id=message_id, delta=delta)
            yield TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id=message_id)
        if self.typed_output is not None:
            from types import SimpleNamespace
            event = SimpleNamespace(result=SimpleNamespace(
                structured_output=self.output_model.model_validate(self.typed_output)))
            for hook in self.hooks:
                if isinstance(hook, entrypoint.StructuredResponseCapture):
                    hook.after(event)


valid_answer = json.dumps({"type": "structuredResponse", "content": {"response": "location_required"}})
with TestClient(app) as client, patch.object(entrypoint, "StrandsAgent", ScriptedAgent), \
        patch.object(entrypoint, "select_investigation_topics", return_value=set()), \
        patch.object(entrypoint, "_build_session_manager", autospec=True) as unsigned_memory:
    request = {"threadId": str(uuid4()), "runId": str(uuid4()), "state": {}, "tools": [], "context": [],
               "messages": [{"id": str(uuid4()), "role": "user", "content": "Prepare a simulation."}]}
    ScriptedAgent.messages = ["I will prepare the draft.", "The tool returned a saved receipt.", valid_answer]
    ScriptedAgent.typed_output = json.loads(valid_answer)
    response = client.post("/invocations", json=request)
    check('"type":"STATE_SNAPSHOT"' in response.text, response.text)
    check('"type":"RUN_ERROR"' not in response.text, response.text)
    # Even an adapter that bypasses native hooks cannot clarify an already selected scope.
    for topics in ({"guidance"}, {"fires"}, {"communication"}):
        with patch.object(entrypoint, "select_investigation_topics", return_value=topics):
            response = client.post("/invocations", json=request)
        check('"type":"RUN_ERROR"' in response.text and '"type":"STATE_SNAPSHOT"' not in response.text,
              response.text)
    # A status code alone cannot complete a factual answer without provider or guidance receipts.
    ScriptedAgent.typed_output = {"content": {"response": "source_results_ready"}}
    response = client.post("/invocations", json=request)
    check('"type":"RUN_ERROR"' in response.text and '"type":"STATE_SNAPSHOT"' not in response.text, response.text)
    # Public completion cannot reintroduce a generated assessment under a typed receipt.
    ScriptedAgent.typed_output = {"content": {"response": "A generated final narrative."}}
    response = client.post("/invocations", json=request)
    check('"type":"RUN_ERROR"' in response.text and '"type":"STATE_SNAPSHOT"' not in response.text, response.text)
    # Never recover an earlier JSON object when the final answer is invalid.
    ScriptedAgent.messages = [valid_answer, "This final answer is not JSON."]
    ScriptedAgent.typed_output = None
    response = client.post("/invocations", json=request)
    check('"type":"RUN_ERROR"' in response.text, response.text)
    check('"type":"STATE_SNAPSHOT"' not in response.text, response.text)
    check(entrypoint.RUN_ERROR_MESSAGES["WFM_INVALID_RESPONSE"] in response.text, response.text)
# These unsigned investigations bind no AgentCore Memory at either entry-point guard (RTGW-2).
check(not unsigned_memory.called, "An unsigned investigation must bind no Memory")
check(ScriptedAgent.session_manager is None, "An unsigned investigation's agent must get no session manager")

from types import SimpleNamespace

with TestClient(app) as client, patch.object(entrypoint, "StrandsAgent", ScriptedAgent), \
        patch.object(entrypoint, "select_investigation_topics", return_value={"weather"}), \
        patch.object(entrypoint, "create_supervisor_agent", lambda **kwargs: SimpleNamespace(
            observations=kwargs["observations"], tool_executor=None)):
    ScriptedAgent.messages = []
    ScriptedAgent.typed_output = {"content": {"response": "source_results_ready"}}
    ScriptedAgent.provider_observations = {"location": {
        "name": "Fixture weather point", "latitude": 39.75, "longitude": -121.6},
        "mapData": {"locations": [{"latitude": 39.75, "longitude": -121.6,
                                  "currentWeather": {"provider": "Fixture"}}]}}
    response = client.post("/invocations", json=request)
    frames = [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines()
              if line.startswith("data: ")]
    snapshots = [frame["snapshot"] for frame in frames if frame["type"] == "STATE_SNAPSHOT"]
    result = next(frame for frame in frames if frame["type"] == "TOOL_CALL_RESULT")
    check(json.loads(result["content"]) == {"status": "success", "result": "Provider receipt"},
          "The actual SDK tool status must survive the AG-UI adapter")
    check(len(snapshots) == 2, response.text)
    check("still running" in snapshots[0]["content"]["response"], response.text)
    check(not snapshots[0]["content"]["knowledgeBaseCitations"], response.text)
    check(snapshots[1]["content"]["location"]["latitude"] == 39.75, response.text)
    check(snapshots[1]["content"]["mapData"] == snapshots[0]["content"]["mapData"], response.text)
    check("DTN" in snapshots[1]["content"]["response"]
          and "unavailable" in snapshots[1]["content"]["response"], response.text)
    check(not snapshots[1]["content"]["knowledgeBaseCitations"], response.text)
    check(frames[-1]["type"] == "RUN_FINISHED", response.text)
    ScriptedAgent.provider_observations = None


from strands.types.session import SessionMessage

# Selection must see owner-scoped native Memory before execution, including exact tool receipts.
history = [
    {"role": "user", "content": [{"text": "Find fires near Sacramento CA"}]},
    {"role": "assistant", "content": [{"toolUse": {
        "toolUseId": "retained", "name": "detection_agent_tool", "input": {"query": "Sacramento CA"}}}]},
    {"role": "user", "content": [{"toolResult": {"toolUseId": "retained", "status": "success",
        "content": [{"text": json.dumps({"location": {"latitude": 38.58, "longitude": -121.49},
                                       "mapData": {"summary": {"totalActiveFires": 0}, "locations": []}})}]}}]},
]
thread = str(uuid4())
memory = SimpleNamespace(session_id=thread, list_messages=lambda *args, **kwargs: [
    SessionMessage.from_message(message, index) for index, message in enumerate(history)])
with TestClient(app) as client, patch.object(entrypoint, "StrandsAgent", ScriptedAgent), \
        patch.object(simulation_context, "investigation_identity", return_value=(thread, "owner-digest")), \
        patch.object(entrypoint, "_build_session_manager", return_value=memory), \
        patch.object(entrypoint, "select_investigation_topics", return_value=set()) as selection, \
        patch.object(entrypoint, "create_supervisor_agent", return_value=SimpleNamespace(
            tool_executor=None)) as build:
    ScriptedAgent.messages, ScriptedAgent.typed_output = [], json.loads(valid_answer)
    response = client.post("/invocations", json={
        **request, "threadId": thread, "messages": [{"id": str(uuid4()), "role": "user",
                                                   "content": "What about tomorrow?"}]})
    check('"type":"RUN_ERROR"' not in response.text, response.text)
    check(selection.call_args.args[2] == history, "Selection must receive native Memory messages")
    check(build.call_args.kwargs["memory_context"]["observations"][0]["location"]["latitude"] == 38.58,
          "Delegation must receive the retained source location")
    check(ScriptedAgent.session_manager is memory, "Selection and execution must share the verified manager")


class LogCapture(logging.Handler):
    """Collect every formatted log record, tracebacks included, to prove what never reaches the log."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(self.format(record))


# Request text and exception text stay out of client errors; request text also stays out of the log.
address = "4521 Canary Ridge Road, Paradise, CA"  # fixture address
failure = "fixture failure detail that stays server-side"
capture = LogCapture()
logging.getLogger().addHandler(capture)
# stdout is outside the logging policy (RTGW-7), so the same request text must not reach it either.
printed = io.StringIO()
with redirect_stdout(printed), TestClient(app) as client, patch.object(entrypoint, "StrandsAgent", ScriptedAgent), \
        patch.object(entrypoint, "select_investigation_topics", return_value={"guidance"}):
    thread = str(uuid4())
    request = {"threadId": thread, "runId": str(uuid4()), "state": {}, "tools": [], "context": [],
               "messages": [{"id": str(uuid4()), "role": "user", "content": f"Evacuation routes near {address}?"}]}
    ScriptedAgent.messages, ScriptedAgent.typed_output, ScriptedAgent.failure = [], None, failure
    response = client.post("/invocations", json=request)
    check(entrypoint.RUN_ERROR_MESSAGES["WFM_RUNTIME_ERROR"] in response.text, response.text)
    check(failure not in response.text, response.text)
    # The adapter puts exception text in its own RUN_ERROR; clients get the stable text instead.
    ScriptedAgent.failure, ScriptedAgent.adapter_error = None, failure
    response = client.post("/invocations", json=request)
    check(entrypoint.RUN_ERROR_MESSAGES["WFM_RUNTIME_ERROR"] in response.text, response.text)
    check(failure not in response.text, response.text)
    ScriptedAgent.adapter_error = None
    ScriptedAgent.typed_output = json.loads(valid_answer)
    memory = SimpleNamespace(session_id=thread, list_messages=lambda *args, **kwargs: [])
    with patch.object(simulation_context, "investigation_identity", return_value=(thread, "owner-digest")), \
            patch.object(entrypoint, "_build_session_manager", autospec=True, return_value=memory) as bind_memory, \
            patch.object(entrypoint, "select_investigation_topics", return_value=set()):
        response = client.post("/invocations", json=request)
    check('"type":"STATE_SNAPSHOT"' in response.text, response.text)
    # AgentCore Memory binds to the signed investigation's session and actor, in that order (RTGW-2).
    bind_memory.assert_called_once_with(session_id=thread, actor_id="owner-digest")
    # Invalid input is rejected without echoing it.
    response = client.post("/invocations", json={"threadId": address})
    check(response.status_code == 400 and response.json() == {"error": "invalid_body"}, response.text)
logging.getLogger().removeHandler(capture)
logged = "\n".join(capture.records)
check(address not in logged, "request text reached the log")
check(address not in printed.getvalue(), "request text reached stdout")
check(failure not in logged, "exception detail reached the server log")
check("RuntimeError" in logged, "safe exception type must remain in the server log")


def on_event_loop():
    """Return True when the caller runs on a thread with a running asyncio event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class BlockingMcpClient:
    """Stand-in MCP client whose start blocks like MCPClient.start(), until a /ping is answered meanwhile."""

    def __init__(self):
        self.starting, self.pinged, self.calls = threading.Event(), threading.Event(), []

    def __enter__(self):
        """Record that the MCP client started off the event loop once /ping answered."""
        self.starting.set()
        self.calls.append(("start", on_event_loop(), self.pinged.wait(timeout=10)))
        return self

    def __exit__(self, *_exc):
        """Record that the MCP client stopped and whether that ran on the event loop."""
        self.calls.append(("stop", on_event_loop()))
        return False


# A simulation turn starts and stops its MCP client off the event loop, so /ping is answered meanwhile.
mcp = BlockingMcpClient()
thread = str(uuid4())
claims = {"session_id": thread, "sub": "fixture-owner", "allowed_operations": ["get_simulation_status"]}
request = {"threadId": thread, "runId": str(uuid4()), "state": {}, "tools": [], "context": [],
           "forwardedProps": {"simulation_context": "fixture-signed-context"},
           "messages": [{"id": str(uuid4()), "role": "user", "content": "Is the run finished?"}]}
answers = []


def checked_context(*_):
    check(not on_event_loop(), "context verification must not read secrets on the event loop")
    return claims


def checked_factory():
    mcp.calls.append(("factory", on_event_loop()))
    return mcp


with TestClient(app) as client, patch.object(entrypoint, "StrandsAgent", ScriptedAgent), \
        patch.object(simulation_context, "verify", side_effect=checked_context), \
        patch.object(entrypoint, "_build_session_manager", autospec=True,
                     return_value=SimpleNamespace(session_id=thread)) as bind_memory, \
        patch.object(mcp_client, "create_mcp_client", side_effect=checked_factory), \
        patch.object(simulation_agent, "build_simulation_agent", side_effect=lambda *_: (None, [])):
    ScriptedAgent.messages = []
    ScriptedAgent.typed_output = {"type": "structuredResponse", "content": {"response": "The run is still going."}}
    turn = threading.Thread(target=lambda: answers.append(client.post("/invocations", json=request)))
    turn.start()
    check(mcp.starting.wait(timeout=10), "the simulation turn never started its MCP client")
    check(client.get("/ping").status_code == 200, "/ping failed while the MCP client was starting")
    mcp.pinged.set()
    turn.join(timeout=30)
check(mcp.calls == [("factory", False), ("start", False, True), ("stop", False)],
      f"the MCP client must construct, start and stop off the event loop: {mcp.calls}")
check(answers and '"type":"STATE_SNAPSHOT"' in answers[0].text, answers[0].text if answers else "no answer")
# AgentCore Memory binds to the signed simulation context's session and the digest of its subject (RTGW-2).
bind_memory.assert_called_once_with(session_id=thread, actor_id=hashlib.sha256(b"fixture-owner").hexdigest())


async def close_after_disconnect():
    """Close a request's resources inside an already-cancelled scope, as a client disconnect leaves them."""
    closed = []
    resources = ExitStack()
    resources.callback(lambda: closed.append(on_event_loop()))
    with anyio.CancelScope() as scope:
        scope.cancel()
        await entrypoint._close_off_loop(resources)
    return closed


check(anyio.run(close_after_disconnect) == [False],
      "a cancelled stream must still close its MCP client, off the event loop")


async def check_transport():
    """Prove the gateway MCP transport sends the bearer token, follows no redirects and closes after a failure."""
    observed = []
    token = secrets.token_urlsafe(16)

    @asynccontextmanager
    async def transport(url, *, http_client):
        check(url == "https://gateway.invalid/mcp", url)
        check(http_client.headers["Authorization"] == f"Bearer {token}", "the gateway request lacks its token")
        check(not http_client.follow_redirects, "the gateway client must not follow redirects")
        check(not http_client.is_closed, "the gateway client closed before the transport opened")
        observed.append(http_client)
        yield ("read", "write", lambda: None)

    oauth_endpoint = "https://auth.invalid/oauth2/token"  # never resolves; get_valid_token is patched
    config = mcp_client.GatewayConfiguration(
        gateway_url="https://gateway.invalid/mcp", region="us-west-2",
        client_id="test-client", client_secret=secrets.token_urlsafe(16), token_endpoint=oauth_endpoint)
    gateway = mcp_client.MCPGatewayClient(config)
    with patch.object(gateway.token_manager, "get_valid_token", return_value=token), \
         patch.object(mcp_client, "streamable_http_client", transport), \
         patch.object(mcp_client, "MCPClient", lambda factory: factory):
        factory = gateway.create_mcp_client()
        try:
            async with factory() as streams:
                check(streams[0] == "read", streams)
                raise ValueError("exercise cleanup after failure")
        except ValueError:
            pass
    check(len(observed) == 1 and observed[0].is_closed, "the gateway HTTP client stayed open after a failure")


asyncio.run(check_transport())
constraints = dict(
    line.split("==", 1) for line in (SOURCE / "requirements-lock.txt").read_text().splitlines()
    if "==" in line and not line.startswith("#")
)
versions = {
    name: importlib.metadata.version(name)
    for name in ("mcp", "fastapi", "starlette", "strands-agents", "bedrock-agentcore", "boto3")
}
for name, version in versions.items():
    check(version == constraints[name], f"{name} differs from reviewed constraints")
print("Runtime startup and its refusal of an invalid setting, bind host, typed-output boundary, invalid-context "
      "rejection (401) and an unreadable signing secret (503), stable client errors, "
      "log privacy, Memory bound only to the signed session and actor, "
      "MCP client start and stop off the event loop and MCP transport lifecycle passed.")
print("RUNTIME_PYTHON_PACKAGES " + json.dumps(versions))
