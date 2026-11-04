"""Shared helpers for the Runtime and Gateway release checks in this folder.

The Runtime image runs these checks with the standard library's unittest (python -m unittest
discover), because it has no pytest; scripts/check_quality.py runs this folder with pytest in its
own process. Offline: no model or AWS calls.
"""
import base64
import hashlib
import hmac
import importlib.util
import json
import sys
import time
import types
import uuid
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
from strands.models import Model

ROOT = Path(__file__).resolve().parents[3]


# The backend/runtime/utils helpers that a Runtime module imports at load time. Checks load modules by path,
# so load() supplies these from their files (the image's WORKDIR /app is not on sys.path here).
RUNTIME_IMPORTS = {
    "communication_agent.py": ("limits",),
    "coordinate_grounding.py": ("structured_response", "gateway"),
    "detection_agent.py": ("limits",),
    "mcp_client.py": ("aws_clients", "log_safety"),
    "knowledge_base_tool.py": ("log_safety",),
    "model_config.py": ("aws_clients",),
    "response_agent.py": ("limits",),
    "simulation_agent.py": ("limits",),
    "simulation_context.py": ("aws_clients",),
    "structured_response.py": ("limits",),
    "supervisor_agent.py": ("limits",),
}


def load(name, path):
    """Import a module from a repository file path under the given module name."""
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    dependencies = {f"utils.{helper}": load(f"checked_{helper}", f"backend/runtime/utils/{helper}.py")
                    for helper in RUNTIME_IMPORTS.get(Path(path).name, ())}
    # Dataclasses resolve postponed annotations through sys.modules, so the module is registered while it runs.
    with patch.dict(sys.modules, {**dependencies, name: module}):
        spec.loader.exec_module(module)
    return module


def gateway_tool_names():
    """Return every Gateway tool name (target name, three underscores, tool) the AgentCore template defines."""
    template = load("checked_cloudformation", "deployment/validate_infra_baseline.py").load_template(
        (ROOT / "backend/deployment/templates/nested/4-agentcore-resources.yaml").read_text())
    return {f"{resource['Properties']['Name']}___{item['Name']}" for resource in template["Resources"].values()
            if resource["Type"] == "AWS::BedrockAgentCore::GatewayTarget"
            for item in resource["Properties"]["TargetConfiguration"]["Mcp"]["Lambda"]["ToolSchema"]["InlinePayload"]}


def gateway_target_tools(logical_id):
    """Return the tool names of a Gateway target's inline schema in the AgentCore resources template."""
    template = load("checked_cloudformation", "deployment/validate_infra_baseline.py").load_template(
        (ROOT / "backend/deployment/templates/nested/4-agentcore-resources.yaml").read_text())
    target = template["Resources"][logical_id]["Properties"]["TargetConfiguration"]["Mcp"]["Lambda"]
    return {item["Name"] for item in target["ToolSchema"]["InlinePayload"]}


def hs256(claims, key, header=None):
    """Sign claims as a compact HS256 token with a bytes key, as the workspace broker's PyJWT does."""
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    text = encode({"alg": "HS256", "typ": "JWT"} if header is None else header) + "." + encode(claims)
    signature = base64.urlsafe_b64encode(hmac.new(key, text.encode(), hashlib.sha256).digest())
    return text + "." + signature.rstrip(b"=").decode()


# The signing-secret fixture and per-purpose keys from the G17 key-derivation rule; the workspace API's
# tests assert the same vectors, so both sides derive identical keys.
SIGNING_SECRET = "fixture-signing-secret-for-key-derivation-tests-only"


PURPOSE_KEYS = {"simulation": "1f583bd646287ef5450fa4ebc168be5199fe5037d017f7705f0bf271f6e8594e",
                "investigation": "90fd3123d4bc1e9034e163c7591c44afcda0726864fba97ef4bd6eebaf76a4f9",
                "firetruck_report": "5c40fcda450dc8512906822d04f44c9829b1f2e54ace93190d6d1fbd4a584307"}


def context_claims(purpose, **changes):
    """Build a valid claim set of the given purpose, as the workspace broker signs it, with changes applied."""
    now = int(time.time())
    claims = {"iss": "wildfire-simulation-broker", "aud": "wildfire-simulation-tools-v1", "iat": now, "nbf": now,
              "exp": now+240, "sub": "owner-digest", "session_id": "thread-1", "purpose": purpose,
              "allowed_operations": []}
    if purpose == "simulation":
        claims.update(incident_id="incident-1", location_revision=1, turn_id=str(uuid.uuid4()),
                      attempt_id="attempt-1", draft_id=None, allowed_operations=["get_simulation_status"])
    if purpose == "firetruck_report":
        claims["context_hash"] = hashlib.sha256(b"fixture").hexdigest()
    return {**claims, **changes}


def signed(claims, purpose, header=None):
    """Sign claims with one purpose's key from PURPOSE_KEYS."""
    return hs256(claims, bytes.fromhex(PURPOSE_KEYS[purpose]), header)


PLANTED_ADDRESS = "4521 Canary Ridge Road, Paradise, CA"  # fixture address that must never reach a log record


def stub_module(name, **attributes):
    """Build a stand-in module for a runtime import that a check does not exercise."""
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def runtime_stubs(**modules):
    """Stand-ins for the image-only generated models and model config, plus the real log_safety helper."""
    models = stub_module("schemas.generated_models", **{name: MagicMock(name=name) for name in (
        "AudioLanguage", "CommunicationOutput", "DetectionOutput", "FireData", "FireLocation", "Location",
        "MapData", "MapSummary", "ReportOutput", "ResponseOutput", "WeatherData")})
    config = stub_module("utils.model_config", create_model=lambda *_, **__: MagicMock(spec=Model),
                         cached_system_prompt=lambda value: value)
    return {"schemas.generated_models": models, "utils.model_config": config,
            "utils.structured_response": load("checked_context", "backend/runtime/utils/structured_response.py"),
            "utils.log_safety": load("checked_log_safety", "backend/runtime/utils/log_safety.py"),
            "utils.gateway": load("checked_gateway_names", "backend/runtime/utils/gateway.py"), **modules}


def scripted_agent(output):
    """Build a stand-in Strands Agent class whose calls return the given structured output."""
    class ScriptedAgent:
        def __init__(self, **_kwargs):
            pass

        def __call__(self, *_args, **_kwargs):
            return types.SimpleNamespace(structured_output=types.SimpleNamespace(model_dump=lambda **kwargs: dict(output)))
    return ScriptedAgent


def prompt_text(blocks):
    """Join a specialist prompt's text and guardContent blocks into the text its model reads."""
    return "".join(block["text"] if "text" in block else block["guardContent"]["text"]["text"] for block in blocks)


def generated_schema_models():
    """Import by path the generated schema models that production's code_owned_keys() imports.

    The Runtime image's release tests carry no API schema: they run in the image's WORKDIR (/app) and use
    the image's own models, which production imports from there. A checkout first generates them from
    schemas/api-schema.yaml, as the image build does. Only this module's own sys.modules entry is added
    and removed: pydantic modules first imported here must stay, because a check's restored sys.modules
    would otherwise drop them.
    """
    schema, folder = ROOT / "schemas/api-schema.yaml", None
    if not schema.is_file():
        path = Path.cwd() / "schemas"
    else:
        folder = load("checked_schema_models", "backend/runtime/schema_models.py").generate_models(schema)
        path = ROOT / "backend/runtime/schemas" if folder is None else Path(folder.name) / "schemas"
    try:
        spec = importlib.util.spec_from_file_location("schemas.generated_models", path / "generated_models.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module  # pydantic resolves the models' references through it
        try:
            spec.loader.exec_module(module)
        finally:
            del sys.modules[spec.name]  # every other check keeps the stand-ins
        return module
    finally:
        if folder is not None:
            sys.path.remove(folder.name)
            folder.cleanup()


# Loaded once, before any check swaps sys.modules; they decide the field names our code owns.
SCHEMA_MODELS = generated_schema_models()


def untrusted_tokens(value, keys=frozenset()):
    """Yield in JSON order the tokens a specialist request must guard: each string value and the given keys."""
    if isinstance(value, str):
        yield json.dumps(value, ensure_ascii=False)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in keys:
                yield json.dumps(key, ensure_ascii=False)
            yield from untrusted_tokens(item, keys)
    elif isinstance(value, list):
        for item in value:
            yield from untrusted_tokens(item, keys)


def access_denied(operation):
    """Build the botocore ClientError an AWS call raises while an IAM grant is not yet in effect."""
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "fixture"}}, operation)


# The model settings utils.model_config.settings() requires, and the Region create_model passes, with fixtures.
MODEL_ENV = {"MODEL_TEMPERATURE": "0.1", "DEPLOYED_DeploymentRegion": "eu-central-1",
             **{f"{agent}_MODEL_ID": "fixture-model"
                for agent in ("SUPERVISOR", "DETECTION", "RESPONSE", "COMM", "SIMULATION", "REPORT")}}


def load_registered(name, relative):
    """Import a module from a repository file path and keep it registered under the given name."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_fire_weather_tool():
    """Import the real runtime fire-weather tool by path; stub its image-only generated models."""
    models = ModuleType("schemas.generated_models")
    for name in ("FireData", "FireLocation", "MapData", "MapSummary", "Location", "WeatherData"):
        setattr(models, name, MagicMock(name=name))
    log_safety = load_registered("tested_log_safety", "backend/runtime/utils/log_safety.py")
    gateway = load_registered("tested_gateway", "backend/runtime/utils/gateway.py")
    with patch.dict(sys.modules, {"schemas.generated_models": models, "utils.log_safety": log_safety,
                                  "utils.gateway": gateway}):
        tool = load_registered("tested_fire_weather",
                               "backend/runtime/agents/detection_agent/tools/fire_weather_tool.py")
    return tool, models


def sample():
    """Build an Amazon Location geocode answer with one US street-address match."""
    return {"ResultItems": [{
        "Position": [-118.55, 34.04], "PlaceType": "PointAddress",
        "MatchScores": {"Overall": 0.99},
        "Address": {"Label": "17000 W Sunset Blvd, CA", "Country": {"Code3": "USA"},
                    "Region": {"Code": "CA"}},
    }]}
