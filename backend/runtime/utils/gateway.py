"""AgentCore Gateway tool names the Runtime calls, and one reader for a Gateway tool call's JSON result.

The inline tool schemas in backend/deployment/templates/nested/4-agentcore-resources.yaml define these
names; Gateway prefixes each tool with its target name and three underscores.
"""
import json

GEOCODE = "geocode-service___geocode_address"
DTN_CURRENT = "weather-dtn-service___get_dtn_current_weather"
DTN_FORECAST = "weather-dtn-service___get_dtn_forecast"
WILDFIRES = "wildfire-service___queryWildfires"
# The observation topic each Gateway weather tool answers.
WEATHER_TOPICS = {DTN_CURRENT: "weather", DTN_FORECAST: "forecast"}
# The provider tools the detection agent registers, by the observation topic each one answers.
DETECTION_TOOLS = {GEOCODE: "location", **WEATHER_TOPICS}


def tool_payload(result) -> dict | None:
    """Return the JSON object a Gateway tool call returned, or None for a failed call or a malformed result.

    An object in the Lambda proxy shape ({"statusCode", "body"}) is unwrapped; a status other than 200 is a
    failure. An {"error": ...} object is returned as it is, so each caller decides what a provider error means.
    """
    if not isinstance(result, dict) or result.get("status") == "error" or result.get("isError"):
        return None
    texts = [block["text"] for block in result.get("content") or []
             if isinstance(block, dict) and isinstance(block.get("text"), str)]
    try:
        data = json.loads("".join(texts))
        if isinstance(data, dict) and "statusCode" in data:
            if data["statusCode"] != 200:
                return None
            data = data.get("body")
            data = json.loads(data) if isinstance(data, str) else data
    except (TypeError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None
