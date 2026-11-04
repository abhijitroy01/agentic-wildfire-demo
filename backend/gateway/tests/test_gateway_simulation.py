"""Simulation Gateway adapter: a validated origin, no redirects, and stable errors for every upstream failure."""
import http.server
import importlib.util
import json
import logging
import socket
import threading
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

GATEWAY = Path(__file__).resolve().parents[1]
TOOL = "get_simulation_status"
PATH = f"/api/internal/v1/simulations/{TOOL}"
UNAVAILABLE = {"error": {"code": "SIMULATOR_UNAVAILABLE", "message": "Simulator API is unavailable."}}
INTERNAL_ALB = "http://internal-Wildf-ApiLo-1234567890.us-west-2.elb.amazonaws.com"


@pytest.fixture
def adapter(load_handler):
    """Load the simulation adapter Lambda with fresh caches."""
    return load_handler("simulation/handler.py")


@pytest.fixture
def simulator():
    """Serve queued (status, headers, body) replies on a loopback port and record every request received."""
    replies, seen = [], []

    class Reply(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.append({"method": self.command, "path": self.path,
                         "context": self.headers.get("X-Simulation-Context"), "body": body})
            status, headers, content = replies.pop(0) if replies else (200, {}, b'{"followed": true}')
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        do_GET = do_POST

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Reply)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(origin=f"http://127.0.0.1:{server.server_port}", replies=replies, seen=seen)
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.parametrize("url, origin", [
    ("https://simulator.example.com", "https://simulator.example.com"),
    ("https://simulator.example.com/", "https://simulator.example.com"),
    (INTERNAL_ALB, INTERNAL_ALB),
])
def test_origin_is_https_or_plain_http_to_an_internal_load_balancer(adapter, monkeypatch, url, origin):
    monkeypatch.setenv("SIMULATION_INTERNAL_URL", url)
    assert adapter.simulator_origin() == origin


@pytest.mark.parametrize("url", [
    "", "https://", "file:///etc/hosts", "ftp://internal-a-1.us-west-2.elb.amazonaws.com",
    "http://simulator.example.com", "http://internal-a-1.us-west-2.elb.amazonaws.com.example.com",
    "https://user@simulator.example.com", "https://simulator.example.com/?next=1",
    "https://simulator.example.com/#part",
])
def test_origin_rejects_other_schemes_hosts_credentials_and_queries(adapter, monkeypatch, url):
    monkeypatch.setenv("SIMULATION_INTERNAL_URL", url)
    with pytest.raises(RuntimeError, match="SIMULATION_INTERNAL_URL"):
        adapter.simulator_origin()


def test_opener_has_no_file_handler(adapter):
    with pytest.raises(urllib.error.URLError, match="unknown url type"):
        adapter.opener().open("file:///etc/hosts", timeout=1)


def test_redirects_are_refused_so_the_signed_context_stays_on_the_simulator(adapter, simulator, monkeypatch):
    monkeypatch.setattr(adapter, "simulator_origin", lambda: simulator.origin)
    simulator.replies.append((302, {"Location": f"{simulator.origin}/captured"}, b""))
    assert adapter.forward(TOOL, {}, "signed-context") == UNAVAILABLE
    assert [request["path"] for request in simulator.seen] == [PATH]


@pytest.mark.parametrize("status, body, expected", [
    (200, b'{"status": "running"}', {"status": "running"}),
    (409, b'{"error": {"code": "RUN_CONFLICT", "message": "Busy."}}',
     {"error": {"code": "RUN_CONFLICT", "message": "Busy."}}),
    (409, b"<html>proxy error</html>", UNAVAILABLE),
    (422, b'["not", "an", "envelope"]', UNAVAILABLE),
    (500, b'{"error": {"code": "INTERNAL", "message": "Boom."}}', UNAVAILABLE),
    (200, b"not json", UNAVAILABLE),
])
def test_answers_become_the_result_the_error_envelope_or_unavailable(adapter, simulator, monkeypatch,
                                                                    status, body, expected):
    monkeypatch.setattr(adapter, "simulator_origin", lambda: simulator.origin)
    simulator.replies.append((status, {"Content-Type": "application/json"}, body))
    assert adapter.forward(TOOL, {"run_id": "r1"}, "signed-context") == expected


def test_unreachable_simulator_is_unavailable(adapter, monkeypatch):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(adapter, "simulator_origin", lambda: f"http://127.0.0.1:{port}")
    assert adapter.forward(TOOL, {}, "signed-context") == UNAVAILABLE


def test_misconfigured_url_is_logged_by_name_and_never_contacted(adapter, monkeypatch, caplog):
    monkeypatch.setenv("SIMULATION_INTERNAL_URL", "http://simulator.example.com")
    monkeypatch.setattr(adapter, "opener", lambda: pytest.fail("a misconfigured origin must not be contacted"))
    with caplog.at_level(logging.WARNING):
        assert adapter.forward(TOOL, {}, "signed-context") == UNAVAILABLE
    assert "SIMULATION_INTERNAL_URL" in caplog.text
    assert "signed-context" not in caplog.text


def tool_context(name=f"simulation-service___{TOOL}"):
    """Build the Lambda context AgentCore Gateway passes, naming the called tool."""
    return SimpleNamespace(client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": name}))


def test_tool_call_reaches_the_simulator_with_its_context_unchanged(adapter, simulator, monkeypatch):
    # The adapter holds no signing key: the workspace API's internal route verifies this context.
    monkeypatch.setattr(adapter, "simulator_origin", lambda: simulator.origin)
    simulator.replies.append((200, {}, b'{"status": "running"}'))
    result = adapter.handler({"trusted_context": "signed-context", "payload": '{"run_id": "r1"}'}, tool_context())
    assert result == {"status": "running"}
    assert simulator.seen == [{"method": "POST", "path": PATH, "context": "signed-context",
                               "body": b'{"run_id": "r1"}'}]


def test_the_simulator_decides_authorization_and_its_refusal_reaches_the_agent(adapter, simulator, monkeypatch):
    monkeypatch.setattr(adapter, "simulator_origin", lambda: simulator.origin)
    refusal = {"error": {"code": "INVALID_SERVICE_CONTEXT", "message": "Invalid service context."}}
    simulator.replies.append((401, {"Content-Type": "application/json"}, json.dumps(refusal).encode()))
    assert adapter.handler({"trusted_context": "forged-context", "payload": "{}"}, tool_context()) == refusal


@pytest.mark.parametrize("event, context", [
    ({"trusted_context": "signed-context", "payload": "{}"}, tool_context("simulation-service___delete_everything")),
    ({"trusted_context": "signed-context", "payload": "{}"}, tool_context(None)),
    ({"trusted_context": "signed-context", "payload": "{}"}, SimpleNamespace(client_context=None)),
    ({"trusted_context": "", "payload": "{}"}, tool_context()),
    ({"trusted_context": {"alg": "none"}, "payload": "{}"}, tool_context()),
    ({"payload": "{}"}, tool_context()),
    ({"trusted_context": "signed-context", "payload": "[]"}, tool_context()),
    ({"trusted_context": "signed-context", "payload": "not json"}, tool_context()),
    ({"trusted_context": "signed-context"}, tool_context()),
])
def test_malformed_tool_calls_are_refused_before_any_network_call(adapter, monkeypatch, event, context):
    monkeypatch.setattr(adapter, "opener", lambda: pytest.fail("a malformed call must not reach the simulator"))
    assert adapter.handler(event, context) == {
        "error": {"code": "INVALID_SERVICE_CONTEXT", "message": "Invalid simulation request."}}


def test_the_adapter_never_reads_the_signing_secret():
    source = (GATEWAY / "simulation/handler.py").read_text()
    for name in ("SIMULATION_CONTEXT_SECRET", "secretsmanager", "import boto3", "hmac"):
        assert name not in source


def test_the_runtime_and_the_broker_stop_a_simulation_turn_at_the_same_tool_call():
    """Both ends of this adapter allow 12 tool calls per simulation turn (architecture/02), each a named bound."""
    from backend.evacuation.agent_broker import MAX_TOOL_CALLS_PER_TURN
    spec = importlib.util.spec_from_file_location("runtime_limits", GATEWAY.parent / "runtime/utils/limits.py")
    limits = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(limits)
    assert limits.SIMULATION_TOOL_CALLS == MAX_TOOL_CALLS_PER_TURN == 12
