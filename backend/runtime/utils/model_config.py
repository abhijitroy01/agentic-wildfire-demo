"""
Centralized model configuration for all agents.

settings() reads and validates the model settings once: each agent type's Bedrock model ID, the temperature
and the published Bedrock guardrail (DEPLOYED_GuardrailId, DEPLOYED_GuardrailVersion) that every agent model
uses. The Runtime calls it at start-up (wfm_agui_entrypoint's lifespan), so a missing or invalid setting stops
the new version, naming the setting, before it serves a turn. MAX_TOKENS holds every agent's output budget.

NOTE: utils/__init__.py imports utils.env_loader first, so a local .env is loaded
before settings() reads the environment.
"""

import functools
import logging
import math
import os
import re
from dataclasses import dataclass

from strands.models import BedrockModel

from utils.aws_clients import deployment_region

logger = logging.getLogger(__name__)

# The variable that names each agent type's Bedrock model. Every one is required; none falls back to another.
MODEL_ID_SETTINGS = {
    "supervisor": "SUPERVISOR_MODEL_ID",
    "detection": "DETECTION_MODEL_ID",
    "response": "RESPONSE_MODEL_ID",
    "communication": "COMM_MODEL_ID",
    "simulation": "SIMULATION_MODEL_ID",
    "report": "REPORT_MODEL_ID",
}

# Output budget (max_tokens) of every agent model; no call site sets its own. Protocol bounds sized to each
# answer, not deployment settings (D38). Task selection runs on the supervisor's model with a smaller budget.
# "report" serves the firetruck report agent.
MAX_TOKENS = {
    "task_selection": 1000,
    "supervisor": 1500,
    "detection": 2000,
    "response": 3500,
    "communication": 3000,
    "simulation": 8000,
    "report": 3000,
}


@dataclass(frozen=True)
class ModelSettings:
    """The validated model settings: each agent type's model ID, the temperature and the guardrail."""
    model_ids: dict[str, str]
    temperature: float
    guardrail: dict | None  # None only for a local run without DEPLOYED_GuardrailId


def _published_guardrail():
    """Read the Bedrock guardrail every agent model uses (DEPLOYED_GuardrailId, DEPLOYED_GuardrailVersion).

    The deployed Runtime (AGENTCORE_RUNTIME=true) requires both values. The
    version must be a published version number: DRAFT is mutable, so its
    filters could change without a deployment. Only a local run without
    DEPLOYED_GuardrailId runs unguarded, with a warning.
    """
    guardrail_id = os.getenv("DEPLOYED_GuardrailId", "").strip()
    version = os.getenv("DEPLOYED_GuardrailVersion", "").strip()
    if not guardrail_id:
        if os.getenv("AGENTCORE_RUNTIME", "").lower() == "true":
            raise RuntimeError(
                "DEPLOYED_GuardrailId is not set; the deployed Runtime applies the Bedrock guardrail "
                "to every agent model.")
        logger.warning("DEPLOYED_GuardrailId is not set; this local run has no Bedrock guardrail")
        return None
    if not re.fullmatch(r"[1-9][0-9]*", version):
        raise RuntimeError(
            f"DEPLOYED_GuardrailVersion must be a published guardrail version number such as 1; "
            f"got {version!r}. DRAFT is mutable and is not accepted.")
    return {
        "guardrail_id": guardrail_id,
        "guardrail_version": version,
        "guardrail_trace": "enabled",  # guardrail assessments appear in model traces
        "guardrail_stream_processing_mode": "async",  # keeps streamed responses responsive
    }


@functools.cache
def settings() -> ModelSettings:
    """Read and validate the model settings once; fail naming every setting that is missing or invalid."""
    problems = [f"{name} is not set" for name in MODEL_ID_SETTINGS.values() if not os.getenv(name, "").strip()]
    text = os.getenv("MODEL_TEMPERATURE", "").strip()
    try:
        temperature = float(text)
    except ValueError:
        temperature = math.nan
    if not 0 <= temperature <= 1:
        problems.append(f"MODEL_TEMPERATURE must be a number from 0 to 1, not {text!r}" if text
                        else "MODEL_TEMPERATURE is not set")
    try:
        guardrail = _published_guardrail()
    except RuntimeError as exc:
        problems.append(str(exc))
    if problems:
        raise RuntimeError(
            "Invalid model settings:\n" + "\n".join(f"- {problem}" for problem in problems) +
            "\n\nThe AgentCore Runtime template (4-agentcore-resources.yaml) sets them; "
            "for a local run, set them in backend/.env.")
    return ModelSettings({agent: os.environ[name].strip() for agent, name in MODEL_ID_SETTINGS.items()},
                         temperature, guardrail)


def create_model(agent_type, budget=None):
    """
    Create a Bedrock model for an agent type, with the published guardrail on every agent.

    Args:
        agent_type: The type of agent, a MODEL_ID_SETTINGS key - REQUIRED
        budget: The MAX_TOKENS entry when it is not the agent type's own (task selection)

    Returns:
        The configured BedrockModel instance, bound to the guardrail when one is configured
    """
    budget = budget or agent_type
    if agent_type not in MODEL_ID_SETTINGS or budget not in MAX_TOKENS:
        raise ValueError(f"Unknown agent type {agent_type!r} or output budget {budget!r}")
    config = settings()
    model_config = {
        "model_id": config.model_ids[agent_type],
        "temperature": config.temperature,
        "max_tokens": MAX_TOKENS[budget],
        # Explicit: BedrockModel would otherwise fall back to the SDK's own default Region.
        "region_name": deployment_region(),
    }

    # Every agent type gets the guardrail: the simulation and report agents run as top-level
    # agents with no supervisor in front of them.
    if config.guardrail:
        model_config.update(config.guardrail)
        if agent_type == "supervisor":
            # Prior assistant routing narration must not be classified as a new prompt attack.
            # Strands guards the latest user/context message across tool cycles; output policy remains active.
            model_config["guardrail_latest_message"] = True

    if agent_type == "simulation":
        from botocore.config import Config
        model_config["boto_client_config"] = Config(read_timeout=60, connect_timeout=10, retries={"max_attempts": 0})
    return BedrockModel(**model_config)


def cached_system_prompt(text: str) -> list[dict]:
    """Wrap a system-prompt string in the Bedrock cachePoint envelope.

    Centralises the prompt-caching shape so every agent uses the same
    structure. Without this, four separate agents each re-construct
    ``[{"text": ...}, {"cachePoint": {"type": "default"}}]`` inline,
    which has already drifted once (different log lines, different
    spacing) and could drift again if Bedrock changes the cachePoint
    contract.

    Returns the list shape Strands expects; pass it directly to
    ``Agent(system_prompt=...)``.
    """
    return [{"text": text}, {"cachePoint": {"type": "default"}}]
