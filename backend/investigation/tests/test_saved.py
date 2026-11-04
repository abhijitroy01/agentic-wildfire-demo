from functools import partial

from backend.evacuation import evidence as planning_evidence
from backend.tests.builders import investigation_readers

"""Workflow restoration, isolation and observable execution without cloud/model calls."""
import json
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import commit, owned
from traffic_simulator.store import Store

from backend import runtime_client, simulation_jobs, trusted_context
from backend.evacuation.contracts import EvidenceResponse
from backend.investigation import fire_observations
from backend.investigation import saved as workflow
from backend.investigation.contracts import IncidentInvestigationRequest
from backend.tests.builders import ledger_result, prepared


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = Store(tmp_path)
    _, incident, _ = prepared(store)
    calls, budgets, queued = [], [], []
    monkeypatch.setattr(trusted_context, "sign", lambda claims, purpose: calls.append((claims, purpose)) or "signed")
    monkeypatch.setattr(runtime_client, "spend_agent_request", lambda storage: budgets.append(True))
    slots = threading.BoundedSemaphore(4)
    monkeypatch.setattr(workflow, "investigation_slots", lambda: slots)

    class Worker:
        def __init__(self, target, args, daemon):
            queued.append((target, args))

        def start(self):
            pass

    # Module-local replacement avoids changing threads in TestClient.
    monkeypatch.setattr(workflow, "threading", type("Threads", (), {"Thread": Worker}))
    return store, incident, calls, budgets, queued


@pytest.mark.parametrize(("elapsed", "status"), [(300, "completed"), (430, "failed")])
def test_saved_investigation_has_its_own_bounded_final_response_deadline(setup, monkeypatch, elapsed, status):
    store, incident, _, _, queued = setup
    now = [time.time()]
    clock = SimpleNamespace(time=lambda: now[0])
    # The workflow sets the lease deadline and the Runtime consumer enforces it, both on this clock.
    monkeypatch.setattr(workflow, "time", clock)
    monkeypatch.setattr(runtime_client, "time", clock)

    def events(payload, **_kwargs):
        yield from invoke_events(payload)
        now[0] += elapsed
        yield {"type": "RUN_FINISHED"}

    monkeypatch.setattr(runtime_client, "invoke_runtime", events)
    workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    target, args = queued.pop()
    target(*args)
    assert workflow.read(store, incident, "alice")["status"] == status


def test_busy_process_refuses_new_work_before_recording_or_billing(setup, monkeypatch):
    store, incident, _, budgets, queued = setup
    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(workflow, "investigation_slots", lambda: slots)
    with pytest.raises(IntegrationError, match="capacity is busy") as error:
        workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    assert error.value.code == "INVESTIGATION_BUSY"
    assert not budgets and not queued
    assert workflow.read(store, incident, "alice") is None
    slots.release()
    monkeypatch.setattr(runtime_client, "invoke_runtime", invoke_events)
    workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    target, args = queued.pop()
    target(*args)
    assert slots.acquire(blocking=False), "Completion must release actual execution capacity"


def invoke_events(payload, **_kwargs):
    yield {"type": "RUN_STARTED"}
    yield {"type": "TOOL_CALL_START", "toolCallId": "detect", "toolCallName": "detection_agent_tool"}
    yield {"type": "TOOL_CALL_END", "toolCallId": "detect"}
    yield {"type": "TOOL_CALL_RESULT", "toolCallId": "detect", "content": "fixture"}
    yield {"type": "TOOL_CALL_START", "toolCallId": "guide", "toolCallName": "public_response_agent_tool"}
    yield {"type": "STATE_SNAPSHOT", "snapshot": {"type": "structuredResponse", "content": {
        "response": "Cited planning guidance", "knowledgeBaseCitations": [{"documentName": "Official fixture"}],
        "mapData": {"locations": []}, "forecastData": None}}}
    yield {"type": "RUN_FINISHED"}


def test_duplicate_start_rejoins_saved_work_and_reload_never_invokes(setup, monkeypatch):
    store, incident, calls, budgets, queued = setup
    monkeypatch.setattr(runtime_client, "invoke_runtime", invoke_events)
    first = workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    second = workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    assert first == second
    assert len(budgets) == len(queued) == 1
    assert workflow.read(store, incident, "alice")["status"] == "running"
    target, args = queued.pop()
    target(*args)
    saved = workflow.read(store, incident, "alice")
    assert saved["status"] == "completed"
    assert saved["activity"][0]["status"] == "result_received"
    assert saved["activity"][1]["status"] == "unverified", "No result event must never become success"
    assert saved["snapshot"]["content"]["response"] == "Cited planning guidance"
    assert calls[0][1] == "investigation" and calls[0][0]["allowed_operations"] == []
    assert workflow.start(store, "alice", incident, IncidentInvestigationRequest(retry=True), investigation_readers()) == saved
    assert len(budgets) == 1
    assert not any(item["kind"] == "incident_investigation" for item in store.pending())


def test_uncited_plan_is_failed_but_observations_remain_available(setup, monkeypatch):
    store, incident, _, _, queued = setup

    def unavailable(payload, **_kwargs):
        for event in invoke_events(payload):
            if event["type"] == "STATE_SNAPSHOT":
                event["snapshot"]["content"].update(knowledgeBaseCitations=None, response="Uncited invented plan",
                    mapData={"locations": [{"latitude": 39.75, "longitude": -121.6}]})
            yield event

    monkeypatch.setattr(runtime_client, "invoke_runtime", unavailable)
    workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    target, args = queued.pop()
    target(*args)
    saved = workflow.read(store, incident, "alice")
    assert saved["status"] == "failed"
    assert saved["snapshot"]["content"]["mapData"]["locations"]
    assert "Uncited invented plan" not in saved["snapshot"]["content"]["response"]
    assert "Guidance is unavailable" in saved["error"]


@pytest.mark.parametrize("ending", [
    [], [{"type": "RUN_ERROR", "message": "private provider credentials"}],
    [{"type": "RUN_FINISHED", "outcome": {"type": "interrupt"}}],
])
def test_snapshot_without_terminal_success_is_not_a_plan(setup, monkeypatch, ending):
    store, incident, _, _, queued = setup
    monkeypatch.setattr(runtime_client, "invoke_runtime", lambda payload, **_kwargs: iter([
        {"type": "STATE_SNAPSHOT", "snapshot": {"type": "structuredResponse", "content": {"response": "Unconfirmed"}}},
        *ending]))
    workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    target, args = queued.pop()
    target(*args)
    saved = workflow.read(store, incident, "alice")
    assert saved["status"] == "failed"
    assert saved["snapshot"]["content"]["knowledgeBaseCitations"] is None
    assert "did not complete" in saved["snapshot"]["content"]["response"]
    assert "credentials" not in saved["error"]


def test_cancel_fences_late_results_and_explicit_retry_uses_a_new_session(setup, monkeypatch):
    store, incident, calls, budgets, queued = setup
    monkeypatch.setattr(runtime_client, "invoke_runtime", invoke_events)
    first = workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    old = owned(store, "incident_investigation", first["task_id"], "alice")
    workflow.update(store, first["task_id"], "alice", old["attempt_id"], status="cancelled")
    workflow.start(store, "alice", incident, IncidentInvestigationRequest(retry=True), investigation_readers())
    new = owned(store, "incident_investigation", first["task_id"], "alice")
    assert new["attempt_id"] != old["attempt_id"]
    with pytest.raises(IntegrationError, match="ended"):
        workflow.update(store, first["task_id"], "alice", old["attempt_id"], status="completed")
    queued[-1][0](*queued[-1][1])
    assert workflow.read(store, incident, "alice")["status"] == "completed"
    assert len(budgets) == 2 and len(calls) == 1


def test_process_loss_becomes_interrupted_without_automatic_restart(setup):
    store, incident, _, budgets, queued = setup
    response = workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    value = owned(store, "incident_investigation", response["task_id"], "alice")
    revision = value["revision"]
    value.update(lease_until=time.time()-1, revision=revision+1)
    commit(store, [(value, revision)])
    assert workflow.read(store, incident, "alice")["status"] == "interrupted"
    assert workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())["status"] == "interrupted"
    assert len(budgets) == len(queued) == 1


def test_foreign_incident_and_client_telemetry_are_rejected(setup, api_app):
    store, incident, _, budgets, _ = setup
    app = api_app(store, "bob", partial(workflow.create_incident_investigation_router, readers=investigation_readers()))
    with TestClient(app) as client:
        url = f"/api/v1/incidents/{incident['incident_id']}/investigation"
        assert client.get(url).status_code == 404
        assert client.post(url, json={}).status_code == 404
        assert client.post(url, json={"evidence": {"crews_staged": 4}}).status_code == 422
    assert not budgets


def test_simulation_plan_uses_server_evidence_and_frozen_identity(setup, monkeypatch):
    store, incident, _, _, queued = setup
    run_id = str(uuid4())
    job = {"id": run_id, "incident_id": incident["incident_id"], "location_revision": 1}
    monkeypatch.setattr(simulation_jobs, "owned_job", lambda *_: job)
    monkeypatch.setattr(workflow, "evidence_context", lambda *_: {
        "run_id": run_id, "input_bundle_id": "server-bundle", "input_bundle_sha256": "a"*64,
        "evidence": [{"facts": [{"metric": "crews_not_arrived", "value": 2}]}]})
    task = workflow.start(store, "alice", incident, IncidentInvestigationRequest(run_id=run_id), investigation_readers())
    assert task["input_bundle_id"] == "server-bundle"
    captured = []
    monkeypatch.setattr(runtime_client, "invoke_runtime",
                        lambda payload, **_kwargs: captured.append(payload) or invoke_events(payload))
    queued[-1][0](*queued[-1][1])
    assert captured[0]["forwardedProps"]["context_envelope"]["evidence"][0]["facts"][0]["value"] == 2
    assert "across every policy" in captured[0]["forwardedProps"]["prompt"]
    assert "public_response_agent_tool" not in captured[0]["forwardedProps"]["prompt"]


def test_updated_context_keeps_observations_and_source_identity_without_duplicate_excerpts(setup, monkeypatch):
    store, incident, _, budgets, queued = setup
    citations = [{"documentName": f"Official source {n}", "sourceUrl": f"https://www.nps.gov/source{n}",
                  "retrievedAt": 123, "excerpt": "Published source passage. " * 24} for n in range(15)]
    observations = {"locations": [{"latitude": 39.75, "longitude": -121.6,
                                   "currentWeather": {"temperature": 82, "provider": "Fixture"}}]}
    forecast = {"days": [{"temperature": 85, "provider": "Fixture"}]}

    def initial_events(payload, **kwargs):
        for event in invoke_events(payload, **kwargs):
            if event["type"] == "STATE_SNAPSHOT":
                event["snapshot"]["content"].update(
                    knowledgeBaseCitations=citations, mapData=observations, forecastData=forecast)
            yield event

    monkeypatch.setattr(runtime_client, "invoke_runtime", initial_events)
    workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    target, args = queued.pop()
    target(*args)
    initial = workflow.read(store, incident, "alice")
    run_id = str(uuid4())
    job = {"id": run_id, "incident_id": incident["incident_id"], "location_revision": 1}
    result = ledger_result()
    monkeypatch.setattr(simulation_jobs, "owned_job", lambda *_: job)
    monkeypatch.setattr(workflow, "result_for", lambda *_: result)
    evidence = workflow.evidence_context(store, job, investigation_readers())
    # Reproduce a plan whose evidence fits, but forwarding the complete prior
    # retrieval text would exceed the same bounded request before any model call.
    evidence_bytes = len(json.dumps(evidence, separators=(",", ":")).encode())
    limit = evidence_bytes + len(json.dumps(initial["snapshot"]).encode()) // 2
    monkeypatch.setattr(workflow, "MAX_PLANNING_CONTEXT_BYTES", limit)
    uncompact = {**evidence, "verified_location": incident["location"], "initial_plan": initial["snapshot"]}
    assert len(json.dumps(uncompact, separators=(",", ":")).encode()) > limit
    task = workflow.start(store, "alice", incident, IncidentInvestigationRequest(run_id=run_id), investigation_readers())
    context = queued[-1][1][5]
    prior = context["initial_plan"]["content"]
    assert task["status"] == "running" and len(budgets) == 2
    assert len(json.dumps(context, separators=(",", ":")).encode()) <= limit
    assert context["evidence"] == evidence["evidence"]
    assert context["initial_plan_task_id"] == initial["task_id"]
    assert prior["mapData"] == observations and prior["forecastData"] == forecast
    assert prior["response"] == initial["snapshot"]["content"]["response"]
    assert prior["knowledgeBaseCitations"] == [
        {key: value for key, value in citation.items() if key != "excerpt"} for citation in citations]
    assert workflow.read(store, incident, "alice") == initial, "The original receipt must remain intact"


def test_observations_are_saved_before_guidance_finishes(setup, monkeypatch):
    store, incident, _, _, queued = setup
    observed = []

    def events(payload, **_kwargs):
        yield {"type": "STATE_SNAPSHOT", "snapshot": {"type": "structuredResponse", "content": {
            "response": "Observations", "mapData": {"locations": [
                {"latitude": 39.75, "longitude": -121.6, "currentWeather": {"provider": "Fixture"}}]}}}}
        saved = workflow.read(store, incident, "alice")
        observed.append(saved)
        yield {"type": "RUN_ERROR"}

    monkeypatch.setattr(runtime_client, "invoke_runtime", events)
    workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    queued[-1][0](*queued[-1][1])
    assert observed[0]["status"] == "running"
    assert observed[0]["snapshot"]["content"]["mapData"]["locations"]
    assert observed[0]["snapshot"]["content"]["knowledgeBaseCitations"] is None
    saved = workflow.read(store, incident, "alice")
    assert saved["status"] == "failed" and saved["snapshot"]["content"]["mapData"]["locations"]


def test_planning_pages_keep_all_facts_and_explicit_missing_values(setup, monkeypatch):
    _, _, _, _, _ = setup
    run_id = str(uuid4())
    retained = {"input_bundle_id": "retained", "input_bundle_sha256": "a"*64}
    monkeypatch.setattr(workflow, "result_for", lambda *_: retained)
    calls = []

    def evidence(query, results):
        calls.append((query.view, query.cursor))
        return EvidenceResponse.model_validate({
            "evidence_id": str(uuid4()), "view": query.view, "artifact_refs": [],
            "availability": "unavailable", "comparisons": [], "missing_reasons": ["MISSING"],
            "facts": [{"fact_id": "shared.missing", "run_id": run_id, "metric": "crews_not_arrived",
                       "value": None, "availability": "unavailable", "reason_code": "MISSING",
                       "derivation": "retained artifact", "source_fields": ["/policies/0/summary"]}],
            "pagination": {"next_cursor": "page2" if not query.cursor else None,
                           "has_more": not query.cursor, "total_items": 2, "returned_items": 1,
                           "offset": 1 if query.cursor else 0, "byte_limit": 32768},
        })

    monkeypatch.setattr(planning_evidence, "build_evidence", evidence)
    context = workflow.evidence_context(None, {"id": run_id}, investigation_readers())
    assert len(calls) == 10, "Every page, including comparisons, must be consumed"
    assert ("comparison", None) in calls and ("comparison", "page2") in calls
    facts = [fact for page in context["evidence"] for fact in page["facts"]]
    assert len(facts) == 1
    assert facts[0]["value"] is None and facts[0]["reason_code"] == "MISSING"
    assert all(page["shared_fact_ids"] == ["shared.missing"] for page in context["evidence"][1:])
    assert context["input_bundle_sha256"] == retained["input_bundle_sha256"]


def test_real_evidence_context_and_display_keep_paired_subset_and_unavailable_source(setup, monkeypatch):
    run_id = str(uuid4())
    result = ledger_result()
    result["sources"]["sources"].append({"name": "Caltrans PeMS", "status": "not_configured"})
    monkeypatch.setattr(workflow, "result_for", lambda *_: result)
    context = workflow.evidence_context(None, {"id": run_id}, investigation_readers())
    pairs = [f for page in context["evidence"] for f in page["facts"] if f["metric"].startswith("paired.")]
    assert any(f["metric"] == "paired.baseline.count" and f["value"] == 3 for f in pairs)
    summary = planning_evidence.planning_summary(context["evidence"])
    assert "702 people exited; 98 unresolved" in summary
    assert "3/4 staged; 1 not staged" in summary
    assert "among 3 crew vehicles arriving in both policies" in summary
    assert "dispatch-to-staging +5.0 seconds" in summary
    assert "Caltrans PeMS: not configured; record count unavailable" in summary
    assert "Caltrans PeMS: not configured; record count 0" not in summary
    assert "Missed staging does not establish lost positions or communications" in summary


def test_cited_plan_and_complete_simulation_summary_survive_display_length(setup, monkeypatch):
    store, incident, _, _, queued = setup
    run_id = str(uuid4())
    job = {"id": run_id, "incident_id": incident["incident_id"], "location_revision": 1}
    result = ledger_result()
    result.update(input_bundle_id="retained-bundle", input_bundle_sha256="a" * 64)
    monkeypatch.setattr(simulation_jobs, "owned_job", lambda *_: job)
    monkeypatch.setattr(workflow, "result_for", lambda *_: result)
    summary = planning_evidence.planning_summary(workflow.evidence_context(store, job, investigation_readers())["evidence"])
    guidance = "Cited scenario application. " * 120
    assert len(summary + "\n\n" + guidance) > 4000

    def events(payload, **kwargs):
        for event in invoke_events(payload, **kwargs):
            if event["type"] == "STATE_SNAPSHOT":
                event["snapshot"]["content"]["response"] = guidance
            yield event

    monkeypatch.setattr(runtime_client, "invoke_runtime", events)
    workflow.start(store, "alice", incident, IncidentInvestigationRequest(run_id=run_id), investigation_readers())
    queued[-1][0](*queued[-1][1])
    saved = workflow.read(store, incident, "alice", run_id)
    assert saved["status"] == "completed"
    assert saved["snapshot"]["content"]["response"] == summary + "\n\n" + guidance
    assert saved["snapshot"]["content"]["knowledgeBaseCitations"]
    assert saved["input_bundle_sha256"] == result["input_bundle_sha256"]

    # The canonical persisted-receipt bound still rejects an oversized update
    # atomically, leaving the running attempt unchanged.
    initial = workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    value = owned(store, "incident_investigation", initial["task_id"], "alice")
    with pytest.raises(IntegrationError) as error:
        workflow.update(store, initial["task_id"], "alice", value["attempt_id"],
                        snapshot={"content": {"response": "x" * workflow.MAX_RECEIPT_BYTES}})
    assert error.value.code == "INVESTIGATION_TOO_LARGE"
    assert workflow.read(store, incident, "alice") == initial


def test_heartbeat_only_runtime_ends_and_releases_execution_slot_at_total_deadline(setup, monkeypatch, runtime_binding):
    store, incident, _, _, queued = setup
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(workflow, "investigation_slots", lambda: slots)
    elapsed = [0.0]
    monkeypatch.setattr(runtime_client.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setenv("SIMULATION_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fixture")

    class Stream:
        closed = False
        def set_socket_timeout(self, remaining):
            assert 0 < remaining <= 190
        def read(self, size):
            elapsed[0] += 1
            return b": heartbeat\n\n"[int(elapsed[0]) % 13:int(elapsed[0]) % 13+1]
        def close(self):
            self.closed = True

    stream = Stream()
    monkeypatch.setattr(runtime_client.boto3, "client", lambda *a, **k: SimpleNamespace(
        invoke_agent_runtime=lambda **kw: {"response": stream}))
    workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    assert not slots.acquire(blocking=False)
    queued[-1][0](*queued[-1][1])
    assert stream.closed and elapsed[0] <= workflow.DEADLINE_SECONDS
    assert workflow.read(store, incident, "alice")["status"] == "failed"
    assert slots.acquire(blocking=False)
    slots.release()


def test_oversized_plan_is_refused_before_billing_or_recording(setup, monkeypatch):
    store, incident, _, budgets, queued = setup
    monkeypatch.setattr(workflow, "MAX_PLANNING_CONTEXT_BYTES", 10)
    with pytest.raises(IntegrationError, match="narrower"):
        workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    assert not budgets and not queued
    assert workflow.read(store, incident, "alice") is None


@pytest.mark.parametrize(("size", "accepted"), [
    # The paradise-2018 acceptance run of phase 7's release attempt 3, refused with 409 at 128 KiB.
    (132_014, True), (192 * 1024, True), (192 * 1024 + 1, False)])
def test_updated_plan_context_is_accepted_to_192_kib_and_refused_above(setup, monkeypatch, size, accepted):
    """An updated plan's context may reach 192 KiB; above it, start() refuses with 409 before billing."""
    store, incident, _, budgets, queued = setup
    run_id = str(uuid4())
    job = {"id": run_id, "incident_id": incident["incident_id"], "location_revision": 1}
    monkeypatch.setattr(simulation_jobs, "owned_job", lambda *_: job)

    def evidence(padding):
        return {"run_id": run_id, "input_bundle_id": "server-bundle", "input_bundle_sha256": "a"*64,
                "evidence": [{"facts": [{"metric": "padding", "value": "x"*padding}]}]}

    def planned(padding):
        # What start() sends: the verified location, the evidence and (no initial plan here) its empty link.
        context = {"verified_location": incident["location"], **evidence(padding),
                   **workflow.initial_plan_context(None)}
        return len(json.dumps(context, separators=(",", ":")).encode())

    padding = size - planned(0)
    assert planned(padding) == size
    monkeypatch.setattr(workflow, "evidence_context", lambda *_: evidence(padding))
    request = IncidentInvestigationRequest(run_id=run_id)
    if accepted:
        assert workflow.start(store, "alice", incident, request, investigation_readers())["status"] == "running"
        assert len(budgets) == len(queued) == 1
        assert len(json.dumps(queued[0][1][5], separators=(",", ":")).encode()) == size
    else:
        with pytest.raises(IntegrationError) as error:
            workflow.start(store, "alice", incident, request, investigation_readers())
        assert (error.value.code, error.value.status) == ("EVIDENCE_LIMIT", 409)
        assert not budgets and not queued
        assert workflow.read(store, incident, "alice", run_id) is None


def test_owner_admission_bounds_parallel_investigations(setup):
    store, incident, _, budgets, _ = setup
    workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    other = {**incident, "incident_id": str(uuid4())}
    with pytest.raises(IntegrationError, match="current incident"):
        workflow.start(store, "alice", other, IncidentInvestigationRequest(), investigation_readers())
    assert len(budgets) == 1


def fire_snapshot(*, response="Source observations", citations=None):
    return {"type": "structuredResponse", "content": {
        "response": response, "knowledgeBaseCitations": citations,
        "mapData": {"locations": [
            {"latitude": 47.6062, "longitude": -122.3321, "currentWeather": {}},
            {"latitude": 46.9, "longitude": -121.7, "fire": {
                "name": "Nearby source fixture", "sourceId": "esri:fixture:first",
                "lastUpdated": "2026-10-26T21:00:00Z"}},
            {"latitude": 46.92, "longitude": -121.72, "fire": {
                "name": "Separate source fixture", "sourceId": "esri:fixture:second"}},
        ]}}}


def test_fire_receipts_survive_uncited_guidance_repeated_snapshots_and_explicit_retry(setup, monkeypatch):
    store, incident, _, _, queued = setup

    def events(payload, **_kwargs):
        snapshot = fire_snapshot()
        yield {"type": "STATE_SNAPSHOT", "snapshot": snapshot}
        yield {"type": "STATE_SNAPSHOT", "snapshot": snapshot}
        yield {"type": "RUN_FINISHED"}

    monkeypatch.setattr(runtime_client, "invoke_runtime", events)
    workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    target, args = queued.pop()
    target(*args)
    first = workflow.read(store, incident, "alice")
    assert first["status"] == "failed", "Missing guidance cannot hide independently retrieved source points"
    points = first["fire_observations"]
    assert len(points) == 2 and points[0]["latitude"] == 46.9
    assert points[0]["source_timestamp"] == "2026-10-26T21:00:00+00:00"
    original = [owned(store, "fire_observation", point["observation_id"], "alice") for point in points]
    assert all(value["revision"] == 1 for value in original), "Repeated snapshots do not rewrite source records"

    next_attempt = workflow.start(store, "alice", incident, IncidentInvestigationRequest(retry=True),
                                  investigation_readers())
    assert next_attempt["fire_observations"] == []
    queued[-1][0](*queued[-1][1])
    refreshed = workflow.read(store, incident, "alice")["fire_observations"]
    assert {point["observation_id"] for point in refreshed}.isdisjoint(
        point["observation_id"] for point in points)
    assert [owned(store, "fire_observation", point["observation_id"], "alice") for point in points] == original


def test_empty_planning_response_still_saves_fire_points_before_runtime_failure(setup, monkeypatch):
    store, incident, _, _, queued = setup
    monkeypatch.setattr(runtime_client, "invoke_runtime", lambda payload, **_kwargs: iter([
        {"type": "STATE_SNAPSHOT", "snapshot": fire_snapshot(response="")},
        {"type": "RUN_ERROR", "message": "Unavailable guidance"},
    ]))
    workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    queued[-1][0](*queued[-1][1])
    saved = workflow.read(store, incident, "alice")
    assert saved["status"] == "failed" and saved["snapshot"] is None
    assert len(saved["fire_observations"]) == 2


def test_invalid_source_records_are_not_selectable_and_query_location_is_not_a_fire(setup):
    store, incident, _, _, _ = setup
    snapshot = fire_snapshot()
    snapshot["content"]["mapData"]["locations"].extend([
        {"latitude": True, "longitude": -121.7, "fire": {"sourceId": "esri:bad:bool"}},
        {"latitude": 91.0, "longitude": -121.7, "fire": {"sourceId": "esri:bad:latitude"}},
        {"latitude": 46.9, "longitude": -121.7, "fire": {"sourceId": " "}},
        {"latitude": 46.9, "longitude": -121.7, "fire": {"name": "Missing source identity"}},
        {"latitude": 46.9, "longitude": -121.7, "fire": {
            "sourceId": "esri:bad:time", "lastUpdated": "2026-10-26T21:00:00"}},
    ])
    points, writes = fire_observations.capture_writes(store, "alice", incident["incident_id"], "attempt", snapshot)
    assert len(points) == len(writes) == 2
    assert {point["source_id"] for point in points} == {"esri:fixture:first", "esri:fixture:second"}


def test_cancelled_attempt_cannot_publish_fire_source_references(setup):
    store, incident, _, _, _ = setup
    started = workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    task = owned(store, "incident_investigation", started["task_id"], "alice")
    snapshot = fire_snapshot()
    points, _ = fire_observations.capture_writes(store, "alice", task["task_id"], task["attempt_id"], snapshot)
    workflow.update(store, task["task_id"], "alice", task["attempt_id"], status="cancelled")
    with pytest.raises(IntegrationError) as error:
        workflow.update(store, task["task_id"], "alice", task["attempt_id"], observation_snapshot=snapshot)
    assert error.value.code == "INVESTIGATION_ENDED"
    assert all(store.get("fire_observation:"+point["observation_id"]) is None for point in points)


def test_fire_source_insert_is_atomic_with_the_fenced_task_update(setup, monkeypatch):
    store, incident, _, _, _ = setup
    started = workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    task = owned(store, "incident_investigation", started["task_id"], "alice")
    snapshot = fire_snapshot()
    points, _ = fire_observations.capture_writes(store, "alice", task["task_id"], task["attempt_id"], snapshot)

    def conflict(*_):
        raise IntegrationError("REVISION_CONFLICT", "State changed.", 409)

    monkeypatch.setattr(workflow, "commit", conflict)
    with pytest.raises(IntegrationError):
        workflow.update(store, task["task_id"], "alice", task["attempt_id"], observation_snapshot=snapshot)
    assert all(store.get("fire_observation:"+point["observation_id"]) is None for point in points)
    assert owned(store, "incident_investigation", task["task_id"], "alice").get("fire_observations") == []


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_explicit_rejoin_migrates_legacy_fire_snapshots_without_another_model_request(setup, status):
    store, incident, _, budgets, queued = setup
    started = workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    task = owned(store, "incident_investigation", started["task_id"], "alice")
    workflow.update(store, task["task_id"], "alice", task["attempt_id"], status=status,
                    snapshot=fire_snapshot(), completed_at=time.time())
    legacy = owned(store, "incident_investigation", task["task_id"], "alice")
    revision = legacy["revision"]
    legacy.pop("fire_observations")
    legacy["revision"] += 1
    commit(store, [(legacy, revision)])
    before = owned(store, "incident_investigation", task["task_id"], "alice")
    assert workflow.read(store, incident, "alice")["fire_observations"] == []
    assert owned(store, "incident_investigation", task["task_id"], "alice") == before, "GET does not migrate"

    migrated = workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    assert migrated["status"] == status and len(migrated["fire_observations"]) == 2
    assert len(budgets) == len(queued) == 1, "Rejoin does not bill or start another Runtime request"
    after = owned(store, "incident_investigation", task["task_id"], "alice")
    assert {key: value for key, value in after.items() if key not in ("revision", "fire_observations")} == {
        key: value for key, value in before.items() if key != "revision"}
    observations = [owned(store, "fire_observation", point["observation_id"], "alice")
                    for point in migrated["fire_observations"]]
    assert workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers()) == migrated
    assert owned(store, "incident_investigation", task["task_id"], "alice") == after
    assert [owned(store, "fire_observation", point["observation_id"], "alice")
            for point in migrated["fire_observations"]] == observations


def test_legacy_fire_snapshot_migration_refuses_expired_task_without_writes_or_model_work(setup):
    store, incident, _, budgets, queued = setup
    started = workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    task = owned(store, "incident_investigation", started["task_id"], "alice")
    workflow.update(store, task["task_id"], "alice", task["attempt_id"], status="failed",
                    snapshot=fire_snapshot(), completed_at=time.time())
    store.update(task["id"], expires=time.time()-1)
    points, _ = fire_observations.capture_writes(store, "alice", task["task_id"], task["attempt_id"],
                                               fire_snapshot())
    with pytest.raises(IntegrationError) as error:
        workflow.start(store, "alice", incident, IncidentInvestigationRequest(), investigation_readers())
    assert (error.value.code, error.value.status) == ("NOT_FOUND", 404)
    assert all(store.get("fire_observation:"+point["observation_id"]) is None for point in points)
    assert len(budgets) == len(queued) == 1
