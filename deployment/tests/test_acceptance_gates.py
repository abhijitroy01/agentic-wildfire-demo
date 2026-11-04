"""Offline tests of two deploy gates: firetruck/report acceptance and the real-model guidance precheck's decisions."""
import asyncio
import builtins
import gzip
import json
import os
import re
import subprocess  # nosec B404 - fixed isolated interpreter arguments, no shell or cloud operations
import sys
from contextlib import ExitStack
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from deployment import ROOT
from deployment.acceptance import accept_firetruck, check_guidance_model, visitor
from deployment.acceptance.smoke_simulation_api import verify_fire_search_center
from deployment.acceptance.visitor import AcceptanceError

# Two browsers' demo-session cookie values, built at runtime.
OWNER, OTHER = "O" * 43, "F" * 43
OWNER_RESOURCES = {("GET", "firetruck/runs/run-1"), ("DELETE", "firetruck/runs/run-1"), ("GET", "firetruck/runs"),
                   ("POST", "firetruck/runs/run-1/reports"), ("GET", "firetruck/reports/report-1"),
                   ("GET", "firetruck/reports/report-1/html")}


@pytest.mark.parametrize("center,expected", [
    ({"latitude": 38.819727, "longitude": -123.188069}, True),
    ({"latitude": 38.70646, "longitude": -122.90408}, False),
    ({"latitude": 38.819727, "longitude": float("nan")}, False),
    ({"latitude": True, "longitude": -123.188069}, False),
    (None, False),
])
def test_live_fire_search_cannot_pass_with_a_nearby_town_or_missing_source_center(center, expected):
    content = {"mapData": {"summary": {"location": center}}}
    assert verify_fire_search_center(content, 38.819727, -123.188069) is expected


def test_standalone_guidance_loader_has_its_pure_contract_dependencies_without_pytest_paths(tmp_path):
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"PYTHONPATH", "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN"}}
    environment.update(AWS_ACCESS_KEY_ID="offline", AWS_SECRET_ACCESS_KEY="offline",
                       AWS_DEFAULT_REGION="us-west-2", AWS_EC2_METADATA_DISABLED="true")
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from deployment.acceptance.check_guidance_model import load_runtime; "
        "runtime = load_runtime(); assert callable(runtime.readers.context); "
        "from fire_risk_modeler.models import AssessmentResult; assert AssessmentResult"
    )
    result = subprocess.run(  # nosec B603 - only the fixed offline loader check, root supplied as a separate argument
        [sys.executable, "-I", "-c", script, str(ROOT)], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-4000:]


def test_standalone_smoke_fallback_and_planning_load_their_contracts_without_pytest_paths(tmp_path):
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"PYTHONPATH", "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN"}}
    environment.update(AWS_ACCESS_KEY_ID="offline", AWS_SECRET_ACCESS_KEY="offline",
                       AWS_DEFAULT_REGION="us-west-2", AWS_EC2_METADATA_DISABLED="true")
    script = """
import io
import json
import sys
import urllib.request
sys.path.insert(0, sys.argv[1])
from deployment.acceptance.smoke_simulation_api import nearest_reported_incident, verify_plan_summary
from deployment.acceptance.background_jobs import contracts
assert contracts()[0].__name__ == "FireRiskJobReference"
feed = {"features": [{"attributes": {"IncidentName": "Fixture Fire"},
                      "geometry": {"y": 47.6, "x": -122.3}}]}
urllib.request.OpenerDirector.open = lambda *args, **kwargs: io.BytesIO(json.dumps(feed).encode())
incident = nearest_reported_incident({"latitude": 47.6, "longitude": -122.3})
assert incident["name"] == "Fixture Fire" and incident["distance_miles"] == 0
assert verify_plan_summary({}, {}) is False
from fire_risk_modeler.models import FireObservation
assert FireObservation
"""
    result = subprocess.run(  # nosec B603 - fixed offline script with a stubbed feed; no shell or AWS calls
        [sys.executable, "-I", "-c", script, str(ROOT)], cwd=tmp_path, env=environment,
        capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-4000:]


def firetruck_workspace(defect):
    """Answer accept_firetruck's requests as the broker does: each run belongs to the browser that started it.

    The first demo session is the owner's, the second the other browser's. defect breaks one rule. Returns the
    handler and the list of (caller, method, route) it saw.
    """
    sent, sessions, stopped = [], iter((OWNER, OTHER)), []

    def reply(request):
        cookie = request.headers.get("cookie", "")
        caller = "owner" if OWNER in cookie else "other" if OTHER in cookie else "new visitor"
        route = request.url.path.removeprefix("/simulation-api/v1/")
        sent.append((caller, request.method, route))
        body = json.loads(request.content) if request.content else {}
        if route == "demo/session":
            return httpx.Response(200, json={"ready": True}, headers={
                "set-cookie": f"__Host-wildfire-demo={next(sessions)}; Path=/; Secure; HttpOnly; SameSite=strict"})
        if caller == "other":
            if (request.method, route) == ("GET", "firetruck/runs"):
                return httpx.Response(200, json=[{"session_id": "run-1"}] if defect == "listed" else [])
            if defect == "foreign-read" and (request.method, route) == ("GET", "firetruck/runs/run-1"):
                return httpx.Response(200, json={"session_id": "run-1", "status": "running"})
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})
        if caller != "owner":
            return httpx.Response(401, json={"error": {"code": "DEMO_SESSION_REQUIRED"}})
        if route == "firetruck/health":
            return httpx.Response(200, json={"status": "ok"})
        if route == "sessions":
            return httpx.Response(201, json={"session_id": "session-1"})
        if route == "incidents":
            return httpx.Response(201, json={"incident_id": "incident-1", "location": {"lat": 34.05, "lon": -118.53}})
        if (request.method, route) == ("POST", "firetruck/runs"):
            if defect == "unavailable":
                return httpx.Response(503, json={"error": {"code": "FIRETRUCK_UNAVAILABLE", "message": "fixture"}})
            return httpx.Response(200, json={"session_id": "run-1", "status": "running"})
        if route == "firetruck/runs/run-1":
            if request.method == "DELETE":
                stopped.append(True)
            return httpx.Response(200, json={"session_id": "run-1", "status": "stopped" if stopped else "running"})
        if route == "firetruck/runs/run-1/reports":
            if "telemetry" in body and defect != "telemetry":
                return httpx.Response(422, json={"error": {"code": "VALIDATION_ERROR"}})
            return httpx.Response(202, json={"report_id": "report-1", "status": "running"})
        if route == "firetruck/reports/report-1":
            return httpx.Response(200, json={"report_id": "report-1", "status": "completed"})
        if route == "firetruck/reports/report-1/html":
            html = "<html><body>Synthetic telemetry from run-1</body></html>"
            if defect == "script":
                html = html.replace("<body>", "<body><script>fetch('/')</script>")
            return httpx.Response(200, text=html, headers={"content-security-policy": "sandbox; default-src 'none'"})
        return httpx.Response(404, json={"error": {"code": "UNEXPECTED_ROUTE"}})
    return reply, sent


@pytest.mark.parametrize("defect,error", [
    (None, None),
    ("foreign-read", "Foreign GET firetruck/runs/run-1: HTTP 200"),
    ("listed", "Foreign run leaked through listing"),
    ("telemetry", "Client telemetry was not rejected"),
    ("script", "Report HTML is unavailable or contains scripts"),
    ("unavailable", "POST /simulation-api/v1/firetruck/runs: HTTP 503 FIRETRUCK_UNAVAILABLE"),
])
def test_firetruck_acceptance_keeps_each_run_to_its_owner_session(tmp_path, monkeypatch, defect, error):
    reply, sent = firetruck_workspace(defect)
    client = httpx.Client
    monkeypatch.setattr(visitor.httpx, "Client",
                        lambda **options: client(transport=httpx.MockTransport(reply), **options))
    monkeypatch.setattr(accept_firetruck.time, "sleep", lambda seconds: None)
    streams = []

    async def stream_check(url, headers, foreign_headers, origin):
        streams.append({"url": url, "owner": headers["Cookie"], "other": foreign_headers["Cookie"], "origin": origin})
        return ["TRUCK_UPDATE"]
    monkeypatch.setattr(accept_firetruck, "stream_check", stream_check)
    output = tmp_path / "acceptance-response.json"
    monkeypatch.setattr(sys, "argv", ["accept_firetruck", "--base-url", "https://workspace.example",
                                      "--output", str(output)])
    if error:
        with pytest.raises((RuntimeError, AcceptanceError), match=error):
            accept_firetruck.main()
    else:
        accept_firetruck.main()
    receipt = json.loads(output.read_text())
    assert receipt["status"] == ("failed" if error else "passed")
    owner_stops = sent.count(("owner", "DELETE", "firetruck/runs/run-1"))
    if defect == "unavailable":
        assert receipt["error"] == error and owner_stops == 0, "No run started, so there is nothing to stop"
        return
    assert owner_stops >= 1, "A started test run is always stopped, even when a check fails"
    if error:
        assert receipt["error"] == error
        return
    assert receipt["checks"] == [
        "Amazon Location preset resolution", "idempotent start", "owned stream",
        "foreign read/stop/list/stream denied", "stop retains evidence", "client telemetry rejected",
        "live AgentCore report", "foreign report access denied", "sandboxed escaped report HTML"]
    # The second browser probes every owned resource with its own session, never the owner's.
    assert {(method, route) for caller, method, route in sent if caller == "other"} == OWNER_RESOURCES
    assert streams == [{"url": "wss://workspace.example/simulation-api/v1/firetruck/runs/run-1/stream",
                        "owner": f"__Host-wildfire-demo={OWNER}", "other": f"__Host-wildfire-demo={OTHER}",
                        "origin": "https://workspace.example"}]
    assert owner_stops == 2, "The explicit stop, then the cleanup stop"
    assert "Synthetic" in (tmp_path / "acceptance-response.html").read_text()


def guidance_runtime(cleaned):
    """Stand in for check_guidance_model.load_runtime(): two hook classes, a supervisor module and schema models."""
    class Guard:
        def before(self, event):
            event.cancel_tool = event.reason

    class Grounding:
        def before(self, event):
            event.cancel_tool = event.reason

    class Model:
        chunks = ()

        async def stream(self, *args, **kwargs):
            for chunk in self.chunks:
                yield chunk
    return SimpleNamespace(GuidanceSelectionGuard=Guard, CoordinateGrounding=Grounding,
                           supervisor_agent=SimpleNamespace(create_model=lambda *args, **kwargs: Model()),
                           models=SimpleNamespace(cleanup=lambda: cleaned.append(True)))


def test_guidance_planning_helpers_use_the_api_readers_without_its_host_settings_or_cloud(monkeypatch):
    from backend import simulation_jobs
    from backend.dependencies import InvestigationReaders
    from backend.evacuation import evidence, service
    from backend.investigation import saved

    original_import = builtins.__import__

    def bounded_import(name, *args, **kwargs):
        fromlist = kwargs.get("fromlist", args[2] if len(args) > 2 else ())
        if name in ("backend.app", "backend.api_settings") or (
                name == "backend" and {"app", "api_settings"}.intersection(fromlist or ())):
            raise AssertionError("Retained evidence must not import the API host or provider settings")
        return original_import(name, *args, **kwargs)

    def cloud_client(*args, **kwargs):
        raise AssertionError("Assembling retained evidence must not call AWS")

    monkeypatch.setattr(builtins, "__import__", bounded_import)
    monkeypatch.setattr(check_guidance_model.boto3, "client", cloud_client)
    runtime = check_guidance_model.planning_helpers()
    assert isinstance(runtime.readers, InvestigationReaders)
    assert runtime.readers.context is service.context
    assert runtime.readers.owned_job is simulation_jobs.owned_job
    assert runtime.readers.build_evidence is evidence.build_evidence
    assert runtime.readers.planning_summary is evidence.planning_summary
    assert runtime.evidence_context_for_result is saved.evidence_context_for_result
    assert runtime.initial_plan_context is saved.initial_plan_context
    assert runtime.MAX_PLANNING_CONTEXT_BYTES == saved.MAX_PLANNING_CONTEXT_BYTES


@pytest.mark.parametrize("separate_observations", [False, True])
def test_guidance_load_fixture_builds_all_real_planning_views_from_retained_artifacts(
        tmp_path, monkeypatch, separate_observations):
    from backend.tests.builders import ledger_result

    result = ledger_result()
    result.update(input_bundle_id="retained-inputs", input_bundle_sha256="a" * 64)
    observations = {"mapData": {"locations": [{"latitude": 39.76, "longitude": -121.62}]},
                    "forecastData": {"fire": "retained forecast"}}
    initial = {"status": "completed", "snapshot": {"content": observations}}
    fixture_dir = tmp_path / "traffic-simulator/.runtime/runs/completed-fixture"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "result.json.gz").write_bytes(gzip.compress(json.dumps(result).encode()))
    (fixture_dir / "initial-investigation.json").write_text(json.dumps(initial))
    observation_file = tmp_path / "observations.json"
    observation_file.write_text(json.dumps(observations))
    monkeypatch.setenv("SIMULATION_FIXTURE", fixture_dir.name)
    monkeypatch.setattr(check_guidance_model, "ROOT", tmp_path)

    runtime = check_guidance_model.planning_helpers()
    fixture = check_guidance_model.load_fixture(observation_file if separate_observations else None, runtime)
    assert fixture.result == result and fixture.recorded_initial == initial
    assert fixture.observations == observations
    context = fixture.simulation
    assert str(UUID(context["run_id"])) == context["run_id"]
    assert context["input_bundle_id"] == "retained-inputs" and context["input_bundle_sha256"] == "a" * 64
    assert {page["view"] for page in context["evidence"]} == {
        "outcomes", "crew", "comparison", "sources", "assumptions"}
    for view in ("outcomes", "crew", "comparison", "sources", "assumptions"):
        pages = [page["pagination"] for page in context["evidence"] if page["view"] == view]
        assert pages[0]["offset"] == 0 and not pages[-1]["has_more"]
        assert sum(page["returned_items"] for page in pages) == pages[-1]["total_items"]
    facts = [fact for page in context["evidence"] for fact in page["facts"]]
    assert any(fact["metric"] == "people_exited" and fact["value"] == 702 for fact in facts)
    assert all(fact["run_id"] == context["run_id"] for fact in facts)
    assert "702 people exited; 98 unresolved" in runtime.readers.planning_summary(context["evidence"])


@pytest.mark.parametrize("failed", [False, True])
def test_guidance_check_fails_when_any_recorded_case_failed_and_always_restores_the_runtime(
        tmp_path, monkeypatch, failed):
    cleaned = []
    runtime = guidance_runtime(cleaned)
    originals = (runtime.GuidanceSelectionGuard.before, runtime.CoordinateGrounding.before,
                 runtime.supervisor_agent.create_model)
    monkeypatch.setattr(check_guidance_model, "load_installation",
                        lambda path, root: SimpleNamespace(agent_model_id="fixture-model"))
    monkeypatch.setattr(check_guidance_model, "use_deployed_runtime", lambda installation: None)
    monkeypatch.setattr(check_guidance_model, "load_runtime", lambda: runtime)
    monkeypatch.setattr(check_guidance_model, "load_fixture", lambda path, loaded: None)

    def run_cases(loaded, fixture, report, save):
        assert loaded.GuidanceSelectionGuard.before is not originals[0], "Cases run with the hooks traced"
        report["cases"] += [{"name": "initial", "status": "passed"},
                            {"name": "missing-location", "status": "failed" if failed else "passed"}]
        save()
    monkeypatch.setattr(check_guidance_model, "run_cases", run_cases)
    output = tmp_path / "guidance-model-check.json"
    if failed:
        with pytest.raises(RuntimeError, match="inspect its retained receipt"):
            check_guidance_model.main(["--output", str(output)])
    else:
        check_guidance_model.main(["--output", str(output)])
    receipt = json.loads(output.read_text())
    assert receipt["status"] == ("failed" if failed else "passed")
    assert [case["name"] for case in receipt["cases"]] == ["initial", "missing-location"]
    assert (runtime.GuidanceSelectionGuard.before, runtime.CoordinateGrounding.before,
            runtime.supervisor_agent.create_model) == originals
    assert cleaned == ([] if failed else [True])


def test_instrumentation_records_source_rejections_and_guardrail_rules_but_no_prompt_text():
    runtime = guidance_runtime([])
    report = {"guardrail_blocks": [], "source_rejections": []}
    blocked = {"action": "BLOCKED", "type": "DENY", "name": "Investment advice", "confidence": "HIGH",
               "match": "Should I invest in Bitcoin?"}
    chunks = [{"metadata": {"trace": {"guardrail": {"inputAssessment": {"policy": {"topics": [blocked]}}}}}},
              {"contentBlockDelta": {"delta": {"text": "answer"}}}]

    async def collect(stream):
        return [chunk async for chunk in stream]
    with ExitStack() as stack:
        check_guidance_model.instrument(stack, runtime, report)
        runtime.GuidanceSelectionGuard().before(SimpleNamespace(
            reason="SOURCE_RESULTS_REQUIRED: run the selected specialists", cancel_tool=None))
        runtime.CoordinateGrounding().before(SimpleNamespace(reason=None, cancel_tool=None))
        model = runtime.supervisor_agent.create_model("supervisor", max_tokens=1500)
        model.chunks = chunks
        assert asyncio.run(collect(model.stream([{"role": "user", "content": []}]))) == chunks
    assert report["source_rejections"] == ["SOURCE_RESULTS_REQUIRED"]
    assert report["guardrail_blocks"] == [{"agent": "supervisor", "rules": [
        {"type": "DENY", "name": "Investment advice", "confidence": "HIGH", "action": "BLOCKED"}]}]


@pytest.mark.parametrize("defect,error", [
    (None, None),
    ("oversized", "Real initial guidance context exceeded the API bound"),
    ("uncited", "Real initial guidance selection failed"),
    ("generic", "Real initial guidance did not synthesize a cited scenario plan"),
    ("not-rejoined", "Repeated guidance did not rejoin its invocation receipt"),
])
def test_guidance_cases_require_cited_scenario_plans_that_rejoin_their_receipts(defect, error):
    bound = []

    def build_public_response_tool(citations, failures, *, assessments, planning_context, question):
        bound.append((planning_context, question))
        receipt = []

        def guidance(prompt):
            if receipt:
                return {**receipt[0], "response_text": "Another plan."} if defect == "not-rejoined" else receipt[0]
            if defect != "uncited":
                citations.append({"documentName": "Official fixture"})
            assessments.append("Assessed against the scenario")
            receipt.append({"knowledgeBaseCitations": list(citations), "response_text": (
                "Generic advice." if defect == "generic" else
                "Planning guidance — scenario interpretation: keep staging clear of the exits.")})
            return receipt[0]
        return guidance
    runtime = SimpleNamespace(
        build_public_response_tool=build_public_response_tool,
        initial_plan_context=lambda initial: {"initial_plan": initial["snapshot"]["content"]["response"]},
        MAX_PLANNING_CONTEXT_BYTES=16 if defect == "oversized" else 192 * 1024)
    fixture = check_guidance_model.Fixture(result={"scenario": {"lat": 39.76, "lon": -121.62}},
                                           recorded_initial=None, observations={}, simulation={"run_id": "run-1"})
    report, saved = {"cases": []}, []
    if error:
        with pytest.raises(RuntimeError, match=error):
            check_guidance_model.guidance_cases(runtime, fixture, report, lambda: saved.append(len(report["cases"])))
        assert not report["cases"]
        return
    check_guidance_model.guidance_cases(runtime, fixture, report, lambda: saved.append(len(report["cases"])))
    assert [case["name"] for case in report["cases"]] == ["initial", "updated"] and saved == [1, 2]
    # The updated plan's context carries the initial plan's cited answer.
    assert fixture.simulation["initial_plan"].startswith("Planning guidance — scenario interpretation")
    # Each plan binds its context and the planning question as the supervisor's planning turn does (phase 2 Q1).
    assert [context for context, _ in bound] == [{"verified_location": {"lat": 39.76, "lon": -121.62}},
                                                 fixture.simulation]
    assert len({question for _, question in bound}) == 1 and bound[0][1].startswith("Select published planning")


def test_the_public_sop_case_asks_the_browser_checks_question_and_requires_its_topics():
    """The deployer's real-model case sends the live browser check's question, so a refused wording fails first."""
    browser = (ROOT / "deployment/acceptance/check_public_sop_browser.cjs").read_text()
    assert f"'{check_guidance_model.PUBLIC_SOP_QUESTION}'" in browser
    assert re.findall(r"assert\.match\(text, /([^/]+)/i\)", browser) == list(check_guidance_model.PUBLIC_SOP_TOPICS)


@pytest.mark.parametrize("defect,error", [
    (None, None),
    ("refused", "selected no task scope"),
    ("scope", "precheck failed"),
    ("uncited", "precheck failed"),
    ("topic", "precheck failed"),
])
def test_the_public_sop_case_requires_a_cited_guidance_answer_on_every_topic(defect, error):
    asked = []

    def select_investigation_topics(question, previous, messages, *, source_location):
        asked.append((question, previous, messages))
        if defect == "refused":
            raise ValueError("Supervisor did not select a validated investigation task scope")
        return {"fires", "guidance"} if defect == "scope" else {"guidance"}

    def create_supervisor_agent(*, guidance_citations, guidance_assessments, user_question, **options):
        def supervisor(prompt, structured_output_model):
            if defect != "uncited":
                guidance_citations.append({"documentName": "Official fixture"})
            guidance_assessments.append("Evacuation routes; " + ("" if defect == "topic" else "escape routes; ")
                                        + "crew communications.")
            return SimpleNamespace(stop_reason="end_turn", structured_output=SimpleNamespace(model_dump=lambda: {
                "type": "structuredResponse", "content": {"response": "source_results_ready"}}))
        return supervisor
    runtime = SimpleNamespace(
        supervisor_agent=SimpleNamespace(select_investigation_topics=select_investigation_topics,
                                         create_supervisor_agent=create_supervisor_agent),
        investigation_memory_context=lambda messages: {"questions": [], "observations": []},
        format_context=lambda question, context: question, PublicAnswerResponse=None,
        bind_guidance_citations=lambda payload, citations, *, observations, assessments: {"content": {
            "response": assessments[-1], "knowledgeBaseCitations": citations}})
    report, saved = {"cases": []}, []
    if error:
        with pytest.raises(RuntimeError, match=error):
            check_guidance_model.public_sop_case(runtime, report, lambda: saved.append(True))
    else:
        check_guidance_model.public_sop_case(runtime, report, lambda: saved.append(True))
    # A fresh visitor: no retained observations and no conversation history.
    assert asked == [(check_guidance_model.PUBLIC_SOP_QUESTION, {}, [])]
    [case] = report["cases"]
    assert saved == [True] and case["status"] == ("failed" if error else "passed")
    if defect == "topic":
        assert case["missing_topics"] == ["escape route"]


@pytest.mark.parametrize("selection,statuses", [("expected", ["passed"] * 3), ("wrong", ["failed"] * 3)])
def test_location_selection_records_whether_each_weather_question_chose_its_place(selection, statuses):
    fire = {"latitude": 39.81, "longitude": -121.44, "fire": {"name": "Camp"}}

    def select_investigation_topics(question, previous=None, messages=(), *, source_location=None):
        if "Camp fire" in question and selection == "expected":
            source_location.update(latitude=39.81, longitude=-121.44)
        if "Seattle" in question and selection == "wrong":
            source_location.update(latitude=39.81, longitude=-121.44)
        return set() if source_location is None and selection == "expected" else {"weather"}
    runtime = SimpleNamespace(supervisor_agent=SimpleNamespace(select_investigation_topics=select_investigation_topics))
    report = {"cases": []}
    check_guidance_model.location_selection_cases(runtime, fire, {}, [], report)
    assert [case["name"] for case in report["cases"]] == [
        "named-fire-weather-target", "new-place-weather-target", "missing-location"]
    assert [case["status"] for case in report["cases"]] == statuses
