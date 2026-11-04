"""Capture only SDK-validated final output; streaming prose is never a receipt."""
import importlib
import json
import math
import re
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from strands.hooks import AfterInvocationEvent, AfterToolCallEvent, BeforeToolCallEvent, HookProvider
from strands.models import Model

from utils.limits import MEMORY_OBSERVATION_BYTES, MEMORY_QUESTION_CHARS, MEMORY_RECENT_ITEMS

# A JSON string token, escapes included; compact JSON contains no other double quotes.
_JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
# Keys our code writes into specialist contexts that no generated schema model defines: the communication,
# guidance and broker planning context keys, and the DTN and fire adapters' extra record fields.
CONTEXT_KEYS = frozenset({
    "selected_incident", "selected_location", "planning_guidance", "source_records", "previous_observations",
    "conversation_observations", "current_observations", "verified_location", "initial_plan",
    "initial_plan_task_id", "shared_fact_ids", "hour", "precipAmount", "truncated", "truncationNote",
    "sourceAvailability", "status", "code"})


def provider_records_for_reasoning(value):
    """Project source receipts without indices lacking a supplied interpretation contract."""
    if isinstance(value, dict):
        return {key: provider_records_for_reasoning(item) for key, item in value.items() if key != "dtnSpecific"}
    if isinstance(value, list):
        return [provider_records_for_reasoning(item) for item in value]
    return value


class ToolResultModel(Model):
    """Require tool calls for public receipt workflows; keep the guarded provider and native SDK output."""
    def __init__(self, model):
        self.model = model

    def get_config(self):
        return self.model.get_config()

    def update_config(self, **kwargs):
        return self.model.update_config(**kwargs)

    async def stream(self, messages, tool_specs=None, system_prompt=None, *, tool_choice=None, **kwargs):
        async for event in self.model.stream(
                messages, tool_specs=tool_specs, system_prompt=system_prompt,
                tool_choice=tool_choice or {"any": {}}, **kwargs):
            yield event

    async def structured_output(self, *args, **kwargs):
        async for event in self.model.structured_output(*args, **kwargs):
            yield event


class InvestigationPlan(BaseModel):
    """Requested tasks; an empty selection requires clarification."""
    model_config = ConfigDict(extra="forbid")
    topics: list[Literal["fires", "weather", "forecast", "location", "guidance", "communication"]] = Field(
        max_length=6)
    source_id: str | None = Field(default=None, description="Exact retained location ID, or null for a new place.")


def observation_topics(observations):
    """Evidence presence, including a successful zero-fire result, comes from source receipts."""
    data = observations.get("mapData") or {}
    topics = set()
    if (data.get("summary") or {}).get("totalActiveFires") is not None:
        topics.add("fires")
    if any(loc.get("currentWeather") for loc in data.get("locations", [])):
        topics.add("weather")
    if observations.get("forecastData"):
        topics.add("forecast")
    if observations.get("location"):
        topics.add("location")
    return topics


SOURCE_TOPICS = frozenset({"fires", "weather", "forecast", "location"})
SOURCE_FAILURE_CODES = frozenset({
    "SOURCE_UNAVAILABLE", "SOURCE_INVALID", "TOOL_UNAVAILABLE", "LOOKUP_INTERRUPTED",
    "LOCATION_UNRESOLVED", "WEATHER_BUDGET_EXHAUSTED",
})


def source_availability(observations):
    """Only actual measurements count as available; explicit code-owned failures settle other topics."""
    receipts = {topic: {"status": "available", "code": None} for topic in observation_topics(observations)}
    for topic, receipt in (observations.get("sourceAvailability") or {}).items():
        if (topic in SOURCE_TOPICS and isinstance(receipt, dict)
                and receipt.get("status") == "unavailable" and receipt.get("code") in SOURCE_FAILURE_CODES
                and topic not in receipts):
            receipts[topic] = {"status": "unavailable", "code": receipt["code"]}
    return receipts


def observation_outcomes(observations):
    """Fresh source success or explicit fresh failure can finish a requested lookup."""
    return set(source_availability(observations))


def unavailable_sources(observations, topics, code):
    """Attach only curated failure codes and never replace independent successful measurements."""
    if code not in SOURCE_FAILURE_CODES:
        raise ValueError("Unsupported source failure code")
    successful = observation_topics(observations)
    receipts = observations.setdefault("sourceAvailability", {})
    for topic in (set(topics) & SOURCE_TOPICS)-successful:
        receipts.setdefault(topic, {"status": "unavailable", "code": code})


def investigation_locations(previous, memory):
    """Give the selector stable IDs for exact search/fire points, separate from model narration."""
    entries = []
    for index, source in enumerate([*(memory or {}).get("observations", []), previous or {}]):
        data = source.get("mapData") or {}
        search = source.get("location") or (data.get("summary") or {}).get("location")
        candidates = [(f"source-{index}-search", "search", search)]
        candidates.extend((f"source-{index}-fire-{position}", "fire",
                           {"name": loc["fire"].get("name"), "latitude": loc.get("latitude"),
                            "longitude": loc.get("longitude")})
                          for position, loc in enumerate(data.get("locations", [])) if loc.get("fire"))
        for identifier, kind, location in candidates:
            if not isinstance(location, dict):
                continue
            lat, lon = location.get("latitude"), location.get("longitude")
            if (isinstance(lat, bool) or isinstance(lon, bool) or not isinstance(lat, (int, float))
                    or not isinstance(lon, (int, float)) or not math.isfinite(lat) or not math.isfinite(lon)
                    or not -90 <= lat <= 90 or not -180 <= lon <= 180):
                continue
            entries.append({"source_id": identifier, "kind": kind, "name": location.get("name"),
                            "latitude": lat, "longitude": lon})
    searches = [item for item in entries if item["kind"] == "search"]
    if searches:
        searches[-1]["latest_search"] = True
    return entries


class InvestigationCompletionGuard(HookProvider):
    """Give the supervisor a recoverable tool error before completing without this turn's receipts."""
    def __init__(self, ready, *, allow_clarification=True):
        self.ready = ready
        self.allow_clarification = allow_clarification

    def register_hooks(self, registry, **kwargs):
        registry.add_callback(BeforeToolCallEvent, self.before)

    def before(self, event):
        if event.tool_use["name"] != "PublicAnswerResponse":
            return
        completion = (event.tool_use.get("input", {}).get("content") or {}).get("response")
        if completion == "location_required" and not self.allow_clarification:
            event.cancel_tool = (
                "SELECTED_TASKS_REQUIRED: Task selection resolved this turn's scope. Run the selected "
                "specialists and use their current receipts; location_required is only for an empty task scope.")
        elif completion == "source_results_ready" and not self.ready():
            event.cancel_tool = (
                "SOURCE_RESULTS_REQUIRED: Run the selected specialists for this turn before completing. "
                "Previous results do not satisfy a requested fresh lookup.")


class ToolExecutionCapture(HookProvider):
    """Preserve the SDK execution status that the AG-UI adapter unwraps from tool results."""
    def __init__(self, max_calls=None):
        self.statuses = {}
        self.max_calls, self.calls = max_calls, 0

    def register_hooks(self, registry, **kwargs):
        registry.add_callback(AfterToolCallEvent, self.after)
        registry.add_callback(BeforeToolCallEvent, self.before)

    def before(self, event):
        self.calls += 1
        if self.max_calls is not None and self.calls > self.max_calls:
            raise RuntimeError("Investigation tool budget exceeded")

    def after(self, event):
        status = event.result.get("status")
        if status in {"success", "error"}:
            self.statuses[event.tool_use["toolUseId"]] = status

    def envelope(self, call_id, content):
        status = self.statuses.get(call_id)
        if status is None:
            return content
        try:
            result = json.loads(content)
        except (ValueError, TypeError):
            result = content
        return json.dumps({"status": status, "result": result})


def code_owned_keys() -> frozenset[str]:
    """Return the JSON keys our code owns: every generated schema model's field names plus CONTEXT_KEYS.

    Any other key in a specialist context came from a provider, so guarded_context guards it like a value.
    The models are generated into the Runtime image; a checkout without them owns only CONTEXT_KEYS.
    """
    try:
        models = importlib.import_module("schemas.generated_models")
    except ImportError:
        return CONTEXT_KEYS
    return CONTEXT_KEYS | {field.alias or name for model in vars(models).values()
                           if isinstance(model, type) and issubclass(model, BaseModel)
                           and model.__module__ == models.__name__
                           for name, field in model.model_fields.items()}


def guarded_context(question: str, context: dict | None) -> list[dict]:
    """Build the format_context prompt as content blocks that guard the question and all provider text.

    Once a request has a guardContent block, the Bedrock guardrail assesses only those blocks (AWS
    input tags). The question, every JSON string value (provider, conversation and guidance text) and
    every key our code does not own (code_owned_keys) are guarded, one block each, as written: non-ASCII
    text is not escaped. Our framing, code-owned field names, numbers and punctuation stay plain text,
    so the prompt-attack filter does not classify them. Joined, the blocks are the format_context text.
    """
    def guard(text):
        return {"guardContent": {"text": {"text": text}}}
    if not context:
        return [guard(question)]
    owned = code_owned_keys()
    data = json.dumps(context, separators=(",", ":"), default=str, ensure_ascii=False)
    text = f"[UI_CONTEXT]\n{data}\n[END_CONTEXT]\n\nUser Query: "
    blocks, end = [], 0
    for token in _JSON_STRING.finditer(text):
        # A key is followed by ":". It stays plain only as an exact owned name, which JSON never escapes.
        if not (text.startswith(":", token.end()) and token.group()[1:-1] in owned):
            blocks += [{"text": text[end:token.start()]}, guard(token.group())]
            end = token.end()
    return [*blocks, {"text": text[end:]}, guard(question)]


def format_context(question: str, context: dict | None) -> str:
    """Preserve context values without spending provider limits on JSON indentation."""
    return "".join(block["text"] if "text" in block else block["guardContent"]["text"]["text"]
                   for block in guarded_context(question, context))


def investigation_memory_context(messages):
    """Bound reference questions and exact detection receipts from owner-scoped Memory history."""
    calls, questions, observations = {}, [], []
    for message in messages:
        for block in message.get("content", []):
            if message.get("role") == "user" and isinstance(block.get("text"), str):
                # Older turns contain a UI envelope; its default map is not user intent.
                questions.append(block["text"].rsplit("User Query: ", 1)[-1][:MEMORY_QUESTION_CHARS])
            use = block.get("toolUse") or {}
            if use.get("name") == "detection_agent_tool":
                calls[use["toolUseId"]] = True
            result = block.get("toolResult") or {}
            if result.get("toolUseId") not in calls or result.get("status") != "success":
                continue
            try:
                receipt = json.loads("".join(part.get("text", "") for part in result.get("content", [])))
                if receipt.get("status") == "success" and "result" in receipt:
                    receipt = receipt["result"]
                if receipt.get("error") or receipt.get("status") == "error":
                    continue
                source = {key: deepcopy(receipt[key]) for key in ("location", "mapData", "forecastData")
                          if receipt.get(key) is not None}
                if source and len(json.dumps(source).encode()) <= MEMORY_OBSERVATION_BYTES:
                    observations.append(source)
            except (TypeError, AttributeError, ValueError):
                continue  # Malformed history is not source evidence.
    retained = []
    for source in reversed(observations[-MEMORY_RECENT_ITEMS:]):
        if len(json.dumps([source, *retained]).encode()) <= MEMORY_OBSERVATION_BYTES:
            retained.insert(0, source)
    return {"questions": questions[-MEMORY_RECENT_ITEMS:], "observations": retained}


def investigation_route_context(context: dict | None) -> dict | None:
    """The supervisor needs source locations to route; guidance receives complete receipts directly."""
    if not context:
        return context
    result = deepcopy(context)
    previous = context.get("previous_observations") or {}
    route = {key: deepcopy(previous[key]) for key in ("location",) if previous.get(key)}
    data = previous.get("mapData") or {}
    locations = []
    for loc in data.get("locations", []):
        entry = {key: loc[key] for key in ("latitude", "longitude")}
        if loc.get("fire"):
            entry["fire"] = {"name": loc["fire"].get("name")}
        if entry not in locations:
            locations.append(entry)
    if locations:
        route["mapData"] = {"locations": locations}
    result["previous_observations"] = route
    return result


def observation_text(observations: dict) -> str:
    """Render provider facts for detection-only answers, without model-authored measurements."""
    def value(item):
        if not isinstance(item, dict):
            return "unavailable"
        number = item.get("value")
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            return "unavailable"
        unit = item.get("unit") or ""
        unit = {"degF": "°F", "F": "°F", "percent": "%", "degrees": "°"}.get(unit, unit)
        return f"{number:g} {unit}".strip()

    data = observations.get("mapData") or {}
    summary = data.get("summary") or {}
    lines = []
    if summary.get("totalActiveFires") is not None:
        lines.append(f"Reported fires from ArcGIS/NIFC near {summary.get('searchArea') or 'the search point'}:")
        fires = [loc for loc in data.get("locations", []) if loc.get("fire")]
        for loc in sorted(fires, key=lambda loc: (loc["fire"].get("distanceMiles") is None,
                                                  loc["fire"].get("distanceMiles") or 0)):
            fire = loc["fire"]
            details = [(f'{fire.get("name") or "Unnamed reported fire"} — '
                        f'{loc["latitude"]}, {loc["longitude"]}')]
            for field, unit in (("distanceMiles", "miles from search point"),
                                ("acresBurned", "acres"), ("percentContained", "% contained")):
                if fire.get(field) is not None:
                    details.append(value({"value": fire[field], "unit": unit}))
            if fire.get("lastUpdated"):
                details.append(f'updated {fire["lastUpdated"]}')
            lines.append(" · ".join(details))
        if not fires:
            lines.append("No matching fire records returned. This does not establish that no fire exists.")
        if summary.get("analysisTimestamp"):
            lines.append(f'Retrieved {summary["analysisTimestamp"]}.')
        lines.append("The source may contain delayed or incomplete reports; these locations do not confirm a fire at the search point.")
        if data.get("truncated"):
            lines.append("The source capped this list; additional reported fires may exist.")
    for loc in data.get("locations", []):
        weather = loc.get("currentWeather")
        if not weather:
            continue
        wind = weather.get("wind") or {}
        name = (loc.get("fire") or {}).get("name") or loc.get("name") or "Observation point"
        lines.append(f'{name} · DTN · {loc["latitude"]:.4f}, {loc["longitude"]:.4f}\n'
                     f'Temperature: {value(weather.get("temperature"))} · '
                     f'Humidity: {value(weather.get("relativeHumidity"))}\n'
                     f'Wind: {value(wind.get("speed"))} · Gusts: {value(wind.get("gust"))}')
        if weather.get("timestamp"):
            lines.append(f'Observed {weather["timestamp"]}.')
    forecast = observations.get("forecastData")
    if forecast:
        lines.append(f'{forecast.get("provider") or "DTN"} forecast · UTC source-day summaries:')
        for day in forecast.get("days", []):
            daily = day.get("summary") or {}
            lines.append(f'{day["date"]}: {value(daily.get("lowTemp"))}–{value(daily.get("highTemp"))}; '
                         f'wind up to {value(daily.get("maxWindSpeed"))}; '
                         f'average humidity {value({"value": daily.get("avgHumidity"), "unit": "percent"})}; '
                         f'rain probability {value({"value": daily.get("precipProb"), "unit": "percent"})}.')
        if not forecast.get("days"):
            lines.append("Forecast periods are available in the weather panel.")
    labels = {"fires": "Reported fire source", "weather": "Current weather",
              "forecast": "Weather forecast", "location": "Location resolution"}
    for topic, receipt in sorted(source_availability(observations).items()):
        if receipt["status"] == "unavailable":
            lines.append(f'{labels[topic]} unavailable ({receipt["code"]}). No values are inferred.')
    if lines:
        return "\n\n".join(lines)
    if observations.get("location"):
        return "Source observations are unavailable for this location. Please retry the requested lookup."
    return "No sourced observations were returned. Please specify the city, address or fire location to investigate."


def bind_guidance_citations(payload: dict, citations: list, *, guidance_failed=False,
                           observations=None, assessments=None, briefings=None, communications=None) -> dict:
    """Final public provenance is copied from retrieval, never rewritten by a model."""
    result = deepcopy(payload)
    if observations is not None:
        for field in ("mapData", "forecastData", "location"):
            result["content"][field] = deepcopy(observations.get(field))
        result["content"]["sourceAvailability"] = source_availability(observations) or None
    result["content"]["knowledgeBaseCitations"] = deepcopy(citations) or None
    facts = observation_text(observations) if observations and (
        observations.get("mapData") or observations.get("forecastData")) else None
    briefing = "\n\n".join(briefings or [])
    if assessments:
        result["content"]["response"] = "\n\n".join(text for text in (briefing, facts, assessments[-1]) if text)
    elif observations and payload["content"]["response"] == "source_results_ready":
        result["content"]["response"] = "\n\n".join(text for text in (briefing, observation_text(observations)) if text)
    elif communications:
        result["content"]["response"] = ""
    elif payload["content"]["response"] == "location_required":
        result["content"]["response"] = "Which city, address or fire location should I investigate?"
    elif payload["content"]["response"] == "source_results_ready" and not guidance_failed:
        raise ValueError("Public completion has no source observations or guidance receipt")
    if guidance_failed:
        result["content"]["knowledgeBaseCitations"] = None
        result["content"]["response"] = (
            "Published guidance could not be synthesized into a validated planning response. "
            "The map and forecast contain separately retrieved observations where available. "
            "Retry the guidance step or consult local authorities and incident command."
        )
    if communications:
        receipt = communications[-1]
        result["content"]["audioData"] = deepcopy(receipt.get("audioData"))
        result["content"]["response"] = "\n\n".join(
            text for text in (result["content"]["response"], receipt["response_text"]) if text)
    return result


class AnswerContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    response: str = Field(min_length=1, max_length=32000)


class AnswerResponse(BaseModel):
    """Source observations, citations and artifacts are attached outside model generation."""
    model_config = ConfigDict(extra="forbid")
    type: Literal["structuredResponse"] = "structuredResponse"
    content: AnswerContent


class PublicAnswerContent(AnswerContent):
    response: Literal["source_results_ready", "guidance_unavailable", "location_required"] = Field(
        description="Completion status only. The Runtime attaches verified observations and selected guidance.")


class PublicAnswerResponse(AnswerResponse):
    """Public completion receipt; source-backed display content is assembled outside generation."""
    content: PublicAnswerContent


class StructuredResponseCapture(HookProvider):
    def __init__(self):
        self.value = None

    def register_hooks(self, registry, **kwargs):
        registry.add_callback(AfterInvocationEvent, self.after)

    def after(self, event):
        result = getattr(event.result, "structured_output", None)
        if isinstance(result, BaseModel):
            self.value = result.model_dump(mode="json")
