import json
import logging
import time
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from fastapi.testclient import TestClient
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import budget_day, commit, digest, owned, record
from traffic_simulator.store import Store

from backend import runtime_client, simulation_jobs, trusted_context
from backend.evacuation import agent_broker, service
from backend.evacuation.contracts import EvidenceResponse, SubmitRequest
from backend.tests.builders import SECRET, claims_for, forged, ledger_result, prepared

TOOLS = "/api/internal/v1/simulations/"
INVESTIGATION = {"question": "Show current weather.", "thread_id": "local-thread", "context_envelope": {}}
STORAGE_DETAIL = "fixture storage detail that stays out of the log"


def spent_agent_requests(store):
    """Return how many requests today's shared daily agent budget has counted (0 before the first)."""
    return (store.get(f"budget:agent_requests:{budget_day()[0]}") or {}).get("count", 0)


def test_stream_uses_authoritative_receipt_and_replays_without_second_invocation(
        tmp_path, monkeypatch, signing_secret, api_app):
    """An agent-run submission uses the saved receipt, replays without a second Runtime call and leaks no secret."""
    store = Store(tmp_path)
    session, incident, draft = prepared(store)
    monkeypatch.setattr("traffic_simulator.traffic.configured", lambda: True)
    invoked = []

    def invoke(payload, *, timeout_seconds):
        assert 0 < timeout_seconds <= 210
        invoked.append(payload)
        selection = payload["forwardedProps"]["simulation_selection"]
        assert selection["draft_revision"] == draft["draft_revision"] == 2
        assert selection["draft"]["status"] == "ready"
        claims = trusted_context.verify(payload["forwardedProps"]["simulation_context"], "simulation")
        assert claims["purpose"] == "simulation" and claims["turn_id"] == payload["runId"]
        agent_broker.execute_tool(store, "submit_simulation", {
            "draft_id": draft["draft_id"], "draft_revision": draft["draft_revision"]}, claims)
        yield {"type": "STATE_SNAPSHOT", "snapshot": {"content": {
            "response": "Run submitted.", "simulationData": {"run_id": str(uuid4())}}}}
    monkeypatch.setattr(runtime_client, "invoke_runtime", invoke)
    app = api_app(store, "alice", agent_broker.create_broker_router)
    body = {"request_id": str(uuid4()), "session_id": session["session_id"], "incident_id": incident["incident_id"],
            "location_revision": 1, "question": "Run this draft.", "intent": "submit", "draft_id": draft["draft_id"]}
    with TestClient(app) as client:
        first = client.post("/api/v1/agent/turns", json=body)
        assert first.status_code == 200, first.text
        frames = [json.loads(line[6:]) for line in first.text.splitlines() if line.startswith("data: ")]
        output = next(f["snapshot"]["content"]["simulationData"] for f in frames if f["type"] == "STATE_SNAPSHOT")
        assert output["run"]["run_id"] == store.list("alice")[0]["id"]
        assert output["status"] == "submitted"
        assert "trusted_context" not in first.text and "fixture-secret" not in first.text
        again = client.post("/api/v1/agent/turns", json=body)
        assert again.status_code == 200 and output["run"]["run_id"] in again.text
        assert len(invoked) == 1 and len([j for j in store.list("alice") if j.get("kind") == "run"]) == 1
        conflict = client.post("/api/v1/agent/turns", json={**body, "question": "Different request"})
        assert conflict.status_code == 409


def test_connection_failure_after_submission_preserves_receipt(tmp_path, monkeypatch, signing_secret, caplog,
                                                               api_app):
    """A Runtime disconnect after a tool call still completes the turn with the saved run receipt."""
    caplog.set_level(logging.WARNING, logger="backend.evacuation.agent_broker")
    store = Store(tmp_path)
    session, incident, draft = prepared(store)
    monkeypatch.setattr("traffic_simulator.traffic.configured", lambda: True)

    def invoke(payload, **_kwargs):
        claims = trusted_context.verify(payload["forwardedProps"]["simulation_context"], "simulation")
        agent_broker.execute_tool(store, "submit_simulation", {
            "draft_id": draft["draft_id"], "draft_revision": draft["draft_revision"]}, claims)
        raise ConnectionError("fixture disconnect")
        yield  # pragma: no cover
    monkeypatch.setattr(runtime_client, "invoke_runtime", invoke)
    app = api_app(store, "alice", agent_broker.create_broker_router)
    with TestClient(app) as client:
        response = client.post("/api/v1/agent/turns", json={
            "request_id": str(uuid4()), "session_id": session["session_id"],
            "incident_id": incident["incident_id"], "location_revision": 1,
            "question": "Run this draft.", "intent": "submit", "draft_id": draft["draft_id"]})
        assert response.status_code == 200
        assert store.list("alice")[0]["id"] in response.text
        assert '"status": "submitted"' in response.text
        assert "connection failed" in response.text
    # The server log names the failure by type only.
    assert "lost its Runtime stream: reason=ConnectionError" in caplog.text
    assert "fixture disconnect" not in caplog.text


def test_replaced_turn_attempt_cannot_call_tools_or_publish(tmp_path):
    store = Store(tmp_path)
    session, incident, _ = prepared(store)
    turn_id = str(uuid4())
    turn = record("turn", turn_id, "alice", status="running", attempt_id="new-attempt",
                  lease_until=time.time()+100, effects=[])
    commit(store, [(turn, None)])
    claims = {"sub": "alice", "session_id": session["session_id"], "incident_id": incident["incident_id"],
              "location_revision": 1, "turn_id": turn_id, "attempt_id": "old-attempt",
              "allowed_operations": ["get_simulation_status"]}
    with pytest.raises(IntegrationError, match="ended"):
        agent_broker.execute_tool(store, "get_simulation_status", {"run_id": str(uuid4())}, claims)
    with pytest.raises(IntegrationError, match="newer attempt"):
        agent_broker.complete_turn(store, "alice", turn_id, "stale answer", attempt_id="old-attempt")
    assert owned(store, "turn", turn_id, "alice")["status"] == "running"


def test_failed_answer_persistence_closes_stream(tmp_path, monkeypatch, signing_secret, caplog, api_app):
    """A storage failure while saving the answer ends the browser stream with RUN_ERROR and no detail."""
    caplog.set_level(logging.WARNING, logger="backend.evacuation.agent_broker")
    store = Store(tmp_path)
    session, incident, _ = prepared(store)
    monkeypatch.setattr(runtime_client, "invoke_runtime", lambda payload, **_kwargs: iter([]))

    def fail(*args, **kwargs):
        raise RuntimeError("fixture persistence failure")
    monkeypatch.setattr(agent_broker, "complete_turn", fail)
    app = api_app(store, "alice", agent_broker.create_broker_router)
    with TestClient(app) as client:
        response = client.post("/api/v1/agent/turns", json={
            "request_id": str(uuid4()), "session_id": session["session_id"],
            "incident_id": incident["incident_id"], "location_revision": 1,
            "question": "What can this simulator do?"})
        assert "RUN_ERROR" in response.text
        assert "fixture persistence failure" not in response.text
    assert "answer was not saved: reason=RuntimeError" in caplog.text
    assert "fixture persistence failure" not in caplog.text


def test_saved_json_evidence_can_complete_and_replay_without_duplicate_pages(tmp_path):
    store = Store(tmp_path)
    session, incident, _ = prepared(store)
    run_id, evidence_id, turn_id = (str(uuid4()) for _ in range(3))
    page = {
        "evidence_id": evidence_id, "view": "outcomes", "availability": "available",
        "artifact_refs": [{"run_id": run_id, "sha256": "a"*64, "artifact_schema_version": 3}],
        "facts": [{"fact_id": "unresolved", "run_id": run_id, "metric": "unresolved_vehicles",
                   "value": 49, "availability": "available", "derivation": "Requested minus exited",
                   "source_fields": ["scenario.vehicles", "summary.exited_vehicles"]}],
        "comparisons": [], "missing_reasons": [],
        "pagination": {"next_cursor": None, "has_more": False, "total_items": 1,
                       "returned_items": 1, "offset": 0, "byte_limit": 32768},
    }
    request = {"session_id": session["session_id"], "incident_id": incident["incident_id"],
               "location_revision": 1, "question": "Who remains unresolved?"}
    turn = record("turn", turn_id, "alice", status="running", request=request,
                  effects=[{"operation": "get_simulation_evidence", "result": page, "selection": {}}])
    commit(store, [(turn, None)])
    output = agent_broker.complete_turn(store, "alice", turn_id, "49 vehicles remain unresolved.")
    assert output["evidence"][0]["facts"][0]["value"] == 49
    saved = store.get(f"turn:{turn_id}")
    assert saved["effects"] == [{"operation": "get_simulation_evidence", "selection": {},
                                 "output_evidence_index": 0}]
    assert agent_broker.complete_turn(store, "alice", turn_id, "Retry") == output
    for path in ("evidence_id", "run_id"):
        invalid = json.loads(json.dumps(page))
        if path == "evidence_id":
            invalid[path] = "not-a-uuid"
        else:
            invalid["facts"][0][path] = "not-a-uuid"
        with pytest.raises(ValueError):
            EvidenceResponse.model_validate(invalid)


@pytest.mark.parametrize(("size", "accepted"), [(200 * 1024, True), (256 * 1024, True), (256 * 1024 + 1, False)])
def test_a_simulation_turn_saves_evidence_to_256_kib_and_refuses_the_read_above(tmp_path, monkeypatch, size,
                                                                               accepted):
    """A turn's saved tool effects may reach 256 KiB, under DynamoDB's 400 KB item; a read past it gets 409.

    The turn has already read pages (one earlier effect holds their bytes); its next read is a real outcomes page.
    """
    store = Store(tmp_path)
    session, incident, _ = prepared(store)
    run_id = str(uuid4())
    monkeypatch.setattr(simulation_jobs, "owned_job", lambda *_: {
        "id": run_id, "session_id": session["session_id"], "incident_id": incident["incident_id"],
        "location_revision": 1})
    monkeypatch.setattr(agent_broker, "result_for", lambda *_: ledger_result())
    read = {"view": "outcomes", "run_ids": [run_id]}

    def running_turn(padding):
        """Save a running turn whose earlier evidence reads hold `padding` bytes; return its claims."""
        turn_id = str(uuid4())
        earlier = {"operation": "get_simulation_evidence", "result": {"facts": "x"*padding}, "selection": {}}
        commit(store, [(record("turn", turn_id, "alice", status="running", attempt_id="attempt",
                               lease_until=time.time()+100, effects=[earlier]), None)])
        return {"sub": "alice", "session_id": session["session_id"], "incident_id": incident["incident_id"],
                "location_revision": 1, "turn_id": turn_id, "attempt_id": "attempt",
                "allowed_operations": ["get_simulation_evidence"]}

    def saved_effects(claims):
        return owned(store, "turn", claims["turn_id"], "alice")["effects"]

    def measured(claims):
        """The turn's saved effects as the broker measures them."""
        return len(json.dumps(saved_effects(claims), ensure_ascii=False).encode())

    calibration = running_turn(0)
    agent_broker.execute_tool(store, "get_simulation_evidence", read, calibration)
    claims = running_turn(size - measured(calibration))
    if accepted:
        assert agent_broker.execute_tool(store, "get_simulation_evidence", read, claims)["view"] == "outcomes"
        assert measured(claims) == size
    else:
        with pytest.raises(IntegrationError) as error:
            agent_broker.execute_tool(store, "get_simulation_evidence", read, claims)
        assert (error.value.code, error.value.status) == ("TOOL_BUDGET_EXCEEDED", 409)
        assert len(saved_effects(claims)) == 1, "A refused read saves nothing"


def test_completed_run_analysis_deduplicates_across_refresh_request_ids(tmp_path, monkeypatch, signing_secret,
                                                                        api_app):
    """Analysis of one completed run calls the Runtime once, whatever request IDs a refreshed page sends."""
    store = Store(tmp_path)
    session, incident, draft = prepared(store)
    monkeypatch.setattr("traffic_simulator.traffic.configured", lambda: True)
    run = service.submit(store, "alice", SubmitRequest.model_validate({
        "request_id": str(uuid4()), "draft_id": draft["draft_id"], "draft_revision": draft["draft_revision"],
        "capability_version": "1.0.0"}))
    monkeypatch.setattr(agent_broker, "result_for", lambda *_: {"result": "retained-stable-fixture"})
    invoked = []

    def invoke(payload, **_kwargs):
        invoked.append(payload)
        yield {"type": "STATE_SNAPSHOT", "snapshot": {"content": {"response": "Cited comparison fixture."}}}
    monkeypatch.setattr(runtime_client, "invoke_runtime", invoke)
    app = api_app(store, "alice", agent_broker.create_broker_router)
    body = {"session_id": session["session_id"], "incident_id": incident["incident_id"],
            "location_revision": 1, "run_id": run["run_id"], "intent": "analyze"}
    with TestClient(app) as client:
        first = client.post("/api/v1/agent/turns", json={
            **body, "request_id": str(uuid4()), "question": "Compare.", "policy_id": "baseline"})
        second = client.post("/api/v1/agent/turns", json={
            **body, "request_id": str(uuid4()), "question": "Refreshed.", "policy_id": "phased"})
        assert first.status_code == second.status_code == 200
        assert "Cited comparison fixture." in second.text
        assert len(invoked) == 1


def test_internal_tool_route_refuses_bad_contexts_and_is_unavailable_without_a_secret(
        tmp_path, monkeypatch, signing_secret, api_app):
    """The tool route answers 401 for a wrong-purpose or malformed context and 503 when no secret can be read."""
    store = Store(tmp_path)
    app = api_app(store, "alice", agent_broker.create_broker_router)
    tools = TestClient(app)
    monkeypatch.delenv("SIMULATION_CONTEXT_SECRET")
    signed = {"x-simulation-context": forged(claims_for("simulation"))}
    unavailable = tools.post(TOOLS + "get_simulation_status", json={}, headers=signed)
    assert unavailable.status_code == 503 and unavailable.json()["error"]["code"] == "AGENT_NOT_CONFIGURED"
    monkeypatch.setenv("SIMULATION_CONTEXT_SECRET", SECRET)
    for token in (forged(claims_for("investigation"), "investigation"),
                  forged(claims_for("firetruck_report"), "firetruck_report"),
                  forged(claims_for("simulation", exp=[1])), "W10.e30.c2ln"):
        refused = tools.post(TOOLS + "get_simulation_status", json={}, headers={"x-simulation-context": token})
        assert refused.status_code == 401 and refused.json()["error"]["code"] == "INVALID_SERVICE_CONTEXT"


def test_a_concurrent_agent_turn_that_loses_the_session_commit_spends_no_budget(tmp_path, monkeypatch,
                                                                                 signing_secret, api_app):
    """A turn whose session read went stale before its commit gets 409 and spends no daily budget unit."""
    store = Store(tmp_path)
    session, incident, _ = prepared(store)
    monkeypatch.setattr(runtime_client, "invoke_runtime", lambda *_, **__: pytest.fail("The losing turn ran"))
    key, read, stale = f"session:{session['session_id']}", store.get, []

    def before_the_other_turn(identifier):
        """Answer this request's first session read with the revision it had before another turn committed."""
        value = read(identifier)
        if identifier == key and not stale:
            stale.append(identifier)
            return {**value, "revision": value["revision"] - 1}
        return value

    monkeypatch.setattr(store, "get", before_the_other_turn)
    app = api_app(store, "alice", agent_broker.create_broker_router)
    response = TestClient(app).post("/api/v1/agent/turns", json={
        "request_id": str(uuid4()), "session_id": session["session_id"], "incident_id": incident["incident_id"],
        "location_revision": 1, "question": "What can this simulator do?"})
    assert response.status_code == 409 and response.json()["error"]["code"] == "REVISION_CONFLICT"
    assert stale and spent_agent_requests(store) == 0


def test_a_budget_refused_turn_reports_failed_on_both_turn_routes(tmp_path, monkeypatch, api_app):
    """A turn past the daily budget gets 429 and ends as failed with the refusal's code on both turn routes.

    Asking the same question again is refused by the budget again, not 409, and the turn ends the same way.
    """
    monkeypatch.setenv("AGENT_REQUESTS_PER_DAY", "0")
    monkeypatch.setattr(runtime_client, "invoke_runtime", lambda *_, **__: pytest.fail("A refused turn ran"))
    store = Store(tmp_path)
    session, incident, _ = prepared(store)
    app = api_app(store, "alice", agent_broker.create_broker_router)
    body = {"request_id": str(uuid4()), "session_id": session["session_id"], "incident_id": incident["incident_id"],
            "location_revision": 1, "question": "What can this simulator do?"}
    turn_id = str(uuid5(NAMESPACE_URL, digest(["alice", body["request_id"]])))
    failed = {"turn_id": turn_id, "status": "failed", "error_code": "DAILY_LIMIT_REACHED"}
    with TestClient(app) as turns:
        for _ in range(2):
            refused = turns.post("/api/v1/agent/turns", json=body)
            assert refused.status_code == 429 and refused.json()["error"]["code"] == "DAILY_LIMIT_REACHED"
            assert turns.get(f"/api/v1/agent/turns/{turn_id}").json() == failed
            assert turns.get(f"/api/v1/sessions/{session['session_id']}/turns").json() == [failed]
