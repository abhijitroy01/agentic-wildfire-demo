"""Runtime checks: the detection agent's fire and weather results."""
import json
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from checks_support import PLANTED_ADDRESS, load, runtime_stubs, scripted_agent, stub_module
from pydantic import BaseModel


def load_detection_agent(name):
    """Load the detection agent by path with stand-ins for its Gateway client, fire-weather tool and grounding."""
    stubs = runtime_stubs(**{
        "utils.mcp_client": stub_module("utils.mcp_client", create_mcp_client=MagicMock()),
        "agents.detection_agent.tools.fire_weather_tool": stub_module(
            "fire_weather_tool", build_fire_weather_tool=lambda _client: MagicMock()),
        "agents.detection_agent.coordinate_grounding": stub_module(
            "coordinate_grounding", CoordinateGrounding=lambda _, **kwargs: types.SimpleNamespace(observations={})),
    })
    with patch.dict(sys.modules, stubs):
        return load(name, "backend/runtime/agents/detection_agent/detection_agent.py")


class DetectionResultChecks(unittest.TestCase):
    """Fire ranking, capped fire lists and answers without a location (G28)."""

    def setUp(self):
        """Load the fire-weather detection tool; each test binds it to a stand-in Gateway client."""
        with patch.dict(sys.modules, runtime_stubs()):
            self.fire = load("checked_fire_weather", "backend/runtime/agents/detection_agent/tools/fire_weather_tool.py")

    def test_zero_percent_containment_ranks_as_uncontained(self):
        """Check fire priority treats 0% containment as uncontained and 100% as none."""
        priority = self.fire.calculate_priority
        self.assertAlmostEqual(priority(0, 0), 0.4)
        self.assertAlmostEqual(priority(10000, 0), 1.0)
        self.assertEqual(priority(0, 100), 0)
        self.assertEqual(priority(0, None), 0)  # unknown containment keeps its old meaning
        self.assertGreater(priority(500, 0), priority(500, 50))

    def test_nearby_fires_are_selected_before_distant_large_fires_and_invalid_points_are_skipped(self):
        features = [
            {"attributes": {"DailyAcres": 10000, "PercentContained": 0}, "geometry": {"x": 20, "y": 0}},
            {"attributes": {"DailyAcres": 0, "CalculatedAcres": 10000}, "geometry": {"x": 0, "y": 0}},
            {"attributes": {}, "geometry": {"x": 0, "y": float("nan")}},
            {"attributes": {}, "geometry": {"x": True, "y": 0}},
        ]
        selected = self.fire.presort_fire_features(features, 0, 0, limit=1)
        self.assertEqual(selected[0]["_lon"], 0)
        self.assertEqual(selected[0]["_priority"], 0)
        self.assertEqual(self.fire.convert_timestamp(0), "1970-01-01T00:00:00+00:00")

    def test_source_identifier_preserves_provider_identity_including_zero_object_id(self):
        feature = {"attributes": {"OBJECTID": 0}, "_lat": 0, "_lon": 0, "_distance": 0, "_priority": 0}
        self.fire.create_mapdata_from_presorted([feature], [], {"latitude": 0, "longitude": 0})
        self.assertEqual(self.fire.FireData.call_args.kwargs["sourceId"], "esri:OBJECTID:0")

    def test_missing_fire_measurements_remain_unknown_and_reported_zero_is_preserved(self):
        features = [{"attributes": attrs, "_lat": 34.1, "_lon": -118.5, "_distance": 1, "_priority": 0}
                    for attrs in ({}, {"DailyAcres": 0, "CalculatedAcres": 20, "PercentContained": 0},
                                  {"DailyAcres": None, "CalculatedAcres": 20, "PercentContained": 50})]
        self.fire.create_mapdata_from_presorted(features, [], {"latitude": 34.1, "longitude": -118.5})
        values = [call.kwargs for call in self.fire.FireData.call_args_list]
        self.assertEqual([(value["acresBurned"], value["percentContained"]) for value in values],
                         [(None, None), (0, 0), (20, 50)])

    def test_a_capped_fire_list_is_flagged_and_the_search_point_is_not_logged(self):
        """Check a truncated ArcGIS fire list is marked capped and coordinates stay out of logs."""
        feature = {"attributes": {"IncidentName": "Fixture Fire", "PercentContained": 0, "DailyAcres": 10},
                   "geometry": {"x": -118.5, "y": 34.1}}
        for wildfire, capped in (({"features": [feature], "exceededTransferLimit": True, "truncated": True}, True),
                                 ({"features": [feature]}, False)):
            session = MagicMock()
            session.call_tool_sync.side_effect = [
                {"content": [{"text": json.dumps(wildfire)}]},
                {"content": [{"text": json.dumps({"locations": [{"currentWeather": {}}]})}]}]
            with self.subTest(capped=capped), \
                 patch.object(self.fire, "create_mapdata_from_presorted", return_value={"locations": []}), \
                 self.assertLogs(level="DEBUG") as logs:
                result = self.fire.build_fire_weather_tool(session)(34.04118, -118.54988)
            self.assertEqual([call.kwargs["name"] for call in session.call_tool_sync.call_args_list],
                             ["wildfire-service___queryWildfires", "weather-dtn-service___get_dtn_current_weather"])
            self.assertEqual(result.get("truncated", False), capped)
            self.assertEqual(result.get("truncationNote"), self.fire.TRUNCATION_NOTE if capped else None)
            self.assertNotIn("34.04118", "\n".join(logs.output))

    def test_fire_only_search_does_not_require_or_invoke_weather_provider(self):
        session = MagicMock()
        session.call_tool_sync.return_value = {"content": [{"text": json.dumps({"features": [
            {"attributes": {"IncidentName": "Fixture"}, "geometry": {"x": -121.5, "y": 38.6}}
        ]})}]}
        with patch.object(self.fire, "create_mapdata_from_presorted", return_value={"locations": []}) as format_data:
            self.fire.build_fire_weather_tool(session)(38.6, -121.5, include_weather=False)
        # The fire lookup reuses the detection agent's session; it never lists tools or opens a client.
        session.list_tools_sync.assert_not_called()
        self.assertEqual(session.call_tool_sync.call_count, 1)
        self.assertEqual(format_data.call_args.args[1], [])

    def test_reordered_weather_is_bound_to_exact_fire_coordinates_and_zero_is_kept(self):
        features = [{"attributes": {}, "_lat": lat, "_lon": lon, "_distance": 0, "_priority": 0}
                    for lat, lon in ((0, 0), (1, 1), (2, 2), (3, 3))]
        readings = [{"latitude": lat, "longitude": lat,
                     "currentWeather": {"wind": {"speed": {"value": lat, "unit": "mph"}}}}
                    for lat in (3, 0, 2, 1)]
        self.fire.create_mapdata_from_presorted(features, readings, {"latitude": 0, "longitude": 0})
        for index, call in enumerate(self.fire.FireLocation.call_args_list):
            weather = call.kwargs["currentWeather"]
            self.assertEqual(weather["queryLocation"], {"latitude": index, "longitude": index})
            self.assertEqual(weather["wind"]["speed"]["value"], index)

    def test_malformed_fire_timestamp_does_not_make_a_reported_fire_unusable(self):
        for value in (None, {}, [], True, "not-an-instant", "2026-10-27", float("inf")):
            with self.subTest(value=value):
                self.assertIsNone(self.fire.convert_timestamp(value))
        self.assertEqual(self.fire.convert_timestamp("2026-10-27T00:00:00Z"), "2026-10-27T00:00:00Z")

    def test_conflicting_duplicates_errors_and_malformed_weather_do_not_remove_fire_markers(self):
        class Weather(BaseModel):
            wind: dict

        features = [{"attributes": {}, "_lat": lat, "_lon": lat, "_distance": 0, "_priority": 0}
                    for lat in range(4)]
        good = {"wind": {"speed": {"value": 0, "unit": "mph"}}}
        readings = [
            {"latitude": 0, "longitude": 0, "currentWeather": good},
            {"latitude": 0, "longitude": 0, "currentWeather": {"wind": {"speed": {"value": 5, "unit": "mph"}}}},
            {"latitude": 1, "longitude": 1, "currentWeather": {"wind": "malformed"}},
            {"latitude": 2, "longitude": 2, "error": "Unavailable"},
            {"latitude": 3, "longitude": 3, "currentWeather": good, "status": "available"},
            {"latitude": 3, "longitude": 3, "currentWeather": good},
            {"latitude": True, "longitude": 1, "currentWeather": good},
            None,
        ]
        with patch.object(self.fire, "WeatherData", Weather):
            self.fire.create_mapdata_from_presorted(features, readings, {"latitude": 0, "longitude": 0})
        self.assertEqual(self.fire.FireLocation.call_count, 4)
        results = [call.kwargs["currentWeather"] for call in self.fire.FireLocation.call_args_list]
        self.assertEqual(results[:3], [None, None, None])
        self.assertEqual(results[3]["wind"]["speed"]["value"], 0)

    def test_an_answer_without_a_location_is_not_an_error_and_the_query_is_not_logged(self):
        """Check a reply with no place is answered normally and the user query is never logged."""
        detection = load_detection_agent("checked_detection_logs")
        client = MagicMock()
        client.__enter__.return_value.list_tools_sync.return_value = [types.SimpleNamespace(name=name) for name in (
            "geocode-service___geocode_address", "weather-dtn-service___get_dtn_forecast",
            "weather-dtn-service___get_dtn_current_weather")]
        query = f"Find fires near {PLANTED_ADDRESS}"
        for location in (None, {}, {"latitude": 34.04118, "longitude": -118.54988}):
            answer = {"query_type": "current", "response_text": "Which city do you mean?", "location": location}
            with self.subTest(location=location), patch.object(detection, "create_mcp_client", return_value=client), \
                 patch.object(detection, "CoordinateGrounding", return_value=types.SimpleNamespace(
                     observations={"location": location} if location else {})), \
                 patch.object(detection, "Agent", scripted_agent(answer)), self.assertLogs(level="DEBUG") as logs:
                self.assertEqual(detection.detection_agent_tool(query)["location"], location or None)
            text = "\n".join(logs.output)
            self.assertNotIn(PLANTED_ADDRESS, text)
            self.assertNotIn("34.04", text)
        with patch.object(detection, "create_mcp_client", return_value=client), \
                patch.object(detection, "Agent", scripted_agent(answer)):
            self.assertIsNone(detection.detection_agent_tool(query)["location"],
                              "Model-authored coordinates alone must not become a source location")

    def test_the_detection_agent_gets_only_its_tools_for_each_scope(self):
        """F3: the detection model reads provider and visitor text, so of the tools the Gateway lists it gets only
        the fire-weather wrapper and the allowlisted provider tools of its scope, never the wildfire or simulation
        tools."""
        geocode, current, forecast = ("geocode-service___geocode_address",
                                      "weather-dtn-service___get_dtn_current_weather",
                                      "weather-dtn-service___get_dtn_forecast")
        detection = load_detection_agent("checked_detection_tools")
        client = MagicMock()
        client.__enter__.return_value.list_tools_sync.return_value = [types.SimpleNamespace(name=name) for name in (
            geocode, current, forecast, "wildfire-service___queryWildfires", "simulation-service___prepare_simulation")]
        fire_weather = types.SimpleNamespace(name="fire_weather_tool")
        offered = []

        class RecordingAgent(scripted_agent({"query_type": "current", "response_text": "", "location": None})):
            def __init__(self, **kwargs):
                offered.append([tool.name for tool in kwargs["tools"]])

        for scope in (None, ["fires"], ["weather"], ["forecast"], ["location"], ["fires", "weather"]):
            topics = set(scope or ("fires", "weather", "forecast"))
            for located in (None, {"latitude": 34.04118, "longitude": -118.54988}):
                expected = ((["fire_weather_tool"] if "fires" in topics else []) + ([] if located else [geocode])
                            + [name for name, topic in ((current, "weather"), (forecast, "forecast")) if topic in topics])
                offered.clear()
                with self.subTest(scope=scope, located=bool(located)), \
                        patch.object(detection, "create_mcp_client", return_value=client), \
                        patch.object(detection, "build_fire_weather_tool", return_value=fire_weather), \
                        patch.object(detection, "CoordinateGrounding",
                                     return_value=types.SimpleNamespace(observations={})), \
                        patch.object(detection, "Agent", RecordingAgent):
                    detection.detection_agent_tool("Fires and weather near the fixture point",
                                                   requested_topics=scope, source_location=located)
                    self.assertEqual(offered, [expected])
