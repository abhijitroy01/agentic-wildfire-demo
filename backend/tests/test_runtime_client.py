import json
import logging
import socket
import sqlite3
import time
from http.client import HTTPResponse
from types import SimpleNamespace
from uuid import uuid4

import pytest
from botocore.exceptions import BotoCoreError, ClientError, ReadTimeoutError
from botocore.response import StreamingBody
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import budget_day, commit, record
from traffic_simulator.store import Store
from urllib3.response import HTTPResponse as PoolResponse

from backend import leases, runtime_client

TOOLS = "/api/internal/v1/simulations/"
INVESTIGATION = {"question": "Show current weather.", "thread_id": "local-thread", "context_envelope": {}}
STORAGE_DETAIL = "fixture storage detail that stays out of the log"


def spent_agent_requests(store):
    """Return how many requests today's shared daily agent budget has counted (0 before the first)."""
    return (store.get(f"budget:agent_requests:{budget_day()[0]}") or {}).get("count", 0)


def test_incomplete_runtime_frame_and_heartbeat_bytes_are_bounded(monkeypatch):
    monkeypatch.setattr(runtime_client, "MAX_RUNTIME_FRAME_BYTES", 32)
    with pytest.raises(ValueError, match="frame"):
        list(runtime_client.events_from_bytes([b"data: "+b"x"*33]))
    monkeypatch.setattr(runtime_client, "MAX_RUNTIME_BYTES", 16)
    with pytest.raises(ValueError, match="stream"):
        list(runtime_client.events_from_bytes([b": ping\n\n"]*3))


def test_stream_frames_survive_utf8_crlf_and_byte_boundaries():
    raw = ': heartbeat\r\n\r\ndata: {"type":"TEXT_MESSAGE_CONTENT","delta":"🔥"}\r\n\r\ndata: {"type":"RUN_FINISHED"}\n\n'
    assert list(runtime_client.events_from_bytes(bytes([byte]) for byte in raw.encode())) == [
        {"type": "TEXT_MESSAGE_CONTENT", "delta": "🔥"}, {"type": "RUN_FINISHED"}]


@pytest.mark.parametrize("chunked", [False, True])
def test_real_sdk_body_closes_at_terminal_without_an_eof_socket_read(monkeypatch, chunked, runtime_binding):
    """The native SDK releases a Content-Length socket before the next read."""
    left, right = socket.socketpair()
    data = b'data: {"type":"STATE_SNAPSHOT","snapshot":{}}\n\ndata: {"type":"RUN_FINISHED"}\n\n'
    headers = b"Transfer-Encoding: chunked" if chunked else b"Content-Length: "+str(len(data)).encode()
    wire = hex(len(data))[2:].encode()+b"\r\n"+data+b"\r\n0\r\n\r\n" if chunked else data
    right.sendall(b"HTTP/1.1 200 OK\r\n"+headers+b"\r\n\r\n"+wire)
    response = HTTPResponse(left)
    response.begin()
    raw = PoolResponse(body=response, headers=dict(response.getheaders()),
                       original_response=response, preload_content=False)
    body = StreamingBody(raw, None if chunked else len(data))
    client = SimpleNamespace(invoke_agent_runtime=lambda **_: {"response": body})
    monkeypatch.setattr(runtime_client.boto3, "client", lambda *_, **__: client)
    monkeypatch.setenv("SIMULATION_RUNTIME_ARN",
                       "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fixture")
    try:
        events = list(runtime_client.invoke_runtime({"threadId": str(uuid4())}, timeout_seconds=3))
        assert [event["type"] for event in events] == ["STATE_SNAPSHOT", "RUN_FINISHED"]
        assert raw.closed
    finally:
        body.close()
        left.close()
        right.close()


@pytest.mark.parametrize("ending", [
    [],
    [{"type": "RUN_ERROR"}],
    [{"type": "RUN_FINISHED", "outcome": {"type": "interrupt"}}],
])
def test_runtime_snapshot_without_successful_termination_fails_and_closes(monkeypatch, ending, runtime_binding):
    class Stream:
        closed = False
        def iter_chunks(self, **_kwargs):
            events = [{"type": "STATE_SNAPSHOT", "snapshot": {"content": {"response": "Unconfirmed"}}}, *ending]
            yield b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events)
        def close(self):
            self.closed = True

    stream = Stream()
    monkeypatch.setenv("SIMULATION_RUNTIME_ARN",
                       "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fixture")
    monkeypatch.setattr(runtime_client.boto3, "client", lambda *_, **__: SimpleNamespace(
        invoke_agent_runtime=lambda **_: {"response": stream}))
    with pytest.raises(RuntimeError, match="Runtime"):
        list(runtime_client.invoke_runtime({"threadId": str(uuid4())}))
    assert stream.closed


@pytest.mark.parametrize("chunked", [False, True])
def test_real_sdk_silent_body_ends_at_deadline_and_closes(monkeypatch, chunked, runtime_binding):
    left, right = socket.socketpair()
    headers = b"Transfer-Encoding: chunked" if chunked else b"Content-Length: 100"
    right.sendall(b"HTTP/1.1 200 OK\r\n"+headers+b"\r\n\r\n")
    response = HTTPResponse(left)
    response.begin()
    raw = PoolResponse(body=response, headers=dict(response.getheaders()),
                       original_response=response, preload_content=False)
    body = StreamingBody(raw, None if chunked else 100)
    monkeypatch.setenv("SIMULATION_RUNTIME_ARN",
                       "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fixture")
    monkeypatch.setattr(runtime_client.boto3, "client", lambda *_, **__: SimpleNamespace(
        invoke_agent_runtime=lambda **_: {"response": body}))
    began = time.monotonic()
    try:
        with pytest.raises((ReadTimeoutError, TimeoutError)):
            list(runtime_client.invoke_runtime({"threadId": str(uuid4())}, timeout_seconds=0.05))
        assert raw.closed and time.monotonic()-began < 3
    finally:
        body.close()
        left.close()
        right.close()


def test_runtime_version_changes_execution_but_never_the_signed_memory_thread(monkeypatch, runtime_binding):
    requests = []
    class Stream:
        def iter_chunks(self, **_kwargs):
            yield b'data: {"type":"RUN_STARTED"}\n\ndata: {"type":"RUN_FINISHED"}\n\n'
        def close(self):
            pass
    def invoke(**kwargs):
        requests.append(kwargs)
        return {"response": Stream(), "runtimeSessionId": kwargs["runtimeSessionId"]}
    monkeypatch.setattr(runtime_client.boto3, "client",
                        lambda *_, **__: SimpleNamespace(invoke_agent_runtime=invoke))
    monkeypatch.setenv("SIMULATION_RUNTIME_ARN",
                       "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fixture")
    payload = {"threadId": str(uuid4()), "forwardedProps": {"investigation_context": "signed-unchanged"}}
    first = list(runtime_client.invoke_runtime(payload))
    list(runtime_client.invoke_runtime(payload))
    monkeypatch.setenv("SIMULATION_RUNTIME_ENDPOINT", "RELEASE_B")
    monkeypatch.setenv("SIMULATION_RUNTIME_VERSION", "40")
    second = list(runtime_client.invoke_runtime(payload))
    assert requests[0]["runtimeSessionId"] == requests[1]["runtimeSessionId"] != requests[2]["runtimeSessionId"]
    assert requests[0]["qualifier"] == "RELEASE_A" and requests[2]["qualifier"] == "RELEASE_B"
    assert all(json.loads(r["payload"]) == payload for r in requests)
    identities = [next(e["value"] for e in events if e.get("name") == "runtime_binding")
                  for events in (first, second)]
    assert identities[0]["conversation_id"] == identities[1]["conversation_id"] == payload["threadId"]
    # Rebinding for rollback restores the prior execution namespace without erasing newer Memory messages.
    monkeypatch.setenv("SIMULATION_RUNTIME_ENDPOINT", "RELEASE_A")
    monkeypatch.setenv("SIMULATION_RUNTIME_VERSION", "39")
    list(runtime_client.invoke_runtime(payload))
    assert requests[-1]["runtimeSessionId"] == requests[0]["runtimeSessionId"]


@pytest.mark.parametrize("endpoint,version", [("", "39"), ("DEFAULT", "39"), ("RELEASE_A", ""),
                                           ("RELEASE_A", "latest"), ("RELEASE_B", "0")])
def test_unpinned_runtime_binding_is_refused_before_invocation(monkeypatch, endpoint, version):
    monkeypatch.setenv("SIMULATION_RUNTIME_ARN",
                       "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fixture")
    monkeypatch.setenv("SIMULATION_RUNTIME_ENDPOINT", endpoint)
    monkeypatch.setenv("SIMULATION_RUNTIME_VERSION", version)
    monkeypatch.setattr(runtime_client.boto3, "client", lambda *_, **__: pytest.fail("Unexpected AWS call"))
    with pytest.raises(IntegrationError, match="versioned"):
        next(runtime_client.invoke_runtime({"threadId": str(uuid4())}))


@pytest.mark.parametrize("failure", [
    ClientError({"Error": {"Code": "InternalServerError", "Message": STORAGE_DETAIL}}, "TransactWriteItems"),
    BotoCoreError(),
    sqlite3.OperationalError(STORAGE_DETAIL),
    IntegrationError("REVISION_CONFLICT", STORAGE_DETAIL, 409),
], ids=lambda failure: type(failure).__name__)
def test_a_lease_release_that_fails_in_storage_logs_its_type_and_returns(tmp_path, monkeypatch, caplog, failure):
    """A storage error while a lease is released is logged by its type alone, and release returns normally."""
    store = Store(tmp_path)
    commit(store, [(record("turn", "fixture-turn", "alice", attempt_id="fixture-attempt",
                           lease_until=time.time() + 100), None)])

    def fail(*_args):
        raise failure

    monkeypatch.setattr(leases, "commit", fail)
    caplog.set_level(logging.WARNING, logger="backend.leases")
    assert leases.release(store, "turn", "fixture-turn", "alice", "fixture-attempt") is None
    assert [(entry.name, entry.levelname, entry.getMessage()) for entry in caplog.records] == [
        ("backend.leases", "WARNING", f"A turn lease was not released: reason={type(failure).__name__}")]
    assert STORAGE_DETAIL not in caplog.text
