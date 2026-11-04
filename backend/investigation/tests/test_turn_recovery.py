"""Accepted chat turns survive transport loss without repeating paid work."""
import asyncio
import json
import queue
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import commit, digest
from traffic_simulator.store import Store

from backend import fire_risk_broker, runtime_client, trusted_context
from backend.investigation import broker, saved
from backend.investigation.contracts import InvestigationRequest
from backend.tests.builders import client

SNAPSHOT = {"type": "structuredResponse", "content": {
    "response": "Reported source observations.",
    "location": {"name": "Fixture point", "latitude": 38.5, "longitude": -121.5},
    "mapData": {"summary": {"totalActiveFires": 0}, "locations": []},
}}


@pytest.fixture
def turn_setup(tmp_path, monkeypatch):
    store, queued, paid, calls = Store(tmp_path), [], [], []
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(broker, "investigation_slots", lambda: slots)
    monkeypatch.setattr(trusted_context, "sign", lambda *_: "signed-fixture")
    monkeypatch.setattr(runtime_client, "spend_agent_request", lambda *_: paid.append(True))
    monkeypatch.setattr(fire_risk_broker, "enrich_answer", lambda *_: SNAPSHOT["content"])
    monkeypatch.setenv("SIMULATION_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fixture")

    def events(payload, _deadline):
        calls.append(payload)
        yield {"type": "RUN_STARTED"}
        yield {"type": "STATE_SNAPSHOT", "snapshot": SNAPSHOT}
        yield {"type": "RUN_FINISHED"}

    class Worker:
        def __init__(self, *, target, daemon):
            self.target = target

        def start(self):
            queued.append(self.target)

    monkeypatch.setattr(runtime_client, "runtime_events", events)
    monkeypatch.setattr(broker, "threading", SimpleNamespace(Thread=Worker, Event=threading.Event))
    return store, queued, paid, calls, slots


def start(store, body, owner="alice"):
    return asyncio.run(broker.investigation_turn(store, owner, body))


def frames(response):
    async def collect():
        return "".join([part async for part in response.body_iterator])
    return [json.loads(line[6:]) for line in asyncio.run(collect()).splitlines() if line.startswith("data: ")]


def test_general_and_saved_investigations_share_one_process_bound():
    assert broker.investigation_slots is saved.investigation_slots


def test_disconnect_and_rejoin_retains_final_answer_without_another_paid_call(turn_setup):
    store, queued, paid, calls, slots = turn_setup
    body = InvestigationRequest(thread_id="conversation", question="Reported fires?", request_id=uuid4())
    response = start(store, body)

    async def disconnect():
        assert await response.body_iterator.__anext__() == ": heartbeat\n\n"
        await response.body_iterator.aclose()
    asyncio.run(disconnect())
    assert not slots.acquire(blocking=False), "Disconnect cannot release a still-running paid invocation"
    queued.pop()()
    receipt = broker.turn_view(broker.read_turn(store, "alice", body.request_id))
    assert receipt["status"] == "completed" and receipt["snapshot"] == SNAPSHOT
    replayed = frames(start(store, body))
    assert [event["type"] for event in replayed] == ["STATE_SNAPSHOT", "RUN_FINISHED"]
    assert replayed[0]["snapshot"] == receipt["snapshot"]
    assert len(paid) == len(calls) == 1 and not queued
    assert slots.acquire(blocking=False)


def burst_worker(turn_setup, monkeypatch, *, on_full=None):
    """Fill the real broker's bounded channel before allowing its connected reader to drain it."""
    store, queued, paid, calls, slots = turn_setup
    filled = threading.Event()
    channels = []

    class ObservedQueue(queue.Queue):
        def __init__(self, maxsize):
            super().__init__(maxsize)
            self.maximum = 0
            channels.append(self)

        def put(self, item, block=True, timeout=None):
            super().put(item, block=block, timeout=timeout)
            self.maximum = max(self.maximum, self.qsize())
            if self.qsize() == self.maxsize and not filled.is_set():
                if on_full:
                    on_full()
                filled.set()

    args = json.dumps({"query": "Use reviewed public guidance and retained source observations."})
    chunks = list(args)
    initial = [
        {"type": "RUN_STARTED"},
        {"type": "CUSTOM", "name": "runtime_execution", "value": {"build_id": "fixture-build"}},
        {"type": "TOOL_CALL_START", "toolCallName": "public_response_agent_tool", "toolCallId": "guidance"},
    ]
    progress = [{"type": "TOOL_CALL_ARGS", "toolCallId": "guidance", "delta": char} for char in chunks]
    result = {"type": "TOOL_CALL_RESULT", "toolCallId": "guidance",
              "content": json.dumps({"status": "success", "result": "Retained guidance."})}
    events = [*initial, *progress, result, {"type": "STATE_SNAPSHOT", "snapshot": SNAPSHOT},
              {"type": "RUN_FINISHED"}]

    def runtime_events(payload, _deadline):
        calls.append(payload)
        yield from events

    monkeypatch.setattr(broker, "queue", SimpleNamespace(Queue=ObservedQueue, Full=queue.Full))
    monkeypatch.setattr(runtime_client, "runtime_events", runtime_events)
    body = InvestigationRequest(thread_id="conversation", question="Response guidance?", request_id=uuid4())
    response = start(store, body)
    worker = threading.Thread(target=queued.pop(), daemon=True)
    worker.start()
    assert filled.wait(2), "Exercise actual bounded-channel saturation before reading the stream."
    return store, body, response, worker, events, channels, paid, calls, slots


def test_connected_burst_preserves_tool_identity_arguments_results_and_terminal(turn_setup, monkeypatch):
    store, body, response, worker, events, channels, paid, calls, slots = burst_worker(turn_setup, monkeypatch)
    delivered = frames(response)
    worker.join(2)
    assert not worker.is_alive()
    # The broker publishes its enriched authoritative snapshot immediately before the Runtime terminal frame.
    assert delivered == [*events[:-1], {"type": "STATE_SNAPSHOT", "snapshot": SNAPSHOT}, events[-1]]
    args = "".join(event["delta"] for event in delivered if event["type"] == "TOOL_CALL_ARGS")
    assert json.loads(args)["query"] == "Use reviewed public guidance and retained source observations."
    assert channels[0].maxsize == 64 and channels[0].maximum <= 64
    assert broker.read_turn(store, "alice", body.request_id)["status"] == "completed"
    assert len(paid) == len(calls) == 1 and slots.acquire(blocking=False)


def test_disconnect_unblocks_a_full_channel_and_retains_the_same_paid_answer(turn_setup, monkeypatch):
    store, body, response, worker, _, channels, paid, calls, slots = burst_worker(turn_setup, monkeypatch)

    async def disconnect():
        first = await response.body_iterator.__anext__()
        assert json.loads(first[6:])["type"] == "RUN_STARTED"
        await response.body_iterator.aclose()

    asyncio.run(disconnect())
    worker.join(2)
    assert not worker.is_alive(), "A retired full stream cannot wedge the paid worker's cleanup."
    receipt = broker.read_turn(store, "alice", body.request_id)
    assert receipt["status"] == "completed" and receipt["snapshot"] == SNAPSHOT
    assert frames(start(store, body))[0]["snapshot"] == SNAPSHOT
    assert channels[0].maximum <= 64 and len(paid) == len(calls) == 1
    assert slots.acquire(blocking=False)


def test_delivery_deadline_retires_a_full_channel_without_wedging_cleanup(turn_setup, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(broker, "time", SimpleNamespace(time=lambda: clock[0]))

    def expire():
        clock[0] += broker.TURN_SECONDS + 1

    store, body, _, worker, _, channels, paid, calls, slots = burst_worker(
        turn_setup, monkeypatch, on_full=expire)
    worker.join(2)
    assert not worker.is_alive(), "Terminal/sentinel delivery must stop at the original lease deadline."
    receipt = broker.read_turn(store, "alice", body.request_id)
    assert receipt["status"] == "interrupted" and receipt["snapshot"] is None
    assert store.get("investigation_lease:" + digest("alice"))["lease_until"] == 0
    assert channels[0].maximum <= 64 and len(paid) == len(calls) == 1
    assert slots.acquire(blocking=False)


def test_pending_duplicate_rejoins_and_conflicting_request_identity_is_refused(turn_setup):
    store, queued, paid, calls, _ = turn_setup
    body = InvestigationRequest(thread_id="conversation", question="Reported fires?", request_id=uuid4())
    start(store, body)
    start(store, body)
    assert len(queued) == len(paid) == 1 and not calls
    with pytest.raises(IntegrationError) as refused:
        start(store, body.model_copy(update={"question": "A different paid request."}))
    assert refused.value.code == "IDEMPOTENCY_CONFLICT"


def test_busy_process_refuses_another_owner_before_receipt_or_billing(turn_setup):
    store, queued, paid, _, _ = turn_setup
    body = InvestigationRequest(thread_id="conversation", question="Reported fires?", request_id=uuid4())
    start(store, body)
    other = body.model_copy(update={"request_id": uuid4()})
    with pytest.raises(IntegrationError) as refused:
        start(store, other, owner="bob")
    assert refused.value.code == "INVESTIGATION_BUSY" and refused.value.retry_after == 30
    assert store.get(f"investigation_turn:{broker.turn_id('bob', other.request_id)}") is None
    assert len(paid) == len(queued) == 1
    with pytest.raises(IntegrationError) as hidden:
        broker.read_turn(store, "bob", body.request_id)
    assert hidden.value.code == "NOT_FOUND"


def test_expired_receipt_is_interrupted_and_fences_the_late_worker(turn_setup):
    store, queued, paid, _, slots = turn_setup
    body = InvestigationRequest(thread_id="conversation", question="Reported fires?", request_id=uuid4())
    start(store, body)
    key = f"investigation_turn:{broker.turn_id('alice', body.request_id)}"
    receipt = store.get(key)
    revision = receipt["revision"]
    receipt.update(lease_until=time.time()-1, revision=revision+1)
    commit(store, [(receipt, revision)])
    interrupted = broker.read_turn(store, "alice", body.request_id)
    assert interrupted["status"] == "interrupted" and interrupted["snapshot"] is None
    assert not slots.acquire(blocking=False), "Expiry fences publication but must not free a real worker slot"
    queued.pop()()
    assert store.get(key)["status"] == "interrupted" and store.get(key)["snapshot"] is None
    assert len(paid) == 1 and slots.acquire(blocking=False)
    response = frames(start(store, body))
    assert response[0]["type"] == "RUN_ERROR" and not queued and len(paid) == 1


def test_thread_start_failure_publishes_failure_and_releases_both_bounds(turn_setup, monkeypatch):
    store, _, paid, calls, slots = turn_setup

    class FailedWorker:
        def __init__(self, **_):
            pass

        def start(self):
            raise OSError("Synthetic thread setup failure")
    monkeypatch.setattr(broker, "threading", SimpleNamespace(Thread=FailedWorker, Event=threading.Event))
    body = InvestigationRequest(thread_id="conversation", question="Reported fires?", request_id=uuid4())
    with pytest.raises(OSError):
        start(store, body)
    receipt = broker.read_turn(store, "alice", body.request_id)
    assert receipt["status"] == "failed" and receipt["snapshot"] is None
    assert store.get("investigation_lease:"+digest("alice"))["lease_until"] == 0
    assert len(paid) == 1 and not calls and slots.acquire(blocking=False)


def test_missing_final_snapshot_is_failure_not_a_completed_turn(turn_setup, monkeypatch):
    store, queued, _, _, _ = turn_setup
    monkeypatch.setattr(runtime_client, "runtime_events", lambda *_: iter([{"type": "RUN_FINISHED"}]))
    body = InvestigationRequest(thread_id="conversation", question="Reported fires?", request_id=uuid4())
    start(store, body)
    queued.pop()()
    receipt = broker.read_turn(store, "alice", body.request_id)
    assert receipt["status"] == "failed" and receipt["snapshot"] is None


def test_oversized_final_answer_retains_small_failure_receipt_and_releases_capacity(turn_setup, monkeypatch):
    store, queued, paid, calls, slots = turn_setup
    oversized = {**SNAPSHOT, "content": {**SNAPSHOT["content"], "response": "x" * broker.MAX_TURN_RECEIPT_BYTES}}
    monkeypatch.setattr(fire_risk_broker, "enrich_answer", lambda *_: oversized["content"])
    body = InvestigationRequest(thread_id="conversation", question="Reported fires?", request_id=uuid4())
    start(store, body)
    queued.pop()()
    receipt = broker.read_turn(store, "alice", body.request_id)
    assert receipt["status"] == "failed" and receipt["snapshot"] is None
    assert receipt["error"]["code"] == "INVESTIGATION_FAILED"
    assert len(json.dumps(receipt).encode()) < broker.MAX_TURN_RECEIPT_BYTES
    assert len(paid) == len(calls) == 1 and slots.acquire(blocking=False)
    assert store.get("investigation_lease:" + digest("alice"))["lease_until"] == 0


def test_receipt_http_contract_returns_final_snapshot_and_hides_other_owners(turn_setup, api_app):
    store, queued, paid, calls, _ = turn_setup
    body = InvestigationRequest(thread_id="conversation", question="Reported fires?", request_id=uuid4())
    start(store, body)
    path = f"/api/v1/investigation/turns/{body.request_id}"
    with client(api_app(store, "alice", broker.create_investigation_router)) as browser:
        pending = browser.get(path)
        assert pending.status_code == 200 and pending.json()["status"] == "running"
        assert pending.json()["snapshot"] is None and pending.json()["error"] is None
        queued.pop()()
        completed = browser.get(path)
        assert completed.status_code == 200 and completed.json()["status"] == "completed"
        assert completed.json()["snapshot"] == SNAPSHOT
        assert completed.json()["request_id"] == str(body.request_id)
    with client(api_app(store, "bob", broker.create_investigation_router)) as browser:
        hidden = browser.get(path)
        assert hidden.status_code == 404 and hidden.json()["error"]["code"] == "NOT_FOUND"
        assert str(body.request_id) not in hidden.text and "Fixture point" not in hidden.text
    assert len(paid) == len(calls) == 1
