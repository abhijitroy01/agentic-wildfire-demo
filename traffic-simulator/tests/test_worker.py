"""Worker logs and failed-run messages never expose provider keys, tool output or provider text."""
import asyncio
import copy
import hashlib
import json
import logging
import threading
from email.utils import formatdate

import httpx
import pytest
from test_traffic import area, network
from test_traffic import response as tomtom_response
from traffic_simulator import errors, feeds, input_bundles, traffic, worker
from traffic_simulator.errors import IntegrationError
from traffic_simulator.examples import EXAMPLES
from traffic_simulator.models import Scenario
from traffic_simulator.network import NetworkTooLarge
from traffic_simulator.store import Store
from traffic_simulator.testing import create_job

TOMTOM_KEY = "FAKE-TOMTOM-KEY-not-a-credential"
FIRMS_KEY = "FAKEFIRMSKEYnotacredential"


@pytest.fixture
def fresh_http_loggers():
    """Start with a new process's httpx/httpcore logger levels and restore the session's levels afterwards."""
    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore")]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.setLevel(logging.NOTSET)
    yield
    for logger, level in zip(loggers, levels, strict=True):
        logger.setLevel(level)


def test_worker_logging_keeps_provider_keys_out_of_logs(monkeypatch, caplog, fresh_http_loggers):
    monkeypatch.setenv("TOMTOM_API_KEY", TOMTOM_KEY)
    monkeypatch.setenv("FIRMS_MAP_KEY", FIRMS_KEY)
    sent = []

    def respond(request):
        sent.append(str(request.url))
        if request.url.host == "api.tomtom.com":
            return httpx.Response(200, json=tomtom_response(), headers={"Date": formatdate(usegmt=True)})
        if request.url.host == "firms.modaps.eosdis.nasa.gov":
            return httpx.Response(200, text="latitude,longitude\n34.0259,-118.7798\n")
        return httpx.Response(200, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))

    async def keyed_requests():
        await feeds.fetch_sources(area(), [])
        await traffic.capture_speeds(area(), network())

    # The worker logs at INFO; pytest already owns the root handlers, so set the level here.
    caplog.set_level(logging.INFO)
    asyncio.run(keyed_requests())
    # Control: without the worker's setup, httpx writes the keyed URLs into the log.
    assert TOMTOM_KEY in caplog.text and FIRMS_KEY in caplog.text
    caplog.clear()
    sent.clear()
    worker.configure_logging()
    asyncio.run(keyed_requests())
    assert any(TOMTOM_KEY in url for url in sent) and any(FIRMS_KEY in url for url in sent)
    assert not [r for r in caplog.records if TOMTOM_KEY in r.getMessage() or FIRMS_KEY in r.getMessage()]


def test_worker_refuses_to_start_without_a_valid_policy_parallelism(monkeypatch):
    """In AWS mode the worker stops at start-up, naming POLICY_PARALLELISM, before it touches the job store."""
    monkeypatch.setattr(worker.settings, "CLOUD", True)
    monkeypatch.setattr(worker, "Store", lambda: pytest.fail("The worker must check its settings first."))
    monkeypatch.delenv("POLICY_PARALLELISM", raising=False)
    with pytest.raises(RuntimeError, match=r"requires POLICY_PARALLELISM \(deployment/settings.toml policy_parallelism\)"):
        worker.loop(threading.Event())
    monkeypatch.setenv("POLICY_PARALLELISM", "4")
    with pytest.raises(RuntimeError, match="POLICY_PARALLELISM must be an integer from 1 to 3 policies, not '4'"):
        worker.loop(threading.Event())


def test_worker_loop_never_scans_the_shared_jobs_table(monkeypatch):
    """Delivery and repair belong to the table stream and the scheduled outbox dispatcher, not the worker."""
    stop, scans, receives = threading.Event(), [], []

    class CloudStore:
        cloud = True

        def __init__(self):
            self.sqs = self

        def dispatch_outbox(self, job_id=None):
            scans.append(job_id)

        def receive_message(self, **_):
            receives.append(True)
            stop.set()
            return {"Messages": []}
    monkeypatch.setattr(worker, "Store", CloudStore)
    monkeypatch.setattr(worker.settings, "QUEUE", "https://example.invalid/queue")
    worker.loop(stop)
    assert receives == [True] and scans == []


def test_road_discovery_is_background_fenced_and_preserves_source_manifest(monkeypatch):
    from test_network_providers import source_manifest

    store = Store()
    job = create_job(store, "alice", {"lat": 47.6, "lon": -122.3, "radius_km": 4})
    store.update(job["id"], kind="road_discovery")
    result = {"highways": [], "geometry": {"type": "FeatureCollection", "features": []},
              "source_manifest": source_manifest()}
    calls = []

    def discover(area, progress, check, *, timings):
        check()
        progress("Extracting canonical road snapshot", .06)
        calls.append((area.lat, area.lon, area.radius_km))
        return result

    monkeypatch.setattr(worker, "discover", discover)
    monkeypatch.setattr(worker, "simulate", lambda *_: pytest.fail("Discovery must not run a simulation"))
    assert worker.execute(store, store.get(job["id"]), threading.Event())
    finished = store.get(job["id"])
    assert finished["status"] == "completed" and finished["artifact_attempt"]
    assert store.artifact(job["id"], "result") == result
    assert calls == [(47.6, -122.3, 4)]


def test_local_sumo_worker_does_not_claim_api_fire_risk_jobs(monkeypatch):
    stop = threading.Event()
    job = {"id": "risk-job", "kind": "fire_risk_prepare", "cancel": False}

    class LocalStore:
        cloud = False

        def pending(self):
            stop.set()
            return [job]

    monkeypatch.setattr(worker, "Store", LocalStore)
    monkeypatch.setattr(worker, "execute", lambda *_: pytest.fail("Fire risk has its own consumer"))
    worker.loop(stop)


def test_road_discovery_failure_retains_safe_typed_reason_without_an_artifact(monkeypatch):
    store = Store()
    job = create_job(store, "alice", {"lat": 47.6, "lon": -122.3, "radius_km": 4})
    store.update(job["id"], kind="road_discovery")

    def unavailable(*args, **kwargs):
        raise IntegrationError("NO_DRIVABLE_ROADS", "No passenger roads in this requested study extent.", 422,
                               reason="Private diagnostic text stays out of the public result")

    monkeypatch.setattr(worker, "discover", unavailable)
    assert worker.execute(store, store.get(job["id"]), threading.Event())
    record = store.get(job["id"])
    assert record["status"] == "failed" and record["error_code"] == "NO_DRIVABLE_ROADS"
    assert record["error"] == "No passenger roads in this requested study extent."
    assert "Private diagnostic" not in json.dumps(record) and not record.get("artifact_attempt")


# The variables only the worker task receives, as the stack sets them from deployment/settings.toml.
WORKER_VARIABLES = {name: ",".join(spec["value"]) if isinstance(spec["value"], list) else str(spec["value"])
                    for name, spec in worker.settings.operational_settings().items()
                    if name in {"POLICY_PARALLELISM", "MAX_NETWORK_EDGES",
                                "CACHED_NETWORKS_PER_WORKER", "CACHED_NETWORK_MIB_PER_WORKER",
                                "DEMO_AREAS_TO_PREBUILD", "TOMTOM_REQUEST_CONCURRENCY",
                                "OSM_SOURCE_HTTP_TIMEOUT_SECONDS", "ROAD_PREPARATION_TIMEOUT_SECONDS",
                                "OSM_SOURCE_MAX_ATTEMPTS", "OSM_SOURCE_RETRY_MAX_SECONDS",
                                "OSM_SOURCE_MAX_MIB", "OSM_SOURCE_CACHE_MIB_PER_WORKER",
                                "OSM_INDEX_CACHE_SECONDS", "OSM_ROAD_XML_MAX_MB"}}


@pytest.mark.parametrize("variable", sorted(set(WORKER_VARIABLES) - {"POLICY_PARALLELISM"}))
def test_worker_refuses_to_start_without_each_network_cache_setting(monkeypatch, variable):
    """In AWS mode a missing worker setting stops the worker at start-up, by name, before it touches the store."""
    assert len(WORKER_VARIABLES) == 14 == len(worker.settings.WORKER_SETTINGS)
    monkeypatch.setattr(worker.settings, "CLOUD", True)
    monkeypatch.setattr(worker, "Store", lambda: pytest.fail("The worker must check its settings first."))
    for name, value in WORKER_VARIABLES.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(variable)
    with pytest.raises(RuntimeError, match=rf"requires {variable} \(deployment/settings.toml {variable.lower()}\)"):
        worker.loop(threading.Event())


def test_a_starting_worker_prepares_each_demo_area_once_and_lets_waiting_jobs_go_first(monkeypatch, caplog):
    """Demo areas are prepared one at a time and only while no job waits, so a job waits for at most one build."""
    monkeypatch.setenv("DEMO_AREAS_TO_PREBUILD", "paradise,malibu")
    stop, events, arrivals = threading.Event(), [], []
    first = create_job(Store(), "alice", Scenario().model_dump())

    def execute(store, job, stop, receipt=None, readings=None):
        """Record a job the worker ran and complete it."""
        events.append(("job", job["id"]))
        store.update(job["id"], status="completed")
        return True

    def area_network(area, progress, check, *, timings):
        """Record a demo area the worker prepared; the first time, queue a job as if a visitor started one."""
        events.append(("area", (area.lat, area.lon, area.radius_km)))
        if len(arrivals) == 0:
            # A job arrives while the first demo area is being prepared; it runs before the next area.
            arrivals.append(create_job(Store(), "bob", Scenario().model_dump())["id"])
        else:
            stop.set()
        return None, {}

    monkeypatch.setattr(worker, "execute", execute)
    monkeypatch.setattr(worker, "area_network", area_network)
    with caplog.at_level(logging.INFO, logger="traffic_simulator.worker"):
        worker.loop(stop)
    assert events == [("job", first["id"]), ("area", EXAMPLES["paradise"]), ("job", arrivals[0]),
                      ("area", EXAMPLES["malibu"])]
    ready = [json.loads(r.getMessage().removeprefix("demo_area_ready ")) for r in caplog.records
             if r.getMessage().startswith("demo_area_ready ")]
    assert [line["area"] for line in ready] == ["paradise", "malibu"]


def test_a_failed_demo_area_is_logged_and_skipped(monkeypatch, caplog):
    """A provider outage during the warm-up never stops the worker; that area's first run builds its network."""
    monkeypatch.setenv("DEMO_AREAS_TO_PREBUILD", "paradise,malibu")
    stop, attempts = threading.Event(), []

    def area_network(area, progress, check, *, timings):
        """Fail the first demo area as a canonical publisher outage would; stop after the second."""
        attempts.append(area.lat)
        if len(attempts) == 1:
            raise RuntimeError("The canonical OSM road source is unavailable.")
        stop.set()
        return None, {}

    monkeypatch.setattr(worker, "area_network", area_network)
    worker.loop(stop)
    assert attempts == [EXAMPLES["paradise"][0], EXAMPLES["malibu"][0]]
    assert "Demo area paradise road network was not prepared; its first run builds it" in caplog.text


def test_an_empty_demo_area_list_skips_the_warm_up(monkeypatch):
    """With cache.demo_areas_to_prebuild = [] the worker only waits for jobs."""
    monkeypatch.setenv("DEMO_AREAS_TO_PREBUILD", "")
    monkeypatch.setattr(worker, "area_network", lambda *_, **__: pytest.fail("Nothing to prepare."))

    class StopOnWait(threading.Event):
        """A stop event that ends the loop the first time the worker would wait for a job."""

        def wait(self, timeout=None):
            """Stop instead of waiting."""
            self.set()
            return True

    worker.loop(StopOnWait())


def test_a_cloud_worker_polls_briefly_until_its_demo_areas_are_prepared(monkeypatch):
    """While demo areas remain, SQS waits are 1 second, so a delivered job goes before the next build; then 20."""
    monkeypatch.setenv("DEMO_AREAS_TO_PREBUILD", "paradise")
    stop, waits, prepared = threading.Event(), [], []

    class CloudStore:
        """A cloud job store whose queue is always empty."""
        cloud = True

        def __init__(self):
            self.sqs = self

        def receive_message(self, **request):
            """Record the long-poll wait and return no message; stop on the second poll."""
            waits.append(request["WaitTimeSeconds"])
            if len(waits) == 2:
                stop.set()
            return {"Messages": []}

    monkeypatch.setattr(worker, "Store", CloudStore)
    monkeypatch.setattr(worker.settings, "QUEUE", "https://example.invalid/queue")
    monkeypatch.setattr(worker, "area_network", lambda area, *_, **__: prepared.append(area.lat) or (None, {}))
    worker.loop(stop)
    assert waits == [1, 20] and prepared == [EXAMPLES["paradise"][0]]


def failed_run(tmp_path, monkeypatch, error):
    """Run one queued job whose road network step raises `error` and return the stored job record."""
    store = Store(tmp_path)
    job = create_job(store, "alice", Scenario().model_dump())

    def fail(*_, **__):
        raise error
    monkeypatch.setattr(worker, "prepare_network", fail)
    assert worker.execute(store, job, threading.Event())
    return store.get(job["id"])


@pytest.mark.parametrize("error,public", [
    (IntegrationError("NETWORK_INTEGRITY_ERROR", "Frozen network failed its integrity check.", 409),
     "Frozen network failed its integrity check."),
    (RuntimeError("Both public OSM network services are unavailable. Retry later."),
     "Both public OSM network services are unavailable. Retry later."),
    (NetworkTooLarge("Network exceeds the 50 MB source limit. Reduce the area radius."),
     "Network exceeds the 50 MB source limit. Reduce the area radius."),
    (TimeoutError("Network conversion timed out"), errors.PUBLIC_FAILURES[0][1]),
    (FileNotFoundError(2, "No such file or directory", "/srv/private/networks/abc/network.net.xml"),
     errors.PUBLIC_FAILURES[1][1]),
    (json.JSONDecodeError("Expecting value", "<html>/srv/private provider page</html>", 0),
     errors.UNEXPECTED_FAILURE),
    (KeyError("/srv/private/provider-field"), errors.UNEXPECTED_FAILURE),
])
def test_failed_runs_store_public_text_and_log_the_detail(tmp_path, monkeypatch, caplog, error, public):
    job = failed_run(tmp_path, monkeypatch, error)
    assert job["status"] == "failed"
    assert job["error"] == public
    assert str(error) in caplog.text
    # Module loggers follow the repository convention (logging.getLogger(__name__)).
    assert any(r.name == "traffic_simulator.worker" and r.exc_info for r in caplog.records)
    # A failed run keeps the phase timings it reached, for diagnosing slow failures.
    assert {"queue_wait", "total"} <= job["timings"].keys()


@pytest.mark.integration
def test_completed_runs_record_phase_timings_and_log_one_line_per_job(tmp_path, monkeypatch, caplog, grid):
    """The job record gets a public timings map (phase -> seconds) and the log one job_timings line, no URLs."""
    store = Store(tmp_path)
    # Two policies, so the worker's cancellation checks and progress writes run alongside policy processes.
    scenario = Scenario(vehicles=20, crews=0, radius_km=3, duration_minutes=5, departure_minutes=1,
                        hazard_radius_m=0, hazard_growth_m_min=0, policies=["baseline", "phased"])
    job = create_job(store, "alice", scenario.model_dump())
    metadata = {"net_sha256": hashlib.sha256(grid[0].read_bytes()).hexdigest(), "selected_route": {"districts": []}}

    def prepare(scenario, progress, check, *, timings, route_selected=None):
        timings.add("network_cache_hit", 0.25)
        return (*grid[:7], metadata)

    async def sources(*_):
        return {"sources": []}

    monkeypatch.setattr(worker, "prepare_network", prepare)
    monkeypatch.setattr(worker, "fetch_sources", sources)
    with caplog.at_level(logging.INFO, logger="traffic_simulator.worker"):
        assert worker.execute(store, job, threading.Event())
    record = store.get(job["id"])
    assert record["status"] == "completed"
    timings = record["timings"]
    assert set(timings) == {"queue_wait", "network_cache_hit", "public_feeds", "input_bundle", "artifact_writes",
                            "simulation", "policy_baseline", "policy_phased", "total"}
    assert record["progress"] == 1 and [s["policy"] for s in record["summaries"]] == ["baseline", "phased"]
    assert all(isinstance(seconds, int | float) and seconds >= 0 for seconds in timings.values())
    assert timings["network_cache_hit"] == 0.25 and timings["total"] >= timings["simulation"]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("job_timings ")]
    assert len(lines) == 1
    logged = json.loads(lines[0].removeprefix("job_timings "))
    assert logged["job_id"] == job["id"] and logged["outcome"] == "completed"
    assert set(logged) == {"job_id", "kind", "outcome", "timings"} and set(logged["timings"]) == set(timings)
    assert "http" not in lines[0] and str(scenario.lat) not in lines[0]


def artifact_bytes(result):
    """Encode a result as the job store writes the result artifact (Store.artifact), for byte comparison."""
    return json.dumps(result, separators=(",", ":"), allow_nan=False).encode()


@pytest.mark.integration
def test_public_feeds_overlap_the_road_network_and_never_change_the_result(tmp_path, monkeypatch, grid):
    """The feeds start once discovery picks the highway, during the network build; either order gives equal bytes."""
    scenario = Scenario(vehicles=20, crews=1, radius_km=3, duration_minutes=5, departure_minutes=1,
                        hazard_radius_m=0, hazard_growth_m_min=0, policies=["baseline", "phased"])
    metadata = {"net_sha256": hashlib.sha256(grid[0].read_bytes()).hexdigest(), "selected_route": {"districts": [3]}}
    observed = {"sources": [{"name": "Fixture feed", "status": "available", "records": []}]}
    # One fixed bundle ID, so the two runs' frozen inputs (and their digests) can be compared byte for byte.
    monkeypatch.setattr(input_bundles, "uuid4", lambda: "00000000-0000-4000-8000-000000000001")

    def run(order):
        """Run one job whose fake network build and feeds finish in `order`; return its record, result and bundle."""
        started, built = threading.Event(), threading.Event()

        def prepare(scenario, progress, check, *, timings, route_selected=None):
            """Report the route, then build the network, waiting for the feeds to start when they go first."""
            route_selected(metadata["selected_route"])
            if order == "feeds_first" and not started.wait(10):
                raise AssertionError("The public feeds did not start while the road network was prepared")
            built.set()
            return (*grid[:7], copy.deepcopy(metadata))

        async def fetch(area, districts, readings=None):
            """Return the fixed observations, after the network build when the network goes first."""
            started.set()
            if order == "network_first" and not built.wait(10):
                raise AssertionError("The road network never finished")
            assert districts == [3]
            return copy.deepcopy(observed)

        monkeypatch.setattr(worker, "prepare_network", prepare)
        monkeypatch.setattr(worker, "fetch_sources", fetch)
        store = Store(tmp_path / order)
        job = create_job(store, "alice", scenario.model_dump())
        assert worker.execute(store, job, threading.Event())
        record = store.get(job["id"])
        return record, store.artifact(job["id"], "result"), store.artifact(job["id"], "bundle")

    overlapped, sequential = run("feeds_first"), run("network_first")
    for record, _, _ in (overlapped, sequential):
        assert record["status"] == "completed" and "public_feeds" in record["timings"]
    assert overlapped[2] == sequential[2] and overlapped[2]["sources"] == observed
    assert artifact_bytes(overlapped[1]) == artifact_bytes(sequential[1])
    assert overlapped[1]["policies"][0]["summary"]["exited_vehicles"] > 0


def test_a_cancel_while_the_feeds_are_fetched_stops_the_run_without_waiting_for_them(tmp_path, monkeypatch):
    """The worker checks for cancellation while it waits for the background feeds and does not wait them out."""
    store = Store(tmp_path)
    job = create_job(store, "alice", Scenario().model_dump())
    release = threading.Event()

    def prepare(scenario, progress, check, *, timings, route_selected=None):
        """Report the route and return a placeholder network at once."""
        route_selected({"districts": []})
        return (None,) * 7 + ({"selected_route": {"districts": []}},)

    async def fetch(area, districts, readings=None):
        """Ask to cancel the run, then hold the feeds until the test releases them."""
        store.cancel_run(job["id"])
        release.wait(30)
        return {"sources": []}

    monkeypatch.setattr(worker, "prepare_network", prepare)
    monkeypatch.setattr(worker, "fetch_sources", fetch)
    try:
        assert worker.execute(store, job, threading.Event())
        assert not release.is_set(), "the worker returned while the feeds were still being fetched"
    finally:
        release.set()
    assert store.get(job["id"])["status"] == "cancelled"
