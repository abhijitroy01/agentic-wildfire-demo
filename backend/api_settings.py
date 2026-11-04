"""Workspace API settings: the public browser origin, whether anonymous public access is open, the optional service
bindings (the Delphire camera among them) and API limits.

There is no user sign-in; deployed visitors use demo sessions (backend.demo_access).
Storage settings (STORAGE_MODE, AWS_REGION, ARTIFACT_BUCKET, ...) live only in
traffic_simulator.settings; read them there so every module and test sees one value.
"""
import json
import logging
import os
import re
from dataclasses import dataclass, field

from traffic_simulator import settings as simulator_settings

from backend import trusted_context

logger = logging.getLogger(__name__)

LOCAL_URL = "http://127.0.0.1:8000"
# PUBLIC_DEMO_MODE: "true" opens anonymous demo sessions through CloudFront; "false" keeps every
# deployed workspace route closed. AWS mode requires it; local mode treats it as unset (closed).
PUBLIC_DEMO_VALUES = ("true", "false")
if simulator_settings.CLOUD and not os.getenv("FABRIC_PUBLIC_URL"):
    raise RuntimeError("STORAGE_MODE=aws requires FABRIC_PUBLIC_URL")
if simulator_settings.CLOUD and os.getenv("PUBLIC_DEMO_MODE") not in PUBLIC_DEMO_VALUES:
    raise RuntimeError("STORAGE_MODE=aws requires PUBLIC_DEMO_MODE=true or PUBLIC_DEMO_MODE=false")


def public_url() -> str:
    """Return the workspace browser origin (FABRIC_PUBLIC_URL); only local mode defaults to the local server."""
    return os.getenv("FABRIC_PUBLIC_URL", "" if simulator_settings.CLOUD else LOCAL_URL).rstrip("/")


def public_demo_mode() -> bool:
    """Return whether anonymous public demo access is open (PUBLIC_DEMO_MODE=true); unset means closed."""
    value = os.getenv("PUBLIC_DEMO_MODE")
    if value is None:
        return False
    if value not in PUBLIC_DEMO_VALUES:
        raise RuntimeError(f"PUBLIC_DEMO_MODE must be true or false, not {value!r}")
    return value == "true"


# Optional service bindings, read when a request needs them (a missing one disables only its feature). Every route,
# the Runtime client and workspace-config read them here, so all apply the same validity rule.
def runtime_arn() -> str:
    """SIMULATION_RUNTIME_ARN: the AgentCore Runtime the brokers invoke, as a full ARN; "" when unset or malformed.

    A malformed value counts as unset: Runtime-backed routes answer 503 AGENT_NOT_CONFIGURED and workspace-config
    reports agent_configured false, which stops the deployer's binding check.
    """
    value = os.getenv("SIMULATION_RUNTIME_ARN", "")
    return value if trusted_context.FULL_ARN.fullmatch(value) else ""


def geocoder_function() -> str:
    """GEOCODER_FUNCTION_ARN: the Amazon Location geocoder Lambda that address lookups invoke; "" when unset."""
    return os.getenv("GEOCODER_FUNCTION_ARN", "")


def weather_function() -> str:
    """Existing IAM-only DTN Lambda; an absent binding disables fresh weather enrichment."""
    return os.getenv("WEATHER_FUNCTION_ARN", "")


def fire_risk_service() -> tuple[str, str] | None:
    """Return the fixed private modeler binding; caller-controlled URLs never reach this service."""
    from urllib.parse import urlsplit
    value = os.getenv("FIRE_RISK_SERVICE_URL", "")
    key = os.getenv("FIRE_RISK_SERVICE_KEY", "")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        valid_host = host.endswith(".internal") if simulator_settings.CLOUD else (
            host in ("localhost", "127.0.0.1") or host.endswith(".internal"))
        if (parsed.scheme == "http" and valid_host and parsed.port and parsed.path in ("", "/")
                and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                and 32 <= len(key) <= 8192 and all(33 <= ord(char) <= 126 for char in key)):
            return value.rstrip("/"), key
    except ValueError:
        pass
    return None


def fire_risk_limit(name: str) -> int | float:
    """Read an explicitly named fire risk setting from the shared operational authority."""
    return simulator_settings.limit("FIRE_RISK_" + name.upper())


def fire_risk_moisture_profile() -> str:
    spec, raw = simulator_settings.declared_setting("FIRE_RISK_SURFACE_MOISTURE_PROFILE")
    value = spec["value"] if raw is None else raw
    if value not in spec["allowed"]:
        raise RuntimeError("FIRE_RISK_SURFACE_MOISTURE_PROFILE must be low, moderate or high")
    return value


# Operational limits the API alone reads (decisions D20, D23). deployment/settings.toml holds each default, unit and
# range; AWS mode requires the upper-case variable, which the stack sets from that file, and local runs and tests use
# the file's value (tests override it through the variable). traffic_simulator.settings.limit() parses them all.
def agent_requests_per_day() -> int:
    """AGENT_REQUESTS_PER_DAY: AgentCore agent requests all visitors may start per UTC day (budgets section)."""
    return simulator_settings.limit("AGENT_REQUESTS_PER_DAY")


def geocoding_requests_per_day() -> int:
    """GEOCODING_REQUESTS_PER_DAY: Amazon Location address lookups all visitors may start per UTC day."""
    return simulator_settings.limit("GEOCODING_REQUESTS_PER_DAY")


def active_jobs_all_visitors() -> int:
    """ACTIVE_JOBS_ALL_VISITORS: queued, running and cancelling simulation jobs allowed across all visitors."""
    return simulator_settings.limit("ACTIVE_JOBS_ALL_VISITORS")


def active_jobs_per_session() -> int:
    """ACTIVE_JOBS_PER_SESSION: queued, running and cancelling simulation jobs one demo session may have."""
    return simulator_settings.limit("ACTIVE_JOBS_PER_SESSION")


def discoveries_per_api_process() -> int:
    """DISCOVERIES_PER_API_PROCESS: blocking Caltrans highway discoveries one API process runs at once."""
    return simulator_settings.limit("DISCOVERIES_PER_API_PROCESS")


def investigations_per_api_process() -> int:
    """INVESTIGATIONS_PER_API_PROCESS: saved investigations executing in one API process."""
    return simulator_settings.limit("INVESTIGATIONS_PER_API_PROCESS")


def request_body_max_kib() -> int:
    """REQUEST_BODY_MAX_KIB: largest API request body, in KiB; larger bodies get 413 BODY_TOO_LARGE."""
    return simulator_settings.limit("REQUEST_BODY_MAX_KIB")


def request_body_seconds() -> int:
    """REQUEST_BODY_SECONDS: time a client has to send an API request body; slower bodies get 408."""
    return simulator_settings.limit("REQUEST_BODY_SECONDS")


def demo_sessions_per_ip_per_day() -> int:
    """DEMO_SESSIONS_PER_IP_PER_DAY: new demo sessions one viewer IP may start per UTC day."""
    return simulator_settings.limit("DEMO_SESSIONS_PER_IP_PER_DAY")


def max_study_radius_km() -> float:
    """MAX_STUDY_RADIUS_KM: largest study-area radius a visitor may simulate, discover or look up sources for."""
    return simulator_settings.limit("MAX_STUDY_RADIUS_KM")


def firetruck_run_lifetime_minutes() -> int:
    """FIRETRUCK_RUN_LIFETIME_MINUTES: longest a firetruck run lasts; the broker stops counting older runs as active."""
    return simulator_settings.limit("FIRETRUCK_RUN_LIFETIME_MINUTES")


def firetruck_active_runs_per_session() -> int:
    """FIRETRUCK_ACTIVE_RUNS_PER_SESSION: firetruck runs one demo session may have starting or running at once."""
    return simulator_settings.limit("FIRETRUCK_ACTIVE_RUNS_PER_SESSION")


def firetruck_runs_per_session() -> int:
    """FIRETRUCK_RUNS_PER_SESSION: firetruck runs one demo session may start in its lifetime."""
    return simulator_settings.limit("FIRETRUCK_RUNS_PER_SESSION")


def firetruck_streams_per_session() -> int:
    """FIRETRUCK_STREAMS_PER_SESSION: live firetruck update streams one demo session may have open at once."""
    return simulator_settings.limit("FIRETRUCK_STREAMS_PER_SESSION")


def firetruck_stream_lifetime_minutes() -> int:
    """FIRETRUCK_STREAM_LIFETIME_MINUTES: longest one live firetruck update stream stays open, in minutes."""
    return simulator_settings.limit("FIRETRUCK_STREAM_LIFETIME_MINUTES")


def camera_calls_per_day() -> int:
    """CAMERA_CALLS_PER_DAY: Delphire camera calls all visitors may cause per UTC day (budgets section)."""
    return simulator_settings.limit("CAMERA_CALLS_PER_DAY")


def camera_cache_seconds() -> int:
    """CAMERA_CACHE_SECONDS: how long one API process reuses the camera's latest records and image (cache section)."""
    return simulator_settings.limit("CAMERA_CACHE_SECONDS")


def camera_image_max_kib() -> int:
    """CAMERA_IMAGE_MAX_KIB: largest camera image the broker reads from Delphire and serves, in KiB."""
    return simulator_settings.limit("CAMERA_IMAGE_MAX_KIB")


def camera_request_timeout_seconds() -> int:
    """CAMERA_REQUEST_TIMEOUT_SECONDS: longest one Delphire camera call may take, connect to last byte."""
    return simulator_settings.limit("CAMERA_REQUEST_TIMEOUT_SECONDS")


# An API task that lacks one of these, or has a value outside its range, stops at start-up naming the variable.
STARTUP_LIMITS = (agent_requests_per_day, geocoding_requests_per_day, active_jobs_all_visitors,
                  active_jobs_per_session, discoveries_per_api_process, investigations_per_api_process,
                  request_body_max_kib, request_body_seconds,
                  demo_sessions_per_ip_per_day, max_study_radius_km, firetruck_run_lifetime_minutes,
                  firetruck_active_runs_per_session, firetruck_runs_per_session, firetruck_streams_per_session,
                  firetruck_stream_lifetime_minutes, camera_calls_per_day, camera_cache_seconds,
                  camera_image_max_kib, camera_request_timeout_seconds)
if simulator_settings.CLOUD:
    for accessor in STARTUP_LIMITS:
        accessor()


# The optional Delphire camera (the installation file's [camera] section; backend.camera serves it). D7: the
# same rule text is in deployment/installation.py and deployment/infra/lib/stacks.ts, and
# deployment/tests/test_installation.py keeps the three equal. Protocol bounds, not operational settings (D38).
# Lower-case DNS labels; the last label starts with a letter, so an IPv4 host never matches.
CAMERA_ORIGIN = r"https://([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?"
# "https://" (8 characters) plus a host of at most 253.
CAMERA_ORIGIN_MAX_LENGTH = 261
CAMERA_DEVICE_ID = r"[A-Za-z0-9_-]{1,64}"
# The camera aliases of Delphire's latest-records answer (owner brief 2026-10-14).
CAMERA_ALIASES = ("camera_a", "camera_b", "infrared_camera_a", "infrared_camera_b", "test_camera_a")
# A key is visible ASCII (0x21-0x7E) only, so it is always a valid Authorization header value.
CAMERA_KEY_MIN_LENGTH = 16
CAMERA_KEY_MAX_LENGTH = 8192
# Where the camera key comes from in AWS mode: the application provider secret, which the API task receives whole.
CAMERA_KEY_SOURCE = "PROVIDER_CREDENTIALS field DELPHIRE_API_KEY"
# Variables already reported as unusable by this process, so a bad value is logged once, not on every request.
camera_warnings: set[str] = set()


@dataclass(frozen=True, slots=True)
class CameraBinding:
    """The Delphire camera the broker serves: an HTTPS origin, one device, one camera alias and the key."""
    origin: str
    device_id: str
    alias: str
    key: str = field(repr=False)


def camera_origin(value: str) -> str | None:
    """Return the Delphire API origin normalized (lower case, no trailing slash), or None if it is not one."""
    origin = value.strip().lower().removesuffix("/")
    return origin if len(origin) <= CAMERA_ORIGIN_MAX_LENGTH and re.fullmatch(CAMERA_ORIGIN, origin) else None


def camera_key_valid(value: object) -> bool:
    """True for a usable Delphire key: a string of CAMERA_KEY_MIN_LENGTH-CAMERA_KEY_MAX_LENGTH visible ASCII characters."""
    return (isinstance(value, str) and CAMERA_KEY_MIN_LENGTH <= len(value) <= CAMERA_KEY_MAX_LENGTH
            and all("\x21" <= character <= "\x7e" for character in value))


def camera_key() -> object:
    """Return the configured Delphire key value as found, or None when it is absent or the secret is malformed.

    AWS mode reads the DELPHIRE_API_KEY field of PROVIDER_CREDENTIALS (the whole application provider secret, set
    when the task starts); local mode reads the DELPHIRE_API_KEY variable. Neither falls back to the other.
    """
    if not simulator_settings.CLOUD:
        return os.getenv("DELPHIRE_API_KEY")
    try:
        secret = json.loads(os.getenv("PROVIDER_CREDENTIALS") or "{}")
    except ValueError:
        return None
    return secret.get("DELPHIRE_API_KEY") if isinstance(secret, dict) else None


def camera_off(variable: str) -> None:
    """Log once per process that the camera is off because a variable is missing or unusable; never its value."""
    if variable not in camera_warnings:
        camera_warnings.add(variable)
        logger.warning("The camera is off: %s is missing or invalid", variable)


def camera_binding() -> CameraBinding | None:
    """Return the configured Delphire camera, or None when it is off or its configuration is unusable.

    Read per request, like the other optional service bindings. CAMERA_DEVICE_ID empty or unset is the camera
    turned off. Otherwise CAMERA_API_BASE_URL, CAMERA_ALIAS, the device ID and the key must pass the deployer's
    rules; a value that does not keeps the camera off (fail closed) and logs the variable's name once.
    """
    device = os.getenv("CAMERA_DEVICE_ID", "").strip()
    if not device:
        return None
    origin = camera_origin(os.getenv("CAMERA_API_BASE_URL", ""))
    alias = os.getenv("CAMERA_ALIAS", "").strip()
    key = camera_key()
    for variable, usable in (("CAMERA_API_BASE_URL", origin is not None),
                             ("CAMERA_DEVICE_ID", re.fullmatch(CAMERA_DEVICE_ID, device) is not None),
                             ("CAMERA_ALIAS", alias in CAMERA_ALIASES),
                             (CAMERA_KEY_SOURCE if simulator_settings.CLOUD else "DELPHIRE_API_KEY",
                              camera_key_valid(key))):
        if not usable:
            camera_off(variable)
            return None
    return CameraBinding(origin, device, alias, key)
