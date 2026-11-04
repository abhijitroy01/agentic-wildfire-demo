"""Simulation engine and job-store configuration shared by the worker and the workspace API."""
import os
import tomllib
from functools import cache
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parent
# Operational limits (decisions D23, D24): the checked-in deployment/settings.toml holds each value, unit and
# allowed range. The images copy the file to the same place relative to this package.
SETTINGS_FILE = ROOT.parent / "deployment" / "settings.toml"
# Local runs keep scratch networks, results and job records here (git-ignored).
# Containers set DATA_DIR to their writable scratch volume. Created on first use, not at import.
DATA = Path(os.getenv("DATA_DIR", str(ROOT / ".runtime")))
# Required with no default: a missing or mistyped mode must never fall back to local mode,
# where the workspace API serves every caller as one shared local identity.
STORAGE_MODE = os.getenv("STORAGE_MODE")
if STORAGE_MODE not in ("local", "aws"):
    raise RuntimeError(f"STORAGE_MODE must be 'local' or 'aws' (got {STORAGE_MODE!r}); there is no default")
CLOUD = STORAGE_MODE == "aws"
# No default Region: AWS mode requires the task's AWS_REGION; local mode lets the SDK chain decide.
REGION = os.getenv("AWS_REGION", "")
BUCKET = os.getenv("ARTIFACT_BUCKET", "")
TABLE = os.getenv("JOBS_TABLE", "")
QUEUE = os.getenv("QUEUE_URL", "")
if CLOUD and not all((REGION, BUCKET, TABLE, QUEUE)):
    raise RuntimeError("STORAGE_MODE=aws requires AWS_REGION, ARTIFACT_BUCKET, JOBS_TABLE and QUEUE_URL")


def data_dir(*parts: str) -> Path:
    """Return a scratch data folder under DATA_DIR, creating it (and DATA_DIR) on first use."""
    folder = DATA.joinpath(*parts)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@cache
def operational_settings() -> dict:
    """Read deployment/settings.toml once, keyed by environment variable: {"TOMTOM_CALLS_PER_DAY": {value, ...}}.

    The deployer validates the file (deployment.installation.load_settings); the images do not contain the
    deployer package, so components read the same file here and check only the values they are given.
    """
    try:
        with SETTINGS_FILE.open("rb") as handle:
            sections = tomllib.load(handle)
    except FileNotFoundError:
        raise RuntimeError(f"Operational settings file {SETTINGS_FILE} is missing; it ships with the repository "
                           "and the images") from None
    return {key.upper(): spec for entries in sections.values() for key, spec in entries.items()}


def declared_setting(variable: str) -> tuple[dict, str | None]:
    """Return a setting's settings.toml entry and its environment value (None when unset); AWS mode requires it."""
    spec = operational_settings().get(variable)
    if spec is None:
        raise RuntimeError(f"{variable} is not declared in {SETTINGS_FILE.name}; declare every operational limit "
                           "there (decision D23)")
    raw = os.getenv(variable)
    if raw is None and CLOUD:
        raise RuntimeError(f"STORAGE_MODE=aws requires {variable} (deployment/settings.toml "
                           f"{variable.lower()}); deploy with bash deployment/deploy-all.sh --apply")
    return spec, raw


def limit(variable: str) -> int | float:
    """Return an operational limit from its environment variable, else (local mode only) its settings.toml value.

    AWS mode requires the variable: the stack sets every one from deployment/settings.toml. A malformed or
    out-of-range value raises RuntimeError naming the variable, the value and the allowed range.
    """
    spec, raw = declared_setting(variable)
    if raw is None:
        return spec["value"]
    kind = float if isinstance(spec["value"], float) else int
    try:
        value = kind(raw)
    except ValueError:
        value = None
    if value is None or not spec["min"] <= value <= spec["max"]:
        raise RuntimeError(f"{variable} must be {'a number' if kind is float else 'an integer'} from "
                           f"{spec['min']} to {spec['max']} {spec['unit']}, not {raw!r}")
    return value


def names(variable: str) -> list[str]:
    """Return a list-of-names setting from its comma-separated environment variable, else its settings.toml value.

    AWS mode requires the variable ("" means an empty list). A name outside the setting's allowed values raises
    RuntimeError naming the variable, the value and the allowed names.
    """
    spec, raw = declared_setting(variable)
    if raw is None:
        return list(spec["value"])
    values = [name.strip() for name in raw.split(",") if name.strip()]
    if any(name not in spec["allowed"] for name in values):
        raise RuntimeError(f"{variable} must list only {', '.join(spec['allowed'])} (comma-separated, or empty), "
                           f"not {raw!r}")
    return values


def tomtom_calls_per_day() -> int:
    """TOMTOM_CALLS_PER_DAY: TomTom requests all visitors may cause per UTC day (budgets.tomtom_calls_per_day)."""
    return limit("TOMTOM_CALLS_PER_DAY")


def tomtom_requests_per_run() -> int:
    """TOMTOM_REQUESTS_PER_RUN: TomTom readings one traffic-speed run captures (simulation.tomtom_requests_per_run)."""
    return limit("TOMTOM_REQUESTS_PER_RUN")


def tomtom_response_max_age_seconds() -> int:
    """TOMTOM_RESPONSE_MAX_AGE_SECONDS: oldest provider response date a fetched TomTom reading may carry."""
    return limit("TOMTOM_RESPONSE_MAX_AGE_SECONDS")


def tomtom_cache_minutes() -> int:
    """TOMTOM_CACHE_MINUTES: how long a process reuses a TomTom reading for the same point (cache.tomtom_cache_minutes)."""
    return limit("TOMTOM_CACHE_MINUTES")


def tomtom_request_concurrency() -> int:
    """TOMTOM_REQUEST_CONCURRENCY: TomTom flow requests one traffic-speed run sends at once; 1 sends them in turn.

    simulation.tomtom_request_concurrency; only the worker captures traffic, and worker.loop() checks it at start-up.
    """
    return limit("TOMTOM_REQUEST_CONCURRENCY")


def caltrans_response_max_mb() -> int:
    """CALTRANS_RESPONSE_MAX_MB: largest Caltrans highway-service response one discovery request reads, in MB.

    simulation.caltrans_response_max_mb, in megabytes of 1,000,000 bytes; the API and the worker both discover.
    """
    return limit("CALTRANS_RESPONSE_MAX_MB")


def demo_session_days() -> int:
    """DEMO_SESSION_DAYS: lifetime of a demo session and of the records it creates (capacity.demo_session_days)."""
    return limit("DEMO_SESSION_DAYS")


def max_study_radius_km() -> int:
    """MAX_STUDY_RADIUS_KM: common API admission and native road-preparation extent cap."""
    return limit("MAX_STUDY_RADIUS_KM")


def policy_parallelism() -> int:
    """POLICY_PARALLELISM: scenario policies a worker simulates at once in separate processes; 1 runs them in turn.

    simulation.policy_parallelism; only the worker task receives it, and worker.loop() checks it at start-up.
    """
    return limit("POLICY_PARALLELISM")


def max_network_edges() -> int:
    """MAX_NETWORK_EDGES: largest road network a run accepts, in car edges over 15 m (simulation.max_network_edges)."""
    return limit("MAX_NETWORK_EDGES")


def overpass_response_max_mb() -> int:
    """Historical configuration compatibility only; new canonical road preparation does not read this limit."""
    return limit("OVERPASS_RESPONSE_MAX_MB")


def osm_source_http_timeout_seconds() -> int:
    """Configured publisher transport wait; zero disables application HTTP timers."""
    return limit("OSM_SOURCE_HTTP_TIMEOUT_SECONDS")


def osm_source_max_attempts() -> int:
    return limit("OSM_SOURCE_MAX_ATTEMPTS")


def osm_source_retry_max_seconds() -> int:
    return limit("OSM_SOURCE_RETRY_MAX_SECONDS")


def artifact_write_max_attempts() -> int:
    return limit("ARTIFACT_WRITE_MAX_ATTEMPTS")


def road_preparation_timeout_seconds() -> int:
    """Configured native preparation wall deadline; zero keeps cancellable jobs pending without a deadline."""
    return limit("ROAD_PREPARATION_TIMEOUT_SECONDS")


def osm_source_max_mib() -> int:
    """Largest selected regional source file, checked before downloading."""
    return limit("OSM_SOURCE_MAX_MIB")


def osm_source_cache_mib_per_worker() -> int:
    """Separate bounded disk allowance for immutable regional source files."""
    return limit("OSM_SOURCE_CACHE_MIB_PER_WORKER")


def osm_index_cache_seconds() -> int:
    """Publisher polygon-index freshness; every new plan still resolves the current immutable file."""
    return limit("OSM_INDEX_CACHE_SECONDS")


def osm_road_xml_max_mb() -> int:
    """Largest complete-way road extraction, in 1,000,000-byte megabytes."""
    return limit("OSM_ROAD_XML_MAX_MB")


def cached_networks_per_worker() -> int:
    """CACHED_NETWORKS_PER_WORKER: road networks one worker keeps on disk (cache.cached_networks_per_worker)."""
    return limit("CACHED_NETWORKS_PER_WORKER")


def cached_network_mib_per_worker() -> int:
    """CACHED_NETWORK_MIB_PER_WORKER: disk one worker's cached road networks may use, in MiB.

    cache.cached_network_mib_per_worker; least recently used networks are deleted past it or the count above.
    """
    return limit("CACHED_NETWORK_MIB_PER_WORKER")


def demo_areas_to_prebuild() -> list[str]:
    """DEMO_AREAS_TO_PREBUILD: Traffic Lab example areas whose road networks a starting worker prepares while idle.

    cache.demo_areas_to_prebuild; names from traffic_simulator.examples.EXAMPLES, empty to turn the warm-up off.
    """
    return names("DEMO_AREAS_TO_PREBUILD")


# Only the worker task receives these (the API never builds networks in AWS); worker.loop() checks them at start-up,
# so a worker with a missing or out-of-range value stops before it takes a job, naming the variable.
WORKER_SETTINGS = (policy_parallelism, max_network_edges, cached_networks_per_worker,
                   cached_network_mib_per_worker, demo_areas_to_prebuild, tomtom_request_concurrency,
                   osm_source_http_timeout_seconds, road_preparation_timeout_seconds, osm_source_max_mib,
                   osm_source_cache_mib_per_worker, osm_index_cache_seconds, osm_road_xml_max_mb,
                   osm_source_max_attempts, osm_source_retry_max_seconds)


# Both images import this module, so both tasks receive these variables. An AWS task that lacks one, or has a value
# outside its settings.toml range, stops at start-up with an error naming the variable.
STARTUP_LIMITS = (tomtom_calls_per_day, tomtom_requests_per_run, tomtom_response_max_age_seconds,
                  tomtom_cache_minutes, demo_session_days, caltrans_response_max_mb, max_study_radius_km,
                  artifact_write_max_attempts)
if CLOUD:
    for accessor in STARTUP_LIMITS:
        accessor()
