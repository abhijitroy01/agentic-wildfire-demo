"""Acceptance fixtures use independent ledger values, not a running simulator."""
import copy
import json
import os
import shutil
import subprocess  # nosec B404 - runs only node on the repository's metric reference script, never a shell
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError
from traffic_simulator.errors import IntegrationError
from traffic_simulator.store import Store
from traffic_simulator.testing import create_job

from backend.evacuation.capabilities import capabilities
from backend.evacuation.contracts import EvidenceFact, EvidenceRequest, SimulationScenario, VariantRequest
from backend.evacuation.evidence import build_evidence
from backend.tests.builders import ledger_result

RUN = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
ADAPTER = TypeAdapter(EvidenceRequest)


@pytest.fixture
def result():
    """A two-policy result: one crew never departs, three stage, only one returns."""
    return ledger_result()


def query(view="outcomes", **kwargs):
    return ADAPTER.validate_python({"view": view, "run_ids": [RUN],
                                    **({} if view in ("sources", "assumptions") else
                                       {"policy_ids": ["baseline", "coordinated"]}), **kwargs})


def all_pages(request, results, byte_limit=32768):
    pages = []
    for _ in range(100):
        page = build_evidence(request, results, byte_limit=byte_limit)
        assert len(page.model_dump_json().encode()) <= byte_limit
        pages.append(page)
        if not page.pagination.has_more:
            return pages
        request = request.model_copy(update={"cursor": page.pagination.next_cursor})
    raise AssertionError("Pagination did not terminate")


def facts(pages):
    return {(f.policy_id, f.metric, f.crew_id): f for page in pages for f in page.facts}


def test_joint_accounting_and_unreached_threshold(result):
    rows = facts(all_pages(query(), {RUN: result}))
    assert rows[("baseline", "people_unresolved", None)].value == 98
    assert rows[("baseline", "people_exposed", None)].value == 26
    assert rows[("coordinated", "people_exited", None)].value == 734
    assert rows[("baseline", "crews_not_staged", None)].value == 1
    assert rows[("baseline", "crews_waiting", None)].value == 1
    assert rows[("baseline", "crews_exposed", None)].value == 0
    threshold = rows[("baseline", "t95_seconds", None)]
    assert threshold.value is None and threshold.availability == "not_reached"
    assert rows[("baseline", "slow_vehicle_seconds", None)].value == 22500


@pytest.mark.parametrize("change,expected", [
    ({"valid_engine_run": False}, "invalid"),
    ({"valid_accounting": False}, "invalid"),
    ({"exited_vehicles": 399}, "invalid"),
    ({"crews_staged": 5}, "invalid"),
    ({"not_departed_vehicles": True}, "invalid"),
    ({"simulated_seconds": 1700}, "invalid"),
    ({"exposed_vehicles": None}, "invalid"),
    ({"unresolved_vehicles": 10}, "invalid"),
])
def test_invalid_ledger_cannot_produce_success_facts(result, change, expected):
    result["policies"][0]["summary"].update(change)
    page = build_evidence(query(policy_ids=["baseline"]), {RUN: result})
    assert all(f.value is None and f.availability == expected for f in page.facts)


def test_missing_zero_and_absent_threshold_are_distinct(result):
    del result["policies"][0]["summary"]["t95_seconds"]
    rows = facts(all_pages(query(), {RUN: result}))
    assert rows[("baseline", "t95_seconds", None)].availability == "unavailable"
    assert rows[("baseline", "crews_exposed", None)].value == 0
    del result["policies"][0]["summary"]["exposed_crews"]
    page = build_evidence(query(policy_ids=["baseline"]), {RUN: result})
    assert page.availability == "unavailable"
    assert all(f.value is None for f in page.facts)


def test_t95_requires_reached_count_and_valid_time(result):
    summary = result["policies"][0]["summary"]
    summary["t95_seconds"] = 1200
    rows = facts(all_pages(query(), {RUN: result}))
    assert rows[("baseline", "t95_seconds", None)].availability == "invalid"
    summary.update(exited_vehicles=380, active_vehicles=8, not_departed_vehicles=12)
    rows = facts(all_pages(query(), {RUN: result}))
    assert rows[("baseline", "t95_seconds", None)].value == 1200


def test_paired_crew_timing_keeps_nonarrival_context(result):
    pages = all_pages(query("comparison"), {RUN: result})
    rows = facts(pages)
    assert rows[("coordinated", "paired.baseline.count", None)].value == 3
    assert rows[("coordinated", "paired.baseline.response_delta_s", None)].value == 5
    assert rows[("coordinated", "crews_not_staged", None)].value == 1
    delta = next(c for p in pages for c in p.comparisons if c.metric == "people_exited")
    assert delta.delta == 32
    # Never use the absolute staging timestamp mean.
    result["policies"][1]["summary"]["crew_mean_staging_seconds"] = -999
    assert facts(all_pages(query("comparison"), {RUN: result}))[
        ("coordinated", "paired.baseline.response_delta_s", None)].value == 5


def test_incomplete_or_contradictory_crew_timeline_is_unavailable(result):
    del result["policies"][1]["frames"][-1]["crews"][2]["departed_s"]
    rows = facts(all_pages(query("comparison"), {RUN: result}))
    assert rows[("coordinated", "paired.baseline.count", None)].availability == "unavailable"


def test_no_arrivals_does_not_report_zero_response_time(result):
    for policy in result["policies"]:
        policy["summary"].update(crews_staged=0, crews_returned=0)
        for c in policy["frames"][-1]["crews"]:
            c.update(reached_staging_s=None, returned_s=None)
    rows = facts(all_pages(query("comparison"), {RUN: result}))
    assert rows[("coordinated", "paired.baseline.count", None)].value == 0
    assert rows[("coordinated", "paired.baseline.response_delta_s", None)].value is None


def test_crew_focus_retains_assignment_and_nonarrival_totals(result):
    rows = facts(all_pages(query("crew", crew_id="crew-2"), {RUN: result}))
    assert rows[("baseline", "crews_assigned", None)].value == 4
    assert rows[("baseline", "reached_staging_s", "crew-2")].availability == "not_reached"
    assert not any(crew_id == "crew-1" for _, _, crew_id in rows)


def test_replay_uses_at_or_before_and_does_not_substitute_final_exposure(result):
    rows = facts(all_pages(query("replay", time_scope={"kind": "replay", "requested_time_s": 905}), {RUN: result}))
    f = rows[("baseline", "people_unresolved", None)]
    assert f.value == 440
    assert f.time_scope.requested_time_s == 905 and f.time_scope.effective_time_s == 900
    assert rows[("baseline", "crews_exposed", None)].value is None
    assert rows[("baseline", "crews_exposed", None)].availability == "unavailable"


def test_replay_comparison_selects_common_actual_timestamp(result):
    result["policies"][1]["series"][1]["t"] = 890
    pages = all_pages(query("comparison", time_scope={"kind": "replay", "requested_time_s": 905}), {RUN: result})
    rows = facts(pages)
    assert rows[("baseline", "people_unresolved", None)].time_scope.effective_time_s == 0
    assert rows[("coordinated", "people_unresolved", None)].time_scope.effective_time_s == 0
    assert next(c for p in pages for c in p.comparisons if c.metric == "people_exited").delta == 0


def test_different_snapshots_do_not_claim_controlled_deltas(result):
    alternative = copy.deepcopy(result)
    pages = all_pages(query("comparison", run_ids=[RUN, OTHER], policy_ids=["baseline"]),
                      {RUN: result, OTHER: alternative})
    assert all(c.delta is None and c.comparison_kind == "changed_conditions" for p in pages for c in p.comparisons)
    result.update(input_bundle_id=RUN, input_bundle_sha256="abc")
    alternative.update(input_bundle_id=RUN, input_bundle_sha256="abc")
    pages = all_pages(query("comparison", run_ids=[RUN, OTHER], policy_ids=["baseline"]),
                      {RUN: result, OTHER: alternative})
    assert any(c.comparison_kind == "controlled_variant" and c.delta == 0 for p in pages for c in p.comparisons)


def test_source_metadata_excludes_urls_and_raw_provider_records(result):
    source = result["sources"]["sources"][0]
    source["url"] = "https://provider.test/?key=NEVER_IN_TOOL_CONTEXT"
    source["records"] = [{"secret": "NEVER_IN_TOOL_CONTEXT"}]
    text = "".join(p.model_dump_json() for p in all_pages(query("sources"), {RUN: result}))
    assert "NEVER_IN_TOOL_CONTEXT" not in text
    assert "context only" in text
    assert "not_applied" in text


@pytest.mark.parametrize("traffic,value,availability", [
    # The daily TomTom budget was used up: the run states that no provider speed was applied.
    ({"mode": "provider_speed_limits", "status": "unavailable", "reason": "daily_budget_reached", "matches": []},
     "not_applied", "available"),
    ({"mode": "provider_speed_limits", "status": "applied", "matches": [{"edge_id": "e"}]},
     "matched_edge_speed_limits", "available"),
    ({"mode": "provider_speed_limits", "status": "applied"}, None, "unavailable"),
])
def test_traffic_application_fact_follows_the_saved_snapshot(result, traffic, value, availability):
    result["scenario"]["traffic_mode"] = "provider_speed_limits"
    result["traffic"] = traffic
    rows = {f.metric: f for page in all_pages(query("sources"), {RUN: result}) for f in page.facts}
    assert (rows["traffic.application"].value, rows["traffic.application"].availability) == (value, availability)
    assert rows["traffic.status"].value == traffic["status"]


def test_assumptions_remain_inputs_and_preserve_si(result):
    result["scenario"]["hazard_radius_m"] = 150.25
    rows = facts(all_pages(query("assumptions"), {RUN: result}))
    f = rows[(None, "assumption.hazard_radius_m", None)]
    assert f.value == 150.25 and f.unit == "m"
    assert rows[(None, "assumption.highway", None)].availability == "unavailable"


def test_pagination_is_bounded_complete_repeatable_and_bound_to_artifact(result):
    request = query("crew")
    pages = all_pages(request, {RUN: result}, byte_limit=4096)
    assert len(pages) > 2
    assert len({p.evidence_id for p in pages}) == 1
    ids = [f.fact_id for page in pages for f in page.facts]
    assert len(ids) == len(set(ids)) == pages[0].pagination.total_items
    assert pages[0].model_dump() == build_evidence(request, {RUN: result}, byte_limit=4096).model_dump()
    result["scenario"]["people_per_vehicle"] = 3
    with pytest.raises(IntegrationError, match="cursor"):
        build_evidence(request.model_copy(update={"cursor": pages[0].pagination.next_cursor}), {RUN: result})


@pytest.mark.parametrize("body", [
    {"view": "sources", "run_ids": [RUN], "policy_ids": ["baseline"]},
    {"view": "outcomes", "run_ids": [RUN], "time_scope": {"kind": "replay", "requested_time_s": 1}},
    {"view": "crew", "run_ids": [RUN], "crew_id": "../../secret"},
    {"view": "replay", "run_ids": [RUN], "time_scope": {"kind": "replay", "requested_time_s": float("nan")}},
    {"view": "outcomes", "run_ids": [RUN, RUN]},
    {"view": "outcomes", "run_ids": [RUN], "policy_ids": ["baseline", "baseline"]},
    {"view": "outcomes", "run_ids": [RUN], "owner": "someone-else"},
])
def test_discriminated_requests_reject_irrelevant_or_invalid_input(body):
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(body)


def test_strict_scenario_has_no_geographic_defaults_and_rejects_coerced_counts():
    with pytest.raises(ValidationError):
        SimulationScenario()
    with pytest.raises(ValidationError):
        SimulationScenario(lat=39.7, lon=-121.6, radius_km=8, highway="State 191",
                           exit_direction="south", vehicles=True)
    with pytest.raises(ValidationError):
        VariantRequest.model_validate_json(json.dumps({
            "parent_run_id": RUN, "request_id": OTHER, "capability_version": "1.0.0",
            "changes": {"wind_speed": 20}}))


def test_unavailable_fact_requires_null_and_reason():
    with pytest.raises(ValidationError):
        EvidenceFact(fact_id="x", run_id=UUID(RUN), metric="people_exited", value=0,
                     availability="unavailable", derivation="fixture", source_fields=[])


def test_capabilities_only_advertise_implemented_operations_and_actual_evidence(result):
    ops = {c.id: c for c in capabilities(result, job_status="completed").operations}
    assert ops["compare_outcomes"].enabled and ops["crew_details"].enabled
    assert not ops["variant_departure_window"].enabled
    assert not ops["wind_driven_spread"].enabled
    del result["policies"][0]["frames"]
    ops = {c.id: c for c in capabilities(result, job_status="completed").operations}
    assert not ops["crew_details"].enabled and ops["crew_details"].suggested_question is None
    assert {op.id for op in capabilities(job_status="running").operations if op.enabled} == {
        "run_status", "cancel_run"}


def test_api_checks_all_ownership_before_artifacts_and_normalizes_errors(tmp_path, monkeypatch, result):
    from backend import app as main
    from backend.demo_access import workspace_owner
    store = Store(tmp_path)
    monkeypatch.setattr(main, "store", store)
    own = create_job(store, "alice", result["scenario"])
    other = create_job(store, "bob", result["scenario"])
    lab = create_job(store, "alice", result["scenario"])
    # v1 evidence reads only session runs; the Traffic Lab reads only its own runs.
    store.update(own["id"], status="completed", session_id=str(uuid4()))
    store.update(lab["id"], status="completed")
    store.artifact(own["id"], "result", result)
    # One owner dependency serves the v1 and Traffic Lab routes.
    main.app.dependency_overrides[workspace_owner] = lambda: "alice"
    try:
        client = TestClient(main.app)
        response = client.post("/api/v1/evidence", json={"view": "outcomes", "run_ids": [own["id"]],
                                                       "policy_ids": ["baseline"]})
        assert response.status_code == 200, response.json()
        assert response.json()["facts"][0]["run_id"] == own["id"]
        response = client.post("/api/v1/evidence", json={"view": "outcomes", "run_ids": [other["id"], own["id"]]})
        assert response.status_code == 404 and response.json()["error"]["code"] == "RUN_NOT_FOUND"
        invalid = client.post("/api/v1/evidence", json={"view": "outcomes", "run_ids": [own["id"]],
                                                      "secret": "DO_NOT_ECHO"})
        assert invalid.status_code == 422 and "DO_NOT_ECHO" not in invalid.text
        # Traffic Lab routes read the same owner-scoped job records, each route only its own kind.
        assert client.get(f"/api/v1/research/jobs/{lab['id']}").json()["status"] == "completed"
        assert client.get(f"/api/v1/research/jobs/{own['id']}").status_code == 404
        assert client.get(f"/api/v1/research/jobs/{other['id']}").status_code == 404
        assert client.post("/api/v1/evidence", json={"view": "outcomes", "run_ids": [lab["id"]]}).status_code == 404
    finally:
        main.app.dependency_overrides.clear()


@pytest.mark.parametrize("case", ["valid", "missing_crew_count", "invalid_ledger"])
def test_python_evidence_agrees_with_existing_typescript_metrics(result, case):
    root = Path(__file__).resolve().parents[3]
    if not shutil.which("node") or not (root / "ui/node_modules/typescript").exists():
        if os.getenv("REQUIRE_WEB_METRIC_PARITY") == "1":
            pytest.fail("Release verification requires Node and the locked UI dependencies.")
        pytest.skip("Install the UI dependencies (npm ci in ui/) to run cross-language metric agreement.")
    if case == "missing_crew_count":
        del result["policies"][0]["summary"]["exposed_crews"]
    elif case == "invalid_ledger":
        result["policies"][0]["summary"]["exited_vehicles"] = 399
    # The node found on PATH (checked above) runs the repository's own script; the fixture goes in on stdin. No shell.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    completed = subprocess.run(  # nosec B603 B607 - fixed argument list, no shell
        ["node", str(root / "ui/scripts/evidence_metric_reference.cjs")], input=json.dumps(result),
        text=True, capture_output=True, timeout=20, check=True,
    )
    reference = json.loads(completed.stdout)
    rows = facts(all_pages(query("comparison"), {RUN: result}))
    mapping = {"people_exited": "peopleExited", "people_unresolved": "peopleUnresolved",
               "people_waiting": "peopleWaiting", "people_exposed": "peopleExposed",
               "crews_staged": "staged", "crews_not_staged": "notStaged", "crews_exposed": "crewExposed",
               "crews_waiting": "crewWaiting", "crews_returned": "returned", "t95_seconds": "t95"}
    for policy_id, metrics in reference.items():
        for metric, frontend_key in mapping.items():
            fact = rows[(policy_id, metric, None)]
            expected = metrics["mission"][frontend_key] if metrics["mission"] else None
            assert fact.value == expected, (case, policy_id, metric)
    paired = reference["coordinated"]["paired"]
    expected_delta = paired["response"] if paired else None
    assert rows[("coordinated", "paired.baseline.response_delta_s", None)].value == expected_delta


def test_release_metric_gate_cannot_silently_skip(monkeypatch):
    monkeypatch.setenv("REQUIRE_WEB_METRIC_PARITY", "1")
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(pytest.fail.Exception, match="Release verification requires Node"):
        test_python_evidence_agrees_with_existing_typescript_metrics({}, "valid")
