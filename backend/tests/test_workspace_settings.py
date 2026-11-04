"""Workspace settings: one public-URL setting and an explicit public-access mode; no sign-in settings."""
import json
import logging
import runpy
import secrets

import pytest
from fastapi.testclient import TestClient
from traffic_simulator import settings as simulator_settings

from backend import api_settings as settings
from backend.api_contracts import API_VERSION, CAPABILITY_VERSION
from backend.evacuation.routes import create_router

REQUIRED_MODE = "STORAGE_MODE=aws requires PUBLIC_DEMO_MODE=true or PUBLIC_DEMO_MODE=false"
# The API's limit variables, as the stack sets them from deployment/settings.toml.
API_LIMITS = {"AGENT_REQUESTS_PER_DAY": "500", "GEOCODING_REQUESTS_PER_DAY": "1000", "ACTIVE_JOBS_ALL_VISITORS": "5",
              "ACTIVE_JOBS_PER_SESSION": "2", "DISCOVERIES_PER_API_PROCESS": "8",
              "INVESTIGATIONS_PER_API_PROCESS": "4", "REQUEST_BODY_MAX_KIB": "160",
              "REQUEST_BODY_SECONDS": "20", "DEMO_SESSIONS_PER_IP_PER_DAY": "20", "MAX_STUDY_RADIUS_KM": "15.0",
              "FIRETRUCK_RUN_LIFETIME_MINUTES": "120", "FIRETRUCK_ACTIVE_RUNS_PER_SESSION": "2",
              "FIRETRUCK_RUNS_PER_SESSION": "100", "FIRETRUCK_STREAMS_PER_SESSION": "4",
              "FIRETRUCK_STREAM_LIFETIME_MINUTES": "15", "CAMERA_CALLS_PER_DAY": "1000", "CAMERA_CACHE_SECONDS": "60",
              "CAMERA_IMAGE_MAX_KIB": "2048", "CAMERA_REQUEST_TIMEOUT_SECONDS": "10"}


def load_settings():
    """Execute backend/api_settings.py afresh without replacing the imported module."""
    return runpy.run_path(settings.__file__)


def deployed(monkeypatch):
    """Emulate the API task: AWS mode with its public URL, open public access and every limit variable."""
    monkeypatch.setattr(simulator_settings, "CLOUD", True)
    monkeypatch.setenv("FABRIC_PUBLIC_URL", "https://workspace.example/")
    monkeypatch.setenv("PUBLIC_DEMO_MODE", "true")
    for variable, value in API_LIMITS.items():
        monkeypatch.setenv(variable, value)


@pytest.mark.parametrize("variable", sorted(API_LIMITS))
def test_aws_mode_refuses_to_start_without_each_api_limit(monkeypatch, variable):
    deployed(monkeypatch)
    # With every variable set, the start-up checks pass, so they read no limit outside API_LIMITS.
    assert load_settings()["public_demo_mode"]() is True
    monkeypatch.delenv(variable)
    with pytest.raises(RuntimeError, match=f"STORAGE_MODE=aws requires {variable} "):
        load_settings()


def test_aws_mode_names_an_out_of_range_api_limit_and_its_range(monkeypatch):
    deployed(monkeypatch)
    monkeypatch.setenv("MAX_STUDY_RADIUS_KM", "20")
    with pytest.raises(RuntimeError, match=r"MAX_STUDY_RADIUS_KM must be a number from 2.0 to 15.0 km, not '20'"):
        load_settings()


def test_aws_mode_requires_the_public_url_and_an_explicit_public_access_mode(monkeypatch):
    deployed(monkeypatch)
    monkeypatch.setenv("PUBLIC_DEMO_MODE", "true")
    monkeypatch.delenv("FABRIC_PUBLIC_URL", raising=False)
    with pytest.raises(RuntimeError, match="FABRIC_PUBLIC_URL"):
        load_settings()
    monkeypatch.setenv("FABRIC_PUBLIC_URL", "https://workspace.example/")
    for value in ("", "yes", "TRUE"):
        monkeypatch.setenv("PUBLIC_DEMO_MODE", value)
        with pytest.raises(RuntimeError, match=REQUIRED_MODE):
            load_settings()
    monkeypatch.delenv("PUBLIC_DEMO_MODE")
    with pytest.raises(RuntimeError, match=REQUIRED_MODE):
        load_settings()
    for value in ("true", "false"):
        monkeypatch.setenv("PUBLIC_DEMO_MODE", value)
        assert load_settings()["public_demo_mode"]() is (value == "true")


def test_public_access_is_closed_unless_explicitly_open(monkeypatch):
    monkeypatch.delenv("PUBLIC_DEMO_MODE", raising=False)
    assert settings.public_demo_mode() is False
    monkeypatch.setenv("PUBLIC_DEMO_MODE", "false")
    assert settings.public_demo_mode() is False
    monkeypatch.setenv("PUBLIC_DEMO_MODE", "true")
    assert settings.public_demo_mode() is True
    monkeypatch.setenv("PUBLIC_DEMO_MODE", "open")
    with pytest.raises(RuntimeError, match="PUBLIC_DEMO_MODE must be true or false, not 'open'"):
        settings.public_demo_mode()


def test_former_sign_in_settings_are_neither_read_nor_refused(monkeypatch):
    monkeypatch.setenv("COGNITO_POOL_ID", "us-west-2_legacy")
    monkeypatch.setenv("COGNITO_CLIENT_ID", "legacy-client")
    assert not {"POOL", "CLIENT", "AUTH_DOMAIN"} & load_settings().keys()


@pytest.mark.parametrize("arn, bound", [
    ("arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fixture", True),
    ("configured-server-runtime", False), ("arn:aws:bedrock-agentcore::123456789012:runtime/fixture", False),
    ("", False),
])
def test_workspace_config_reports_the_runtime_binding_the_brokers_can_use(monkeypatch, arn, bound, api_app):
    """agent_configured applies the Runtime client's full-ARN rule; the versions are the published contract's."""
    monkeypatch.setenv("SIMULATION_RUNTIME_ARN", arn)
    monkeypatch.setenv("GEOCODER_FUNCTION_ARN", "geocoder-fixture")
    app = api_app(None, "local-researcher", create_router)
    config = TestClient(app).get("/api/v1/workspace-config").json()
    assert config["agent_configured"] is bound and settings.runtime_arn() == (arn if bound else "")
    assert config["geocoder_configured"] is True and settings.geocoder_function() == "geocoder-fixture"
    assert (config["api_version"], config["capability_version"]) == (API_VERSION, CAPABILITY_VERSION)
    monkeypatch.delenv("GEOCODER_FUNCTION_ARN")
    assert TestClient(app).get("/api/v1/workspace-config").json()["geocoder_configured"] is False


def test_public_url_has_a_local_default_only_in_local_mode(monkeypatch, api_app):
    monkeypatch.delenv("FABRIC_PUBLIC_URL", raising=False)
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    monkeypatch.delenv("PUBLIC_DEMO_MODE", raising=False)
    app = api_app(None, "local-researcher", create_router)
    config = TestClient(app).get("/api/v1/workspace-config").json()
    # No sign-in configuration is published; the browser learns only how it is served.
    assert config["access_mode"] == "local"
    assert not {"auth_required", "client_id", "auth_domain", "redirect_uri"} & config.keys()
    assert settings.public_url() == "http://127.0.0.1:8000"
    monkeypatch.setenv("PUBLIC_URL", "https://legacy.example")
    assert settings.public_url() == "http://127.0.0.1:8000"
    monkeypatch.setenv("FABRIC_PUBLIC_URL", "https://workspace.example/")
    assert settings.public_url() == "https://workspace.example"
    monkeypatch.delenv("FABRIC_PUBLIC_URL")
    monkeypatch.setattr(simulator_settings, "CLOUD", True)
    assert settings.public_url() == ""


# The [camera] api_base_url table of deployment/tests/test_installation.py, copied because the API image's
# verification stage cannot read deployment/tests; the D7 test there keeps the rule text equal in the three files.
HOST_253 = ".".join(("a" * 63, "b" * 63, "c" * 63, "d" * 61))
ORIGINS = [
    ("https://wildfireai.com", "https://wildfireai.com"),
    ("https://wildfireai.com/", "https://wildfireai.com"),
    ("https://WildfireAI.com", "https://wildfireai.com"),
    ("https://203.0.113.10", None),
    ("https://wildfireai.com.", None),
    ("https://-x.example", None),
    ("https://wildfireai.com:443", None),
    ("https://u@wildfireai.com", None),
    ("http://wildfireai.com", None),
    ("https://wildfireai.com/x", None),
    ("https://wildfireai.com?q=1", None),
    ("https://localhost", None),
    ("https://[::1]", None),
    ("https://" + HOST_253, "https://" + HOST_253),
    ("https://" + HOST_253 + "d", None),
]
# A Delphire-shaped key, built at run time.
KEY = "-".join(("cam", secrets.token_hex(8)))
CAMERA = {"CAMERA_API_BASE_URL": "https://camera.fixture.example", "CAMERA_DEVICE_ID": "fixture-device",
          "CAMERA_ALIAS": "camera_a"}


@pytest.mark.parametrize("value,origin", ORIGINS)
def test_the_api_applies_the_deployers_camera_origin_rule(value, origin):
    assert settings.camera_origin(value) == origin


def test_camera_origin_normalizes_case_one_trailing_slash_and_spaces():
    assert settings.camera_origin("  HTTPS://Camera.Example.COM/ ") == "https://camera.example.com"
    for refused in ("https://camera.example.com//", "https://camera.example.com:443", "https://198.51.100.7"):
        assert settings.camera_origin(refused) is None, refused


@pytest.fixture
def camera_variables(monkeypatch):
    """The camera's non-secret variables as the stack sets them; no key yet, and no warning logged yet."""
    monkeypatch.setattr(settings, "camera_warnings", set())
    for name, value in CAMERA.items():
        monkeypatch.setenv(name, value)
    for name in ("DELPHIRE_API_KEY", "PROVIDER_CREDENTIALS"):
        monkeypatch.delenv(name, raising=False)


def test_aws_mode_reads_the_camera_key_only_from_the_provider_secret(monkeypatch, camera_variables):
    monkeypatch.setattr(simulator_settings, "CLOUD", True)
    monkeypatch.setenv("DELPHIRE_API_KEY", KEY)
    assert settings.camera_binding() is None, "AWS mode ignores the local variable"
    monkeypatch.setenv("PROVIDER_CREDENTIALS", json.dumps({"TOMTOM_API_KEY": "", "DELPHIRE_API_KEY": KEY,
                                                           "nonce": "fixture"}))
    binding = settings.camera_binding()
    assert (binding.origin, binding.device_id, binding.alias, binding.key) == (
        "https://camera.fixture.example", "fixture-device", "camera_a", KEY)
    assert KEY not in repr(binding)


def test_local_mode_reads_the_camera_key_only_from_its_variable(monkeypatch, camera_variables):
    monkeypatch.setenv("PROVIDER_CREDENTIALS", json.dumps({"DELPHIRE_API_KEY": KEY}))
    assert settings.camera_binding() is None
    monkeypatch.setenv("DELPHIRE_API_KEY", KEY)
    assert settings.camera_binding().key == KEY


def test_a_malformed_provider_secret_turns_the_camera_off_with_one_warning_and_no_value(
        monkeypatch, camera_variables, caplog):
    monkeypatch.setattr(simulator_settings, "CLOUD", True)
    with caplog.at_level(logging.WARNING, logger="backend.api_settings"):
        for secret in ('{"DELPHIRE_API_KEY": "' + KEY + '"', json.dumps([KEY]), json.dumps({"DELPHIRE_API_KEY": 7})):
            monkeypatch.setenv("PROVIDER_CREDENTIALS", secret)
            assert settings.camera_binding() is None
    assert caplog.text.count("The camera is off: PROVIDER_CREDENTIALS field DELPHIRE_API_KEY is missing or "
                             "invalid") == 1, "One warning per process names the field"
    assert KEY not in caplog.text
