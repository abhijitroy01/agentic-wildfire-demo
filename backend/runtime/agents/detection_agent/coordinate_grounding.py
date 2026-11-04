"""Require source-backed coordinates before location-dependent provider calls."""
import json
import math
import re
import threading
from copy import deepcopy

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookProvider
from utils.gateway import DTN_CURRENT, DTN_FORECAST, GEOCODE, WEATHER_TOPICS, tool_payload
from utils.structured_response import (
    observation_outcomes,
    observation_topics,
    provider_records_for_reasoning,
    unavailable_sources,
)

WEATHER = set(WEATHER_TOPICS)
NUMBER = r"[-+]?\d{1,3}(?:\.\d+)?"
PAIR = re.compile(r"(?<![\w.])([-+]?\d{1,3}\.\d+)\s*,\s*([-+]?\d{1,3}\.\d+)(?!\w|\.\d)")
LABELED = re.compile(
    rf"\blat(?:itude)?\s*[:=]\s*({NUMBER})\s*[,; ]+\s*lon(?:gitude)?\s*[:=]\s*({NUMBER})(?!\w|\.\d)",
    re.IGNORECASE)


def point(latitude, longitude):
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        return None
    if not isinstance(latitude, (float, int)) or not isinstance(longitude, (float, int)):
        return None
    if not (math.isfinite(latitude) and math.isfinite(longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return (latitude, longitude)


class CoordinateGrounding(HookProvider):
    """Per-invocation evidence; no process-global or previous-user location state."""
    def __init__(self, query, requested_topics=None, source_location=None):
        self.lock = threading.Lock()
        self.points = set()
        self.named_points = set()
        self.query_tokens = re.findall(r"[^\W_]+", query.casefold())
        self.observations = {}
        self.requested_topics = requested_topics
        self.source_point = None
        if source_location:
            self.source_point = point(source_location.get("latitude"), source_location.get("longitude"))
            if self.source_point is None:
                raise ValueError("Invalid retained source coordinates")
            self.observations["location"] = deepcopy(source_location)
            self.points.add(self.source_point)
        explicit_points = set()
        for pattern in (PAIR, LABELED):
            for lat, lon in pattern.findall(query):
                value = point(float(lat), float(lon))
                if value is not None:
                    explicit_points.add(value)
        self.explicit_points = frozenset(explicit_points)
        self.explicit_point = next(iter(self.explicit_points)) if len(self.explicit_points) == 1 else None
        self.points.update(self.explicit_points)

    def register_hooks(self, registry, **kwargs):
        registry.add_callback(BeforeToolCallEvent, self.before)
        registry.add_callback(AfterToolCallEvent, self.after)

    def unavailable(self, topics, code):
        """Keep independently successful sources while recording a bounded lookup failure."""
        with self.lock:
            unavailable_sources(self.observations, topics, code)

    def requested_named_place(self, arguments):
        address = arguments.get("address")
        if not isinstance(address, str) or PAIR.search(address) or LABELED.search(address):
            return False
        primary = re.findall(r"[^\W_]+", address.split(",", 1)[0].casefold())
        if not primary or not any(token.isalpha() and token not in {
                "lat", "latitude", "lon", "longitude", "coordinates"} for token in primary):
            return False
        return any(self.query_tokens[index:index+len(primary)] == primary
                   for index in range(len(self.query_tokens)-len(primary)+1))

    def before(self, event):
        name = event.tool_use["name"]
        arguments = event.tool_use.get("input", {})
        if name == "ObservationBriefing" and self.requested_topics:
            missing = set(self.requested_topics) - observation_outcomes(self.observations)
            if missing:
                event.cancel_tool = (
                    "REQUESTED_OBSERVATIONS_REQUIRED: Retrieve fresh source success or failure receipts for "
                    + ", ".join(sorted(missing)) + " before completing ObservationBriefing.")
            return
        if name == GEOCODE and self.source_point:
            event.cancel_tool = "RETAINED_LOCATION_BOUND: Use the server-selected source coordinates without geocoding."
            return
        if name == GEOCODE and self.explicit_points and not self.requested_named_place(arguments):
            event.cancel_tool = (
                "EXPLICIT_LOCATION_BOUND: Use the request's exact coordinates. "
                "Geocode only another named place explicitly supplied in the original request.")
            return
        if self.requested_topics is not None:
            topic = {"fire_weather_tool": "fires", **WEATHER_TOPICS}.get(name)
            if topic and topic not in self.requested_topics:
                event.cancel_tool = "TOPIC_NOT_REQUESTED: Complete only the supervisor's selected tasks."
                return
            if name == "fire_weather_tool" and "weather" not in self.requested_topics:
                # Its default enriches fires with weather. Fire-only tasks cannot spend on that provider.
                arguments["include_weather"] = False
        topic = {"fire_weather_tool": "fires", GEOCODE: "location", **WEATHER_TOPICS}.get(name)
        if ((self.observations.get("sourceAvailability") or {}).get(topic) or {}).get("status") == "unavailable":
            event.cancel_tool = "LOOKUP_ENDED: Reuse this turn's source receipt and preserve its availability."
            return
        if name == "fire_weather_tool":
            requested = [point(arguments.get("latitude"), arguments.get("longitude"))]
        elif name == DTN_FORECAST:
            # The Gateway forecast contract accepts one point at the top level.
            requested = [point(arguments.get("latitude"), arguments.get("longitude"))]
        elif name == DTN_CURRENT:
            requested = [point(v.get("latitude"), v.get("longitude"))
                         for v in arguments.get("locations", []) if isinstance(v, dict)]
        else:
            return
        with self.lock:
            # A nearby reported fire can authorize weather at its source point, but cannot
            # replace the explicit anchor of the visitor's primary fire search.
            allowed = ({self.source_point} if self.source_point else
                       self.explicit_points | self.named_points
                       if self.explicit_points and name == "fire_weather_tool" else self.points)
            matched = bool(requested) and all(
                value is not None and any(abs(value[0]-p[0]) <= 0.000001 and abs(value[1]-p[1]) <= 0.000001
                                          for p in allowed)
                for value in requested)
        if not matched:
            if self.source_point or self.explicit_points:
                event.cancel_tool = (
                    "LOCATION_BOUND: Copy the selected request or successful fire receipt coordinates exactly. "
                    "Do not substitute a nearby city or geocode an unrequested location.")
                return
            event.cancel_tool = (
                "UNRESOLVED_COORDINATES: First finish geocode-service___geocode_address. "
                "Then copy its returned latitude/longitude exactly into this tool. "
                "Do not guess, round, or launch dependent lookups alongside geocoding. "
                "Coordinates explicitly supplied in the request and returned fire locations are also supported."
            )

    def after(self, event):
        name = event.tool_use["name"]
        result = event.result
        if name not in {GEOCODE, "fire_weather_tool", *WEATHER}:
            return
        if name == GEOCODE and (self.source_point or self.explicit_points
                               and not self.requested_named_place(event.tool_use.get("input", {}))):
            # Ignore a late geocoder receipt as well as blocking the call before execution.
            return
        # Only successful data can authorize coordinates; failed sources get separate availability receipts.
        data = tool_payload(result)
        if data is None or data.get("error"):
            text = " ".join(str(block.get("text", "")) for block in result.get("content", [])
                            if isinstance(block, dict))
            if any(marker in text for marker in (
                    "LOCATION_BOUND", "UNRESOLVED_COORDINATES", "TOPIC_NOT_REQUESTED",
                    "LOOKUP_ENDED", "REQUESTED_OBSERVATIONS_REQUIRED")):
                return  # A guard refusal is not an attempted provider lookup.
            topics = {{"fire_weather_tool": "fires", GEOCODE: "location", **WEATHER_TOPICS}[name]}
            if name == GEOCODE and not (self.source_point or self.explicit_points):
                topics.update(self.requested_topics or ())
            self.unavailable(topics, "SOURCE_UNAVAILABLE")
            return
        try:
            values = []
            if name == GEOCODE and data.get("provider") == "Amazon Location":
                for v in data.get("candidates", []):
                    score = v.get("score")
                    if isinstance(score, (int, float)) and not isinstance(score, bool) and 90 <= score <= 100:
                        values.append(point(v.get("lat"), v.get("lon")))
            elif name == "fire_weather_tool":
                values = [point(v.get("latitude"), v.get("longitude")) for v in data.get("locations", [])]
            with self.lock:
                self.points.update(v for v in values if v is not None)
                if name == GEOCODE:
                    self.named_points.update(v for v in values if v is not None)
                if name == GEOCODE and data.get("provider") == "Amazon Location":
                    for candidate in data.get("candidates", []):
                        score = candidate.get("score")
                        if (isinstance(score, (float, int)) and not isinstance(score, bool)
                                and 90 <= score <= 100
                                and point(candidate.get("lat"), candidate.get("lon")) in self.points):
                            self.observations["location"] = {
                                "name": candidate.get("matched_address") or "Resolved scenario location",
                                "latitude": candidate["lat"], "longitude": candidate["lon"]}
                            break
                if name == "fire_weather_tool":
                    existing = self.observations.get("mapData", {}).get("locations", [])
                    self.observations["mapData"] = deepcopy(data)
                    self.observations["mapData"].pop("sourceAvailability", None)
                    locations = self.observations["mapData"].setdefault("locations", [])
                    locations.extend(v for v in existing if v not in locations)
                elif name in WEATHER:
                    for location in data.get("locations", []):
                        if (not isinstance(location, dict) or location.get("error")
                                or point(location.get("latitude"), location.get("longitude")) is None):
                            continue
                        self.observations.setdefault("location", {
                            "name": location.get("name") or "Scenario weather location",
                            "latitude": location["latitude"], "longitude": location["longitude"]})
                        if location.get("forecastData"):
                            self.observations["forecastData"] = deepcopy(location["forecastData"])
                        if location.get("currentWeather"):
                            locations = self.observations.setdefault("mapData", {}).setdefault("locations", [])
                            if location not in locations:
                                locations.append(deepcopy(location))
            if name in WEATHER or name == "fire_weather_tool":
                # Full provider records above remain the final answer/Memory source receipt.
                # The specialist sees measurements it can describe, without guessing index scales.
                result["content"] = [{"text": json.dumps(provider_records_for_reasoning(data))}]
            topic = {"fire_weather_tool": "fires", GEOCODE: "location", **WEATHER_TOPICS}[name]
            if topic not in observation_topics(self.observations):
                point_codes = {location.get("errorCode") for location in data.get("locations", [])
                               if isinstance(location, dict)}
                code = ("LOCATION_UNRESOLVED" if name == GEOCODE else
                        "WEATHER_BUDGET_EXHAUSTED" if "WEATHER_BUDGET_EXHAUSTED" in point_codes else
                        "SOURCE_UNAVAILABLE" if "WEATHER_UNAVAILABLE" in point_codes else "SOURCE_INVALID")
                self.unavailable({topic}, code)
                if name == GEOCODE and not (self.source_point or self.explicit_points):
                    self.unavailable(self.requested_topics or (), "LOCATION_UNRESOLVED")
            weather_outcome = (data.get("sourceAvailability") or {}).get("weather")
            if (name == "fire_weather_tool" and isinstance(weather_outcome, dict)
                    and weather_outcome.get("status") == "unavailable"
                    and self.requested_topics and "weather" in self.requested_topics):
                self.unavailable({"weather"}, weather_outcome.get("code", "SOURCE_UNAVAILABLE"))
        except (KeyError, TypeError, ValueError, AttributeError):
            # A malformed/failed provider response cannot authorize coordinates.
            self.unavailable({{"fire_weather_tool": "fires", GEOCODE: "location", **WEATHER_TOPICS}[name]},
                             "SOURCE_INVALID")
            return
