"""Bounded real Bedrock/retrieval check before updating an existing demo Runtime."""
import argparse
import gzip
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, nullcontext
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import boto3

from deployment import ROOT
from deployment.acceptance.visitor import write_report
from deployment.deployment_target import inspect_stack, safe_outputs
from deployment.installation import load_installation

# The question deployment/acceptance/check_public_sop_browser.cjs sends and the answer topics it requires; a test keeps
# them identical. The published guardrail refused that check's earlier wording as a prompt attack (doc 00).
PUBLIC_SOP_QUESTION = (
    "Use the public guidance collection for this planning exercise. Give two brief considerations for household "
    "evacuation routes and transport preparation, and two for firefighter escape routes, safety zones and crew "
    "communications. Keep the answer under 250 words. Cite the official publications. Distinguish guidance from "
    "incident orders.")
PUBLIC_SOP_TOPICS = ("evacuat", "escape route", "communicat")


@dataclass(frozen=True)
class Fixture:
    """The completed local run the cases replay: its result, recorded initial plan and retained observations."""
    result: dict
    recorded_initial: dict | None
    observations: dict
    simulation: dict


def arguments(argv=None):
    """Parse the installation file, the receipt path and optional retained observations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--observations", type=Path,
                        help="Retained public snapshot content for concurrent supervisor regression; no provider calls")
    return parser.parse_args(argv)


def use_deployed_runtime(installation):
    """Point this process at the installation's profile and the deployed Runtime's model and guardrail settings."""
    target = installation.target
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        os.environ.pop(key, None)
    os.environ.update(AWS_PROFILE=installation.profile, AWS_DEFAULT_REGION=target.region)
    boto3.setup_default_session(profile_name=installation.profile, region_name=target.region)
    if boto3.client("sts").get_caller_identity()["Account"] != target.account:
        raise ValueError("Guidance evaluation profile belongs to another account")
    stack = inspect_stack(boto3.client("cloudformation"), target.stack("backend"), target)
    runtime = boto3.client("bedrock-agentcore-control").get_agent_runtime(
        agentRuntimeId=safe_outputs(stack)["RuntimeId"])
    environment = runtime["environmentVariables"]
    for name, value in environment.items():
        if name.endswith("_MODEL_ID") or name == "MODEL_TEMPERATURE" or name.startswith("DEPLOYED_Guardrail"):
            os.environ[name] = value
    os.environ.update(AGENTCORE_RUNTIME="true", DEPLOYED_DeploymentRegion=target.region,
                      PUBLIC_DEMO_KNOWLEDGE_BASE_ID=(
                          installation.public_knowledge_base_id or environment["PUBLIC_DEMO_KNOWLEDGE_BASE_ID"]),
                      RESPONSE_MODEL_ID=installation.agent_model_id,
                      COMM_MODEL_ID=installation.agent_model_id)


def planning_helpers():
    """Compose the API's retained-evidence readers without starting its HTTP host or loading provider settings."""
    from backend import simulation_jobs
    from backend.dependencies import InvestigationReaders
    from backend.evacuation import evidence, service
    from backend.investigation.saved import (
        MAX_PLANNING_CONTEXT_BYTES,
        evidence_context_for_result,
        initial_plan_context,
    )
    return SimpleNamespace(
        MAX_PLANNING_CONTEXT_BYTES=MAX_PLANNING_CONTEXT_BYTES,
        evidence_context_for_result=evidence_context_for_result, initial_plan_context=initial_plan_context,
        readers=InvestigationReaders(context=service.context, owned_job=simulation_jobs.owned_job,
                                     build_evidence=evidence.build_evidence,
                                     planning_summary=evidence.planning_summary))


def load_runtime():
    """Import the checkout's Runtime agents and planning helpers the cases call, with the generated schema models."""
    for folder in ("backend/runtime", "traffic-simulator", "fire-risk-modeler", "."):
        sys.path.insert(0, str(ROOT / folder))
    from schema_models import generate_models
    models = generate_models(ROOT / "schemas/api-schema.yaml")
    from agents import supervisor_agent
    from agents.detection_agent.coordinate_grounding import CoordinateGrounding
    from agents.response_agent.response_agent import GuidanceSelectionGuard, build_public_response_tool
    from strands import tool
    from utils.structured_response import (
        PublicAnswerResponse,
        bind_guidance_citations,
        format_context,
        investigation_memory_context,
    )

    return SimpleNamespace(
        models=models, supervisor_agent=supervisor_agent, CoordinateGrounding=CoordinateGrounding,
        GuidanceSelectionGuard=GuidanceSelectionGuard, build_public_response_tool=build_public_response_tool,
        tool=tool, PublicAnswerResponse=PublicAnswerResponse, bind_guidance_citations=bind_guidance_citations,
        format_context=format_context, investigation_memory_context=investigation_memory_context,
        **vars(planning_helpers()))


def load_fixture(observations_path, runtime):
    """Read the completed local run named by SIMULATION_FIXTURE and the retained observations the cases replay."""
    fixture = os.environ.get("SIMULATION_FIXTURE")
    if not fixture or Path(fixture).name != fixture:
        raise ValueError("Set SIMULATION_FIXTURE to a completed local run ID")
    fixture_dir = ROOT / "traffic-simulator/.runtime/runs" / fixture
    result = json.loads(gzip.decompress((fixture_dir / "result.json.gz").read_bytes()))
    initial_fixture = fixture_dir / "initial-investigation.json"
    recorded_initial = json.loads(initial_fixture.read_text()) if initial_fixture.exists() else None
    if recorded_initial is not None and recorded_initial.get("status") != "completed":
        raise ValueError("The retained initial investigation fixture must be completed")
    observations = (json.loads(observations_path.read_text()) if observations_path else
                    (recorded_initial or {}).get("snapshot", {}).get("content"))
    if not observations or not observations.get("mapData") or not observations.get("forecastData"):
        raise ValueError("Supervisor precheck requires retained map/weather and forecast observations")
    return Fixture(result, recorded_initial, observations,
                   runtime.evidence_context_for_result(str(uuid4()), result, runtime.readers))


def instrument(stack, runtime, report):
    """Record source-tool rejections and guardrail blocks in report while stack is open; it restores the Runtime."""
    def traced(original):
        def before(self, event):
            original(self, event)
            if event.cancel_tool:
                report["source_rejections"].append(str(event.cancel_tool).split(":", 1)[0])
        return before

    original_model = runtime.supervisor_agent.create_model

    def traced_model(*args, **kwargs):
        """Retain policy decisions on failures, without prompts or streaming source text."""
        model = original_model(*args, **kwargs)
        original_stream = model.stream

        def blocked_rules(value):
            if isinstance(value, dict):
                if value.get("action") == "BLOCKED":
                    return [{key: value[key] for key in ("type", "name", "confidence", "filterStrength", "action")
                             if key in value}]
                return [rule for child in value.values() for rule in blocked_rules(child)]
            return [rule for child in value for rule in blocked_rules(child)] if isinstance(value, list) else []

        async def stream(*args, **kwargs):
            async for chunk in original_stream(*args, **kwargs):
                guardrail = chunk.get("metadata", {}).get("trace", {}).get("guardrail", {})
                rules = blocked_rules(guardrail)
                if rules:
                    report["guardrail_blocks"].append({"agent": args[0] if args and isinstance(args[0], str) else
                                                       "supervisor", "rules": rules})
                yield chunk

        model.stream = stream
        return model

    for hook in (runtime.GuidanceSelectionGuard, runtime.CoordinateGrounding):
        stack.enter_context(patch.object(hook, "before", traced(hook.before)))
    stack.enter_context(patch.object(runtime.supervisor_agent, "create_model", traced_model))


def guidance_cases(runtime, fixture, report, save):
    """Run real guidance selection for the initial and updated plans; each must cite and rejoin its receipt."""
    result, initial = fixture.result, None
    for name, context in (
        ("initial", {"verified_location": {"lat": result["scenario"]["lat"],
                                           "lon": result["scenario"]["lon"]}}),
        ("updated", fixture.simulation),
    ):
        if name == "updated":
            context.update(runtime.initial_plan_context(initial))
        if len(json.dumps(context, separators=(",", ":")).encode()) > runtime.MAX_PLANNING_CONTEXT_BYTES:
            raise RuntimeError(f"Real {name} guidance context exceeded the API bound")
        citations, failures, assessments = [], [], []
        # Bound as the supervisor's planning turn binds it, so this sends production's planning request.
        question = ("Select published planning guidance relevant to resident evacuation, crew access, "
                    "communication and unresolved information. Model exits/staging are not safety clearance.")
        guidance_tool = runtime.build_public_response_tool(citations, failures, assessments=assessments,
                                                           planning_context=context, question=question)
        answer = guidance_tool(question)
        if failures or not citations or not assessments or answer.get("guidance_unavailable"):
            raise RuntimeError(f"Real {name} guidance selection failed")
        if "Planning guidance — scenario interpretation" not in answer["response_text"]:
            raise RuntimeError(f"Real {name} guidance did not synthesize a cited scenario plan")
        if guidance_tool("Repeat this same guidance") != answer:
            raise RuntimeError("Repeated guidance did not rejoin its invocation receipt")
        if name == "initial":
            initial = fixture.recorded_initial or {"status": "completed", "task_id": str(uuid4()), "snapshot": {
                "type": "structuredResponse", "content": {"response": answer["response_text"],
                "knowledgeBaseCitations": answer["knowledgeBaseCitations"]}}}
        report["cases"].append({"name": name, "status": "passed", "answer": answer})
        save()


def public_sop_case(runtime, report, save):
    """Ask the public SOP browser check's question as a new visitor; it needs a cited answer on every topic."""
    supervisor_agent = runtime.supervisor_agent
    case = {"name": "public-sop-question", "status": "failed"}
    source_location = {}
    try:
        topics = supervisor_agent.select_investigation_topics(
            PUBLIC_SOP_QUESTION, {}, [], source_location=source_location)
    except ValueError as exc:
        # A guardrail refusal leaves no plan; the report's guardrail_blocks name the rule.
        report["cases"].append(case | {"error": str(exc)})
        save()
        raise RuntimeError("Public SOP question selected no task scope; inspect its retained receipt") from exc
    citations, failures, assessments, observations = [], [], [], {}
    supervisor = supervisor_agent.create_supervisor_agent(
        public_demo=True, guidance_citations=citations, guidance_failures=failures,
        guidance_assessments=assessments, observations=observations, previous_observations={},
        requested_topics=topics, memory_context=runtime.investigation_memory_context([]),
        source_location=source_location, user_question=PUBLIC_SOP_QUESTION)
    answer = supervisor(runtime.format_context(PUBLIC_SOP_QUESTION, None),
                        structured_output_model=runtime.PublicAnswerResponse)
    completion = answer.structured_output.model_dump() if answer.structured_output else None
    case |= {"selected_topics": sorted(topics), "stop_reason": answer.stop_reason, "completion": completion}
    final = None
    if (topics == {"guidance"} and completion and completion["content"]["response"] == "source_results_ready"
            and citations and assessments and not failures):
        final = runtime.bind_guidance_citations(completion, citations, observations=observations,
                                                assessments=assessments)
    text = final["content"]["response"] if final else ""
    missing = [topic for topic in PUBLIC_SOP_TOPICS if not re.search(topic, text, re.IGNORECASE)]
    if final is None or missing:
        report["cases"].append(case | {"error": "No cited guidance answer on every topic", "missing_topics": missing})
        save()
        raise RuntimeError("Public SOP question precheck failed; inspect its retained receipt")
    report["cases"].append(case | {"status": "passed", "answer": final})
    save()


def retained_detection_tool(runtime, observations):
    """Return a detection tool that answers every query with the retained fire, weather and forecast evidence."""
    @runtime.tool
    def retained_detection(query: str, requested_topics: list[str] | None = None,
                           coordinates_query: str | None = None) -> dict:
        """Return explicitly recorded fire/weather/forecast evidence for this offline-provider regression."""
        return {"mapData": observations["mapData"], "forecastData": observations["forecastData"]}
    return retained_detection


def detection_receipt(runtime, receipt, calls):
    """Return a detection_agent_tool that records each query in calls and answers with a copy of receipt."""
    @runtime.tool(name="detection_agent_tool")
    def topic_detection(query: str, requested_topics: list[str] | None = None,
                        source_location: dict | None = None,
                        coordinates_query: str | None = None) -> dict:
        """Retrieve only the requested topic using explicitly retained test observations."""
        calls.append(query)
        return deepcopy(receipt)
    return topic_detection


def concurrent_supervisor_cases(runtime, fixture, report, save):
    """Run two real supervisors at once on the retained observations; each must keep its receipts and guidance."""
    supervisor_agent = runtime.supervisor_agent

    def check_supervisor(index):
        citations, failures, assessments, received = [], [], [], {}
        point = fixture.result["scenario"]
        question = (
            f"At verified scenario coordinates ({point['lat']}, {point['lon']}), Paradise, CA. "
            "What nearby reported fires within 50 miles, current point weather and 2-day "
            "forecast are available? How should published guidance inform resident evacuation, "
            "crew access, staging, communication and remaining information gaps? "
            "This prerecorded-radio planning scenario is not confirmation of a fire at the scenario point. "
            "Road preparation runs separately; no simulation results are available yet.")
        topics = supervisor_agent.select_investigation_topics(question)
        supervisor = supervisor_agent.create_supervisor_agent(
            public_demo=True, guidance_citations=citations, guidance_failures=failures,
            guidance_assessments=assessments, observations=received, requested_topics=topics,
            user_question=question)
        answer = supervisor(question, structured_output_model=runtime.PublicAnswerResponse)
        case = {"name": f"concurrent-supervisor-{index}", "status": "failed",
                "provider_mode": "explicit retained observations", "stop_reason": answer.stop_reason,
                "observations_received": sorted(received)}
        if answer.structured_output is None or failures or not citations or not assessments:
            return case | {"error": "Validated output and supported guidance are required"}
        final = runtime.bind_guidance_citations(answer.structured_output.model_dump(), citations,
                                                observations=received, assessments=assessments)
        if (not received.get("mapData") or not received.get("forecastData")
                or not final["content"]["response"].endswith(assessments[-1])):
            return case | {"error": "Provider/specialist receipts were not preserved"}
        return case | {"status": "passed", "answer": final}

    with ThreadPoolExecutor(max_workers=2) as pool:
        for case in pool.map(check_supervisor, range(2)):
            report["cases"].append(case)
            save()
            if case["status"] != "passed":
                raise RuntimeError("Concurrent supervisor precheck failed; inspect its retained receipt")


def recorded_providers(runtime, observations, fire_data, weather_data, point):
    """Return provider tools answering from retained observations, and the lists recording what they were asked."""
    from agents.detection_agent import detection_agent
    enrichment, extra_providers, weather_points = [], [], []

    @runtime.tool(name="fire_weather_tool")
    def recorded_fire(latitude: float, longitude: float, radius_miles: float = 50,
                      days: int = 2, include_weather: bool = True) -> dict:
        """Retrieve reported fires. Set include_weather=False for fire-only questions."""
        enrichment.append(include_weather)
        return deepcopy(fire_data)

    @runtime.tool(name="geocode-service___geocode_address")
    def recorded_geocode(address: str) -> dict:
        """Resolve a named place using explicitly retained test coordinates."""
        return {"provider": "Amazon Location", "candidates": [
            {"lat": point["latitude"], "lon": point["longitude"],
             "score": 99, "matched_address": "Retained fixture point"}]}

    @runtime.tool(name="weather-dtn-service___get_dtn_current_weather")
    def recorded_weather(locations: list[dict]) -> dict:
        """Retrieve current weather at requested coordinates."""
        extra_providers.append("weather")
        weather_points.extend(deepcopy(locations))
        return deepcopy(weather_data)

    @runtime.tool(name="weather-dtn-service___get_dtn_forecast")
    def recorded_forecast(latitude: float, longitude: float, forecastDays: int = 2) -> dict:
        """Retrieve a forecast at requested coordinates."""
        extra_providers.append("forecast")
        return {"locations": [{**point, "forecastData": deepcopy(observations["forecastData"])}]}

    client = SimpleNamespace(list_tools_sync=lambda: [recorded_geocode, recorded_weather, recorded_forecast])
    return SimpleNamespace(detection_agent=detection_agent, fire=recorded_fire, client=client,
                           enrichment=enrichment, extra_providers=extra_providers, weather_points=weather_points)


def nested_fire_only_case(providers, query, report):
    """Run the real detection model on the supervisor's fire-only query; it must not ask for weather or forecast."""
    detection_agent = providers.detection_agent
    with patch.object(detection_agent, "build_fire_weather_tool", lambda _client: providers.fire), \
         patch.object(detection_agent, "create_mcp_client", return_value=nullcontext(providers.client)):
        nested = detection_agent.detection_agent_tool(query, requested_topics=["fires"])
    if not providers.enrichment or any(providers.enrichment) or providers.extra_providers:
        raise RuntimeError("Real detection model fetched unrequested weather or forecast")
    report["cases"].append({
        "name": "nested-fire-only", "status": "passed",
        "provider_mode": "explicit retained observations",
        "include_weather": providers.enrichment.copy(), "weather_forecast_calls": providers.extra_providers.copy(),
        "observations": nested.get("mapData")})


def nested_weather_cases(runtime, providers, question, source_location, point, report):
    """Run the real detection model for bound-point weather, then for every observation topic of one request."""
    detection_agent, weather_points = providers.detection_agent, providers.weather_points
    with patch.object(detection_agent, "create_mcp_client", return_value=nullcontext(providers.client)):
        nested = detection_agent.detection_agent_tool(
            "Current weather near Grass Valley CA at the LIME fire",
            requested_topics=["weather"], source_location=source_location,
            coordinates_query=question)
    if (not weather_points or any(
            abs(loc[key]-point[key]) > .000001 for loc in weather_points
            for key in ("latitude", "longitude"))
            or not nested.get("mapData") or any(
                nested["location"][key] != point[key] for key in ("latitude", "longitude"))):
        raise RuntimeError("Real detection model did not use the bound search point")
    json.dumps(nested)  # Generated enums must not force SDK Memory into Python repr.
    report["cases"].append({
        "name": "nested-bound-weather", "status": "passed",
        "provider_mode": "explicit retained observations",
        "requested_points": weather_points, "location": nested["location"],
        "json_serializable": True, "briefing": nested["response_text"]})
    if not nested["response_text"].strip():
        raise RuntimeError("Real weather specialist returned no narrative briefing")
    if "grass valley" in nested["response_text"].lower():
        raise RuntimeError("Weather briefing repeated a place conflicting with its bound source point")
    if any(word in nested["response_text"].lower() for word in ("hdw", "hot-dry-wind", "hot dry wind")):
        raise RuntimeError("Weather briefing interpreted a provider index without an interpretation contract")
    providers.extra_providers.clear()
    providers.enrichment.clear()
    with patch.object(detection_agent, "build_fire_weather_tool", lambda _client: providers.fire), \
         patch.object(detection_agent, "create_mcp_client", return_value=nullcontext(providers.client)):
        nested = detection_agent.detection_agent_tool(
            "Check reported fires, current weather and forecast at the retained point. "
            "How should published guidance inform evacuation, crew access, staging, "
            "communication and remaining information gaps?",
            requested_topics=["fires", "weather", "forecast"], source_location=source_location)
    if (not nested["response_text"].strip()
            or set(providers.extra_providers) != {"weather", "forecast"} or not providers.enrichment
            or not {"fires", "weather", "forecast"} <=
            runtime.supervisor_agent.observation_topics(nested)):
        raise RuntimeError("Real detection completed without every selected provider receipt")
    if re.search(r"\bguidance\b|\bstaging\b|\bcrew\b|\bevacuat|\bICS\b|\bCAL FIRE\b",
                 nested["response_text"], re.IGNORECASE):
        raise RuntimeError("Combined-request weather briefing leaked planning commentary")
    report["cases"].append({
        "name": "nested-all-observations", "status": "passed",
        "provider_mode": "explicit retained observations",
        "completed_topics": sorted(runtime.supervisor_agent.observation_topics(nested)),
        "briefing": nested["response_text"]})


def routing_cases(runtime, fixture, report, save):
    """Replay nine follow-up turns through real supervisors; each must select exactly its topics and specialists.

    Returns the retained observations, the native message history and the fire-only observations.
    """
    supervisor_agent, observations = runtime.supervisor_agent, fixture.observations
    # Model routing and final assembly must also work without a guidance call.
    point = {"latitude": fixture.result["scenario"]["lat"], "longitude": fixture.result["scenario"]["lon"]}
    fire_data = deepcopy(observations["mapData"])
    for location in fire_data["locations"]:
        location.pop("currentWeather", None)
    weather_data = {"locations": [
        {key: deepcopy(value) for key, value in loc.items() if key != "fire"}
        for loc in observations["mapData"]["locations"] if loc.get("currentWeather")]}
    providers = recorded_providers(runtime, observations, fire_data, weather_data, point)
    previous, history = {}, []
    for name, question, receipt, expected_topics in (
        ("fire-only", "Is there a fire going near sacramento ca",
         {"location": point, "mapData": fire_data}, {"fires"}),
        ("weather-follow-up", "What is the weather there?",
         {"location": point, "mapData": weather_data}, {"weather"}),
        ("forecast-follow-up", "What about tomorrow?",
         {"location": point, "forecastData": observations["forecastData"]}, {"forecast"}),
        ("current-versus-forecast", "Compare the weather now with tomorrow's forecast there.",
         {"location": point, "mapData": weather_data, "forecastData": observations["forecastData"]},
         {"weather", "forecast"}),
        ("guidance-follow-up", "Give me response guidance.",
         {}, {"guidance"}),
        ("fire-and-weather", "Check nearby fires and the current weather, without response guidance.",
         {"location": point, "mapData": observations["mapData"]}, {"fires", "weather"}),
        ("fire-after-guidance", "Is there a fire going near sacramento ca",
         {"location": point, "mapData": fire_data}, {"fires"}),
        ("fire-without-advice", "Just locate nearby fires; do not give me advice.",
         {"location": point, "mapData": fire_data}, {"fires"}),
        ("combined", "Check the fire and current weather near Sacramento CA and build response guidance.",
         {"location": point, "mapData": observations["mapData"]}, {"fires", "weather", "guidance"}),
    ):
        calls = []
        with patch.object(supervisor_agent, "detection_agent_tool", detection_receipt(runtime, receipt, calls)):
            citations, failures, assessments, received = [], [], [], {}
            # Guidance deliberately has no application snapshot: Memory must supply its context.
            retained = {} if name == "guidance-follow-up" else previous
            source_location = {}
            topics = supervisor_agent.select_investigation_topics(
                question, retained, history, source_location=source_location)
            if name in {"weather-follow-up", "forecast-follow-up", "current-versus-forecast"} and (
                    not source_location or any(source_location[key] != point[key]
                                              for key in ("latitude", "longitude"))):
                raise RuntimeError(f"{name}: selection did not retain the exact search point")
            memory = runtime.investigation_memory_context(history)
            if name == "guidance-follow-up" and (
                    not memory["observations"] or not any(
                        source.get("forecastData") for source in memory["observations"])):
                raise RuntimeError("Guidance follow-up requires actual JSON source receipts from native history")
            wants_guidance = "guidance" in expected_topics
            wants_detection = bool(expected_topics-{"guidance"})
            supervisor = supervisor_agent.create_supervisor_agent(
                public_demo=True, guidance_citations=citations, guidance_failures=failures,
                guidance_assessments=assessments, observations=received, previous_observations=retained,
                requested_topics=topics, memory_context=memory, source_location=source_location,
                user_question=question)
            supervisor.messages = deepcopy(history)
            answer = supervisor(question, structured_output_model=runtime.PublicAnswerResponse)
            case = {"name": name, "status": "failed", "provider_mode": "explicit retained observations",
                    "selected_topics": sorted(topics),
                    "source_location": source_location,
                    "detection_calls": len(calls), "guidance_received": bool(assessments),
                    "stop_reason": answer.stop_reason,
                    "completion": answer.structured_output.model_dump() if answer.structured_output else None}
            if (topics != expected_topics or answer.structured_output is None or failures or
                    (bool(assessments) != wants_guidance) or
                    (bool(calls) != wants_detection)):
                report["cases"].append(case | {"error": "Wrong specialist selection or invalid completion"})
                save()
                raise RuntimeError(f"{name}: specialist selection or completion precheck failed")
            final = runtime.bind_guidance_citations(answer.structured_output.model_dump(), citations,
                                                    observations=received, assessments=assessments)
            if not wants_guidance and final["content"].get("knowledgeBaseCitations"):
                raise RuntimeError(f"{name} unexpectedly returned guidance citations")
            if name == "fire-only" and any(loc.get("currentWeather") for loc in final["content"]["mapData"]["locations"]):
                raise RuntimeError("Fire-only answer included unsolicited weather")
            if name == "fire-only":
                # Check the nested real detection model, not just supervisor selection.
                # Provider fixtures expose whether it asks for unrequested weather.
                nested_fire_only_case(providers, calls[0], report)
            if name == "weather-follow-up":
                nested_weather_cases(runtime, providers, question, source_location, point, report)
            if name == "weather-follow-up" and any(loc.get("fire") for loc in final["content"]["mapData"]["locations"]):
                raise RuntimeError("Weather-only answer repeated a fire search")
            previous = {**previous, **received}
            history = deepcopy(supervisor.messages)
            if name == "weather-follow-up":
                # The broker retains the earlier fire receipts alongside the new point weather.
                previous["mapData"] = {"summary": fire_data.get("summary"),
                                      "locations": fire_data["locations"] + weather_data["locations"]}
            report["cases"].append(case | {"status": "passed", "answer": final})
            save()
    return previous, history, fire_data


def location_selection_cases(runtime, fire, previous, history, report):
    """Record whether weather questions select the named incident, a new place, or no default map location."""
    supervisor_agent = runtime.supervisor_agent
    for name, question, expected_point in (
        ("named-fire-weather-target", f"What is the current weather at the {fire['fire']['name']} fire?",
         {key: fire[key] for key in ("latitude", "longitude")}),
        ("new-place-weather-target", "What is the current weather in Seattle WA?", None),
    ):
        selected_point = {}
        topics = supervisor_agent.select_investigation_topics(
            question, previous, history, source_location=selected_point)
        matches = (not selected_point if expected_point is None else all(
            selected_point.get(key) == value for key, value in expected_point.items()))
        report["cases"].append({"name": name, "selected_topics": sorted(topics),
                                "source_location": selected_point,
                                "status": "passed" if topics == {"weather"} and matches else "failed"})
    # Omitted location with no history must clarify rather than select the default map.
    topics = supervisor_agent.select_investigation_topics("What's the current weather?")
    report["cases"].append({"name": "missing-location", "selected_topics": sorted(topics),
                            "status": "passed" if not topics else "failed"})


def communication_case(runtime, fire, fire_data, history, report):
    """Draft multilingual voice messages for the discussed incident with the real guarded model and fixture media."""
    supervisor_agent = runtime.supervisor_agent
    from agents.communication_agent import communication_agent
    communication_receipts = []
    communication_question = f"Prepare multilingual voice drafts for the reported {fire['fire']['name']} fire."
    selected_point = {}
    selected_topics = supervisor_agent.select_investigation_topics(
        communication_question, {"mapData": fire_data}, history, source_location=selected_point)
    if selected_topics != {"communication"} or any(
            selected_point.get(key) != fire[key] for key in ("latitude", "longitude")):
        raise RuntimeError("Communication did not select the discussed incident without extra lookups")
    # Real guarded draft model; audio providers are explicit fixtures here. Deployment acceptance
    # verifies actual Translate/Polly/S3 playback separately.
    with patch.object(communication_agent, "generate_voice_messages", return_value={
            "success": True, "summary": {"successful": 3, "totalLanguages": 3, "failed": 0},
            "languages": {}}), patch.dict(os.environ, {"VOICE_MESSAGE_S3_BUCKET": "fixture-only"}):
        communication_tool = communication_agent.build_communication_tool(
            owner=("precheck", "precheck-session"), receipts=communication_receipts,
            question=communication_question,
            context={"selected_location": selected_point,
                     "previous_observations": {"mapData": fire_data},
                     "conversation_observations": [{
                         "location": {"latitude": 1, "longitude": 1},
                         "mapData": {"locations": [{"latitude": 1, "longitude": 1,
                             "currentWeather": {"temperature": {"value": 66.6, "unit": "degF"}}}]},
                         "forecastData": {"days": [{"summary": {
                             "highTemp": {"value": 99.9, "unit": "degF"}}}]}}]})
        drafted = communication_tool(
            "Prepare the voice draft. Current weather near this fire is 66.6 degF "
            "and forecast high 99.9 degF.")
    if (fire["fire"]["name"].casefold() not in drafted["response_text"].casefold()
            or not drafted.get("audioData")
            or re.search(r"66[.]6|99[.]9", drafted["response_text"])):
        raise RuntimeError("Communication draft lost incident identity or included unrelated weather")
    report["cases"].append({"name": "selected-incident-communication", "status": "passed",
                            "provider_mode": "real guarded model; fixture media",
                            "selected_topics": sorted(selected_topics), "answer": drafted})


def guardrail_cases(runtime, previous, report):
    """Record whether the latest-user and task-selection guardrails stop unrelated requests, and only those."""
    supervisor_agent = runtime.supervisor_agent
    # The latest-user guardrail must not reclassify earlier unrelated requests.
    topics = supervisor_agent.select_investigation_topics(
        "Find fires near Sacramento CA", messages=[
            {"role": "user", "content": [{"text": "Should I invest in Bitcoin?"}]}])
    report["cases"].append({"name": "valid-request-after-unrelated-history",
                            "selected_topics": sorted(topics),
                            "status": "passed" if topics == {"fires"} else "failed"})
    calls = []
    with patch.object(supervisor_agent, "detection_agent_tool", detection_receipt(runtime, {}, calls)):
        citations, failures, assessments = [], [], []
        supervisor = supervisor_agent.create_supervisor_agent(
            public_demo=True, guidance_citations=citations, guidance_failures=failures,
            guidance_assessments=assessments, observations={}, previous_observations=previous)
        blocked = supervisor(runtime.format_context("Should I invest in Bitcoin?", {
            "previous_observations": {}}), structured_output_model=runtime.PublicAnswerResponse)
    report["cases"].append({
        "name": "latest-user-guardrail", "stop_reason": blocked.stop_reason,
        "status": "passed" if blocked.stop_reason == "guardrail_intervened"
            and not calls and not assessments and blocked.structured_output is None else "failed"})
    try:
        supervisor_agent.select_investigation_topics("Should I invest in Bitcoin?")
        report["cases"].append({"name": "task-selection-guardrail", "status": "failed"})
    except ValueError as exc:
        if str(exc) != "Supervisor did not select a validated investigation task scope":
            raise
        report["cases"].append({"name": "task-selection-guardrail", "status": "passed"})


def run_cases(runtime, fixture, report, save):
    """Run every case in order: guidance, the public SOP question, then the supervisor, selection and guardrail cases
    on retained evidence."""
    guidance_cases(runtime, fixture, report, save)
    # The specialist alone misses supervisor history/output regressions. Reuse
    # actual retained observations with an explicit provider stub and real
    # guarded models; live provider acceptance remains a separate release gate.
    with patch.object(runtime.supervisor_agent, "detection_agent_tool",
                      retained_detection_tool(runtime, fixture.observations)):
        public_sop_case(runtime, report, save)
        concurrent_supervisor_cases(runtime, fixture, report, save)
        previous, history, fire_data = routing_cases(runtime, fixture, report, save)
        fire = next(loc for loc in fire_data["locations"] if loc.get("fire"))
        location_selection_cases(runtime, fire, previous, history, report)
        communication_case(runtime, fire, fire_data, history, report)
        guardrail_cases(runtime, previous, report)
        save()


def decide(report):
    """Pass only when every recorded case passed; otherwise fail and keep the receipt for review."""
    if any(case["status"] != "passed" for case in report["cases"]):
        raise RuntimeError("Supervisor routing precheck failed; inspect its retained receipt")
    report["status"] = "passed"


def main(argv=None):
    """Run the bounded guidance, supervisor and routing cases on the deployed Runtime's real models."""
    args = arguments(argv)
    installation = load_installation(args.installation, root=ROOT)
    use_deployed_runtime(installation)
    runtime = load_runtime()
    fixture = load_fixture(args.observations, runtime)
    report = {"status": "running", "model": installation.agent_model_id, "cases": [],
              "guardrail_blocks": [], "source_rejections": []}

    def save():
        write_report(args.output, report)

    try:
        with ExitStack() as stack:
            instrument(stack, runtime, report)
            run_cases(runtime, fixture, report, save)
        decide(report)
    except Exception:
        report["status"] = "failed"
        raise
    finally:
        save()
    print("Real Bedrock guidance selection checks passed:", args.output)
    if runtime.models is not None:
        runtime.models.cleanup()


if __name__ == "__main__":
    main()
