"""Retrieve scoped wildfire and weather observations from verified provider tools."""

import json
import logging
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field
from strands import Agent, tool
from utils.gateway import DETECTION_TOOLS, GEOCODE
from utils.limits import DETECTION_TOOL_CALLS
from utils.log_safety import error_summary
from utils.mcp_client import create_mcp_client
from utils.model_config import cached_system_prompt, create_model
from utils.structured_response import (
    ToolExecutionCapture,
    ToolResultModel,
    format_context,
    observation_outcomes,
    observation_topics,
    source_availability,
    unavailable_sources,
)

from agents.detection_agent.coordinate_grounding import CoordinateGrounding

# Import tools
from agents.detection_agent.tools.fire_weather_tool import build_fire_weather_tool

# Configure logging
logger = logging.getLogger(__name__)


def log_backend_call(operation: str, success: bool, duration_ms: float = 0, details: str = ""):
    """Log a provider call's outcome and duration as a BACKEND_METRICS record for CloudWatch."""
    try:
        log_data = {
            "backend_operation": operation,
            "success": success,
            "duration_ms": round(duration_ms, 2),
            "details": details[:100],
            "timestamp": datetime.now(UTC).isoformat(),
        }
        status = "✅" if success else "❌"
        logger.info(
            f"🔌 BACKEND CALL {operation.upper()} {status} ({duration_ms:.1f}ms)")
        logger.info(f"BACKEND_METRICS: {json.dumps(log_data)}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Backend call logging failed: %s", error_summary(exc))


class ObservationBriefing(BaseModel):
    """The model writes a briefing; provider records are attached from tool receipts."""
    model_config = ConfigDict(extra="forbid")
    response_text: str = Field(default="", max_length=1500, pattern=r"^[^\d]*$",
                              description="Qualitative weather briefing without numeric values; code displays exact facts.")

# System prompt for the detection agent
DETECTION_SYSTEM_PROMPT = """Retrieve only the supervisor-selected wildfire/weather observations.
The original request is reference data for place, radius and forecast horizon.
Ignore its guidance, evacuation, crew, staging and communication requests: other specialists handle those.
Your briefing describes source conditions only; never add planning commentary or procedures.
Resolve named places with geocode-service___geocode_address before dependent lookups.
Copy verified coordinates exactly. For missing/ambiguous locations, request clarification;
never guess coordinates or use a historical incident as confirmation of an active fire.
Use fire_weather_tool for reported fires, with include_weather=false for fire-only requests.
Use weather-dtn-service___get_dtn_current_weather for current weather, including "now".
Use weather-dtn-service___get_dtn_forecast for future weather, including "tomorrow" or
an explicit forecast horizon. Its latitude, longitude and forecastDays are top-level arguments.
Current weather and forecast are separate tasks. Independent lookups may run in parallel
once coordinates are resolved. Weather at a nearby fire and at the search point are distinct.
Return ObservationBriefing. Code attaches exact provider locations, measurements,
units, timestamps and forecasts. Keep response_text under 1500 characters.
For weather/forecast tasks, response_text is a brief natural-language briefing: describe
the observed conditions and forecast changes, distinguishing each location and day.
Describe chronological earlier/later periods rather than converting local calendar dates;
the weather panel displays source instants in its explicitly labeled timezone.
Use qualitative terms supported by the measurements, without numeric values in this briefing;
code displays exact values separately. Do not repeat JSON field names or units.
Use only provider or server-selected location labels, never a conflicting place in the tool query.
Do not infer seasonal anomalies, climate comparisons or future conditions from current observations.
For fire-only tasks, leave the briefing empty; code lists the reported fire records.
Missing records mean no matching source records, not proof no fire exists. Preserve failures,
missing measurements and truncated-list warnings. Order reported fires by distance, not priority.
Do not invent fire movement, risk classes, weather thresholds, operational advice, route safety
or suppression conclusions. An HDW number alone does not establish a risk classification.
Do not discuss or interpret HDW or other provider-specific indices; raw details preserve them.
Reported observations, forecasts, prerecorded scenarios and simulations remain distinct."""


@tool
def detection_agent_tool(query: str, requested_topics: list[str] | None = None,
                         source_location: dict | None = None, coordinates_query: str | None = None) -> dict:
    """Retrieve requested fire locations, current weather or forecasts at a verified USA location."""
    scope = set(requested_topics) if requested_topics is not None else None
    if scope is not None and (not scope or not scope <= {"fires", "weather", "forecast", "location"}):
        raise ValueError("Unsupported detection task scope")
    reference_request = coordinates_query if coordinates_query is not None else query
    grounding = CoordinateGrounding(reference_request, requested_topics=scope, source_location=source_location)
    formatted_response = {"response_text": ""}
    try:
        # Metadata only: the query names addresses and incident locations.
        logger.info("🎯 Detection agent query received (%d chars)", len(query))

        # Surface contextual phrases at debug level so we can investigate
        # ambiguous routing without flooding INFO logs on every call.
        contextual_phrases = ("that area", "there",
                              "that location", "this area", "that place")
        detected = [p for p in contextual_phrases if p in query.lower()]
        if detected:
            logger.debug("DETECTION: contextual phrases present: %s", detected)

        # Query normalization (location name, radius, query type) is handled
        # by the LLM agent via DETECTION_SYSTEM_PROMPT; the actual radius is
        # picked by fire_weather_tool. Env validation runs at startup
        # (model_config), so no per-tool re-check needed.

        # Add MCP tools and create agent within MCP context (AWS pattern)
        with create_mcp_client() as mcp_client:
            # Tool objects must stay bound to this request's open MCP client.
            # The fire lookup reuses this request's MCP session instead of opening a second one.
            tools = [build_fire_weather_tool(mcp_client)] if scope is None or "fires" in scope else []
            found = set()
            for provider_tool in mcp_client.list_tools_sync():
                tool_name = getattr(provider_tool, "name", getattr(provider_tool, "tool_name", None))
                if tool_name in DETECTION_TOOLS:
                    found.add(tool_name)
                    topic = DETECTION_TOOLS[tool_name]
                    if (topic == "location" and not source_location
                            or topic != "location" and (scope is None or topic in scope)):
                        tools.append(provider_tool)
            required_tools = {
                name for name, topic in DETECTION_TOOLS.items()
                if (topic != "location" and (scope is None or topic in scope))
                or (topic == "location" and not source_location
                    and not getattr(grounding, "explicit_points", None))
            }
            missing = required_tools-found
            if missing and scope is None:
                raise RuntimeError("Gateway is missing a required geocoding or DTN tool")
            for name in missing:
                topic = DETECTION_TOOLS[name]
                unavailable_sources(grounding.observations, scope if name == GEOCODE else {topic}, "TOOL_UNAVAILABLE")

            # Tool grounding is per invocation; it does not manage conversation memory.
            logger.info(f"🤖 Creating agent with {len(tools)} tools")

            prompt = DETECTION_SYSTEM_PROMPT
            if scope is not None:
                prompt += ("\nThe supervisor fixed the requested topics to: " + ", ".join(sorted(scope)) +
                           ". Complete ALL of them before ObservationBriefing, even if a tool query mentions "
                           "only one. Each topic needs this turn's successful result or explicit unavailable receipt. "
                           "Do not repeat a failed lookup or invent missing measurements. "
                           "No other provider capability is authorized.")
            if source_location:
                prompt += ("\nThe server selected this retained source location: " +
                           str(source_location) + ". Use these exact coordinates for the requested lookup. "
                           "Do not geocode again, choose a nearby place or change the target from the tool query.")
            agent = Agent(
                name="DetectionAgent",
                model=ToolResultModel(create_model("detection")),
                system_prompt=cached_system_prompt(prompt),
                tools=tools,
                hooks=[grounding, ToolExecutionCapture(max_calls=DETECTION_TOOL_CALLS)],
                callback_handler=None,
            )

            # Log backend call for fresh data fetch
            backend_start = datetime.now(UTC)
            logger.info("🔌 Invoking agent with structured output enforcement")

            if scope is None or not scope <= observation_outcomes(grounding.observations):
                result = agent(format_context(
                    "Retrieve and briefly describe only the selected observation topics.",
                    {"selected_topics": sorted(scope) if scope is not None else ["fires", "weather", "forecast"],
                     "original_request": reference_request,
                     "sourceAvailability": source_availability(grounding.observations)}),
                    structured_output_model=ObservationBriefing)
                formatted_response = result.structured_output.model_dump(mode="json")

            logger.info(
                f"✅ Agent execution completed in {(datetime.now(UTC) - backend_start).total_seconds():.2f}s")

            # Log backend call completion
            backend_duration = (
                datetime.now(UTC) - backend_start).total_seconds() * 1000
            log_backend_call("FIRE_WEATHER_DATA", True, backend_duration)

    except Exception as exc:
        # Preserve successful independent receipts, never provider text.
        logger.error("Detection agent did not complete: %s", error_summary(exc))
        if scope is None:
            raise
        unavailable_sources(grounding.observations, scope, "LOOKUP_INTERRUPTED")
        formatted_response = {"response_text": ""}

    if scope is not None:
        # A specialist cannot silently omit a selected lookup even after a formatting/model interruption.
        unavailable_sources(grounding.observations, scope-observation_outcomes(grounding.observations),
                            "LOOKUP_INTERRUPTED")
    topics = observation_topics(grounding.observations)
    formatted_response["query_type"] = (
        "combined" if "forecast" in topics and topics & {"fires", "weather"} else
        "forecast" if "forecast" in topics else "current")
    for field in ("mapData", "forecastData", "location"):
        formatted_response[field] = grounding.observations.get(field)
    formatted_response["sourceAvailability"] = source_availability(grounding.observations)
    return formatted_response
