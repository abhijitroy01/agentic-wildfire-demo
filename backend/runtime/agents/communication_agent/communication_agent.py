"""Incident communication specialist; reasoning is isolated from translation, storage and ownership."""
import logging
import os
import threading
from copy import deepcopy

from pydantic import BaseModel, ConfigDict, Field
from strands import Agent, tool
from utils.limits import COMMUNICATION_TURNS
from utils.model_config import cached_system_prompt, create_model
from utils.structured_response import ToolResultModel, guarded_context, provider_records_for_reasoning

from agents.communication_agent.tools.voice_message_tool import generate_voice_messages

logger = logging.getLogger(__name__)
COMMUNICATION_PROMPT = """Draft one plain-English spoken wildfire situation update for human review.
Code fulfills requested languages using Translate and Polly. Never write translations,
language headings, Markdown, emoji or production notes. Return a single paragraph.
Use only the selected source incident and supplied weather/guidance receipts. They are reference
records with timestamps, not necessarily live conditions. Never substitute another incident.
Weather facts must come from this incident's source_records, never the request or planning prose.
Describe the incident as reported by the source at its timestamp, not confirmed active now.
Never claim authorities are aware, responders were dispatched or residents were notified
unless a supplied source explicitly establishes that fact.
State what is known and what is missing. Keep it under one hundred fifty words. Do not invent facts,
thresholds, facilities, routes, phone numbers, schedules, evacuation orders or safety clearances.
Do not declare fire suppression or predict fire spread. No RSS lookup or independent memory.
Use natural plain English. Return CommunicationDraft immediately; code translates and records it.
This is a demonstration draft, not an official alert and nothing is sent to the public."""


class CommunicationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=1000, pattern=r"^[^\r\n]*$",
                      description="One plain-English spoken paragraph. Code translates it; no Markdown or language headings.")


def build_communication_tool(*, owner, context, receipts, prerequisite=None, question=None):
    """Bind source context, verified identity and audio storage to one idempotent invocation."""
    lock = threading.Lock()

    @tool(name="communication_agent_tool")
    def communicate(query: str) -> dict:
        """Prepare reviewable multilingual voice drafts for the selected incident; no alert is sent."""
        if prerequisite and not prerequisite():
            raise RuntimeError("OBSERVATIONS_REQUIRED: Complete requested observations and guidance before communication.")
        with lock:
            if receipts:
                return deepcopy(receipts[-1])
            if owner is None:
                raise ValueError("Verified conversation ownership is required for recordings")
            logger.info("Communication request received (%d chars)", len(query))
            sources = [context.get("previous_observations") or {},
                       *(context.get("conversation_observations") or []),
                       context.get("current_observations") or {}]
            target = context.get("selected_location") or {}
            incidents = {}
            # Without a selected retained ID, only this turn's new fire lookup can bind a draft.
            eligible = sources if target else [context.get("current_observations") or {}]
            for source in eligible:
                for location in (source.get("mapData") or {}).get("locations", []):
                    if location.get("fire") and (not target or all(
                            location.get(key) == target.get(key) for key in ("latitude", "longitude"))):
                        incidents[(location["latitude"], location["longitude"])] = deepcopy(location)
            if len(incidents) != 1:
                receipt = {"query_type": "communication", "response_text":
                           "Which reported fire should I use for the communication draft? "
                           "Locate or select one incident first.", "audioData": None}
                receipts.append(receipt)
                return deepcopy(receipt)
            selected = next(iter(incidents.values()))
            # The draft agent sees one selected incident, rather than every fire in the conversation.
            records = []
            for source in sources:
                matching = [deepcopy(location) for location in (source.get("mapData") or {}).get("locations", [])
                            if all(location.get(key) == selected[key] for key in ("latitude", "longitude"))]
                if matching:
                    record = {"mapData": {"locations": matching}}
                    if all((source.get("location") or {}).get(key) == selected[key]
                           for key in ("latitude", "longitude")) and source.get("forecastData"):
                        record["forecastData"] = deepcopy(source["forecastData"])
                    records.append(record)
            source_context = {"selected_incident": selected, "selected_location": target,
                              "planning_guidance": context.get("planning_guidance") or [],
                              "source_records": records}
            agent = Agent(name="CommunicationAgent", model=ToolResultModel(create_model("communication")),
                          system_prompt=cached_system_prompt(COMMUNICATION_PROMPT), tools=[],
                          callback_handler=None)
            result = agent(guarded_context(question if question is not None else query,
                                           provider_records_for_reasoning(source_context)),
                           structured_output_model=CommunicationDraft,
                           limits={"turns": COMMUNICATION_TURNS})
            if result.structured_output is None:
                raise ValueError("Communication draft has no validated completion")
            draft = CommunicationDraft.model_validate(result.structured_output.model_dump()).text
            text = "Demonstration draft for review. This is not an official alert. " + draft
            audio = generate_voice_messages(text, owner=owner, bucket_name=os.environ["VOICE_MESSAGE_S3_BUCKET"])
            ready = audio["summary"]["successful"]
            receipt = {"query_type": "communication", "response_text": (
                f"Communication draft for review — {ready} of {audio['summary']['totalLanguages']} recordings ready."
                " No alert has been sent.\n\n" + draft), "audioData": audio}
            receipts.append(deepcopy(receipt))
            return receipt

    return communicate
