"""Source outcomes preserve evidence and permit honest partial completion."""
import json
import logging
import sys
import types
import unittest
from copy import deepcopy
from unittest.mock import MagicMock, patch

from checks_support import load, runtime_stubs, scripted_agent, stub_module
from test_runtime_detection_result import load_detection_agent

POINT = {"latitude": 38.5, "longitude": -121.5}
FIRE_DATA = {"summary": {"totalActiveFires": 1}, "locations": [{
    **POINT, "fire": {"sourceId": "fixture-source-1", "name": "Fixture fire", "lastUpdated": "2026-10-30T00:00:00Z"},
}]}


def result_event(name, data=None, *, failure=False, arguments=None):
    return types.SimpleNamespace(
        tool_use={"name": name, "input": arguments or {}},
        result={"status": "error" if failure else "success",
                "content": [{"text": "Fixture provider failure" if failure else json.dumps(data)}]},
    )


class SourceAvailabilityChecks(unittest.TestCase):
    def setUp(self):
        with patch.dict(sys.modules, runtime_stubs()):
            self.grounding = load("checked_partial_grounding",
                                  "backend/runtime/agents/detection_agent/coordinate_grounding.py")
            self.binding = load("checked_partial_binding", "backend/runtime/utils/structured_response.py")

    def test_failed_weather_keeps_fire_receipt_and_can_finish_without_repeating_the_source(self):
        hook = self.grounding.CoordinateGrounding("Fires and weather at the fixture point",
                                                requested_topics={"fires", "weather"}, source_location=POINT)
        hook.after(result_event("fire_weather_tool", deepcopy(FIRE_DATA), arguments={"include_weather": False}))
        pending = types.SimpleNamespace(tool_use={"name": "ObservationBriefing", "input": {}}, cancel_tool=None)
        hook.before(pending)
        self.assertTrue(pending.cancel_tool, "An unattempted source is still required")
        hook.after(result_event(self.grounding.DTN_CURRENT, failure=True, arguments={"locations": [POINT]}))
        finished = types.SimpleNamespace(tool_use={"name": "ObservationBriefing", "input": {}}, cancel_tool=None)
        hook.before(finished)
        self.assertIsNone(finished.cancel_tool)
        final = self.binding.bind_guidance_citations(
            {"type": "structuredResponse", "content": {"response": "source_results_ready"}}, [],
            observations=hook.observations,
        )["content"]
        self.assertEqual(final["mapData"], FIRE_DATA)
        self.assertEqual(final["sourceAvailability"]["fires"], {"status": "available", "code": None})
        self.assertEqual(final["sourceAvailability"]["weather"],
                         {"status": "unavailable", "code": "SOURCE_UNAVAILABLE"})
        self.assertIn("Current weather unavailable", final["response"])
        self.assertIn("fixture-source-1", json.dumps(final["mapData"]))
        retry = types.SimpleNamespace(tool_use={"name": self.grounding.DTN_CURRENT,
                                               "input": {"locations": [POINT]}}, cancel_tool=None)
        hook.before(retry)
        self.assertIn("LOOKUP_ENDED", retry.cancel_tool)

    def test_failed_fire_lookup_never_becomes_zero_fires_and_fake_availability_is_ignored(self):
        hook = self.grounding.CoordinateGrounding("Reported fires", requested_topics={"fires"}, source_location=POINT)
        hook.after(result_event("fire_weather_tool", failure=True))
        self.assertNotIn("mapData", hook.observations)
        self.assertEqual(self.binding.observation_outcomes(hook.observations), {"location", "fires"})
        self.assertIn("Reported fire source unavailable", self.binding.observation_text(hook.observations))
        self.assertNotIn("No matching fire records", self.binding.observation_text(hook.observations))
        self.assertNotIn("fires", self.binding.observation_outcomes(
            {"sourceAvailability": {"fires": {"status": "available", "code": None}}}))

    def test_budget_exhausted_weather_is_a_source_failure_not_a_measurement(self):
        hook = self.grounding.CoordinateGrounding("Current weather", requested_topics={"weather"}, source_location=POINT)
        hook.after(result_event(self.grounding.DTN_CURRENT, {"locations": [{
            **POINT, "error": "Weather operation budget exhausted", "errorCode": "WEATHER_BUDGET_EXHAUSTED",
        }]}))
        self.assertEqual(self.binding.source_availability(hook.observations)["weather"],
                         {"status": "unavailable", "code": "WEATHER_BUDGET_EXHAUSTED"})
        self.assertNotIn("weather", self.binding.observation_topics(hook.observations))

    def test_valid_weather_scope_needs_neither_forecast_nor_geocoder(self):
        detection = load_detection_agent("checked_scope_partial_inventory")
        client = MagicMock()
        client.__enter__.return_value.list_tools_sync.return_value = [
            types.SimpleNamespace(name=self.grounding.DTN_CURRENT)]
        offered = []

        class WeatherAgent(scripted_agent({"response_text": "Current source conditions."})):
            def __init__(inner, **kwargs):
                offered.extend(tool.name for tool in kwargs["tools"])
                inner.grounding = kwargs["hooks"][0]

            def __call__(inner, *args, **kwargs):
                inner.grounding.after(result_event(self.grounding.DTN_CURRENT, {"locations": [{
                    **POINT, "currentWeather": {"temperature": {"value": 65, "unit": "degF"}},
                }]}))
                return super().__call__(*args, **kwargs)

        with patch.object(detection, "create_mcp_client", return_value=client), \
                patch.object(detection, "CoordinateGrounding", self.grounding.CoordinateGrounding), \
                patch.object(detection, "Agent", WeatherAgent):
            receipt = detection.detection_agent_tool("Current weather", requested_topics=["weather"],
                                                     source_location=POINT)
        self.assertEqual(offered, [self.grounding.DTN_CURRENT])
        self.assertEqual(receipt["sourceAvailability"]["weather"], {"status": "available", "code": None})

    def test_specialist_formatting_failure_preserves_earlier_fire_data_and_safe_logs(self):
        detection = load_detection_agent("checked_partial_specialist_failure")
        client = MagicMock()
        client.__enter__.return_value.list_tools_sync.return_value = [
            types.SimpleNamespace(name=name) for name in (
                self.grounding.DTN_CURRENT, self.grounding.DTN_FORECAST)]
        canary = "SYNTHETIC-PRIVATE-EXCEPTION-CANARY"

        class FailedFormattingAgent:
            def __init__(inner, **kwargs):
                inner.grounding = kwargs["hooks"][0]

            def __call__(inner, *_args, **_kwargs):
                inner.grounding.after(result_event("fire_weather_tool", deepcopy(FIRE_DATA),
                                                  arguments={"include_weather": False}))
                raise RuntimeError(canary)

        with patch.object(detection, "create_mcp_client", return_value=client), \
                patch.object(detection, "CoordinateGrounding", self.grounding.CoordinateGrounding), \
                patch.object(detection, "Agent", FailedFormattingAgent), \
                self.assertLogs(detection.logger, level="DEBUG") as logs:
            receipt = detection.detection_agent_tool(
                "Reported fires and current weather", requested_topics=["fires", "weather"], source_location=POINT)
        self.assertEqual(receipt["mapData"], FIRE_DATA)
        self.assertEqual(receipt["sourceAvailability"]["weather"]["code"], "LOOKUP_INTERRUPTED")
        formatted = "\n".join(logging.Formatter().format(record) for record in logs.records)
        self.assertNotIn(canary, formatted)
        self.assertIn("RuntimeError", formatted)

    def test_supervisor_reuses_partial_receipt_and_accepts_complete_source_outcomes(self):
        detection = MagicMock(return_value={
            "mapData": FIRE_DATA, "location": POINT, "sourceAvailability": {
                "fires": {"status": "available", "code": None},
                "weather": {"status": "unavailable", "code": "SOURCE_UNAVAILABLE"}},
        })
        with patch.dict(sys.modules, runtime_stubs(**{
                "agents.detection_agent.detection_agent": stub_module(
                    "detection_agent", detection_agent_tool=detection),
                "agents.response_agent.response_agent": stub_module(
                    "response_agent", build_public_response_tool=lambda *_args, **_kwargs: None),
        })):
            supervisor = load("checked_partial_supervisor", "backend/runtime/agents/supervisor_agent.py")
        with patch.object(supervisor, "Agent", side_effect=lambda **kwargs: kwargs):
            observations, hooks = {}, []
            agent = supervisor.create_supervisor_agent(requested_topics={"fires", "weather"},
                                                       observations=observations, hooks=hooks)
        tool = agent["tools"][0]
        first = tool("Requested observations")
        self.assertEqual(tool("Repeat"), first)
        detection.assert_called_once()
        event = types.SimpleNamespace(tool_use={"name": "PublicAnswerResponse",
                                               "input": {"content": {"response": "source_results_ready"}}},
                                      cancel_tool=None)
        hooks[0].before(event)
        self.assertIsNone(event.cancel_tool)
        self.assertEqual(observations["mapData"], FIRE_DATA)

    def test_empty_scope_can_clarify_and_selected_unresolved_source_finishes_as_explicit_unavailable(self):
        receipt = {"sourceAvailability": {"weather": {"status": "unavailable", "code": "LOCATION_UNRESOLVED"}}}
        detection = MagicMock(return_value=receipt)
        with patch.dict(sys.modules, runtime_stubs(**{
                "agents.detection_agent.detection_agent": stub_module(
                    "detection_agent", detection_agent_tool=detection),
                "agents.response_agent.response_agent": stub_module(
                    "response_agent", build_public_response_tool=lambda *_args, **_kwargs: None),
        })):
            supervisor = load("checked_scope_clarification", "backend/runtime/agents/supervisor_agent.py")
        observations, hooks = {}, []
        with patch.object(supervisor, "Agent", side_effect=lambda **kwargs: kwargs):
            supervisor.create_supervisor_agent(requested_topics=set(), hooks=hooks)
        event = types.SimpleNamespace(tool_use={"name": "PublicAnswerResponse",
                                               "input": {"content": {"response": "location_required"}}},
                                      cancel_tool=None)
        hooks[0].before(event)
        self.assertIsNone(event.cancel_tool)
        hooks = []
        with patch.object(supervisor, "Agent", side_effect=lambda **kwargs: kwargs):
            agent = supervisor.create_supervisor_agent(requested_topics={"weather"},
                                                       observations=observations, hooks=hooks)
        hooks[0].before(event)
        self.assertIn("SELECTED_TASKS_REQUIRED", event.cancel_tool)
        agent["tools"][0]("Current weather at unresolved place")
        event.tool_use["input"]["content"]["response"] = "source_results_ready"
        event.cancel_tool = None
        hooks[0].before(event)
        self.assertIsNone(event.cancel_tool)
        final = self.binding.bind_guidance_citations(
            {"type": "structuredResponse", "content": {"response": "source_results_ready"}}, [],
            observations=observations,
        )["content"]
        self.assertEqual(final["sourceAvailability"], receipt["sourceAvailability"])
        self.assertIsNone(final["mapData"])
        self.assertIn("Current weather unavailable (LOCATION_UNRESOLVED)", final["response"])
        detection.assert_called_once()
