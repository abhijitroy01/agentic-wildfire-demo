import asyncio
import json
import logging
import shutil
import signal
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait

from traffic_simulator import artifact_io, input_bundles, network_preparation, result_reuse, settings
from traffic_simulator.errors import IntegrationError, public_message
from traffic_simulator.examples import EXAMPLES
from traffic_simulator.feeds import fetch_sources
from traffic_simulator.http_logging import quiet_http_client_logs
from traffic_simulator.models import Area, PreparationScenario, Scenario
from traffic_simulator.network import area_network, discover, prepare_network
from traffic_simulator.persistence import digest
from traffic_simulator.simulation import simulate
from traffic_simulator.store import LEASE_SECONDS, Store
from traffic_simulator.timings import Timings
from traffic_simulator.traffic import Readings, capture_speeds

logger = logging.getLogger(__name__)
# The phase text of a run that reused an identical earlier run's result instead of simulating (result_reuse).
REUSED_PHASE = "Comparison ready; reused the result of an identical earlier run"


class Cancelled(Exception):
    pass


class Interrupted(Exception):
    pass


def public_feeds(scenario, districts, readings, timings):
    """Fetch a run's public observation feeds for its area and highway districts, timed as public_feeds."""
    with timings.measure("public_feeds"):
        return asyncio.run(fetch_sources(scenario, districts, readings))


def wait_for(future, check):
    """Return a background task's result, checking for cancellation about once a second until it finishes."""
    while wait([future], timeout=1).not_done:
        check()
    return future.result()


def execute(store, job, stop, receipt=None, readings=None):
    """Claim and run one queued job under its lease; readings holds the process's TomTom budget and reuse cache.

    A run whose scenario and frozen inputs match a completed earlier run reuses that run's result (result_reuse).
    """
    readings = readings or Readings(store)
    token = str(uuid.uuid4())
    job_id = job["id"]
    if not store.claim(job_id, token):
        return False
    job = store.get(job_id)
    # Phase durations (traffic_simulator.timings): stored with the final status and logged once per job.
    timings, outcome = Timings(), "interrupted"
    timings.add("queue_wait", max(0.0, time.time() - job["created"]))
    restored_folder = None
    done, lease_lost = threading.Event(), threading.Event()

    def timing_map():
        """Return this job's phase timings, with total set to the seconds from job creation until now."""
        with timings.lock:
            timings.seconds["total"] = max(0.0, time.time() - job["created"])
        return timings.public()

    def heartbeat():
        while not done.wait(30):
            try:
                store.heartbeat(job_id, token)
                if receipt:
                    store.sqs.change_message_visibility(
                        QueueUrl=settings.QUEUE, ReceiptHandle=receipt, VisibilityTimeout=LEASE_SECONDS)
            except Exception:
                logger.exception("Lease heartbeat failed for %s", job_id)
                lease_lost.set()
                return

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()

    def check():
        if stop.is_set() or lease_lost.is_set():
            raise Interrupted()
        current = store.get(job_id)
        if not current or current.get("leaseToken") != token:
            lease_lost.set()
            raise Interrupted()
        if current.get("cancel"):
            raise Cancelled()

    def progress(phase, fraction):
        check()
        if not store.fenced_update(job_id, token, phase=phase, progress=round(fraction, 3)):
            raise Interrupted()

    def artifact(identifier, name, value=None, *, attempt=None):
        return artifact_io.retained(lambda: store.artifact(identifier, name, value, attempt=attempt), check)

    try:
        if job.get("kind") == "road_discovery":
            result = discover(Area.model_validate(job["scenario"]), progress, check, timings=timings)
            check()
            with timings.measure("artifact_writes"):
                artifact(job_id, "result", result, attempt=token)
            published = store.finish(job_id, token, [], artifact_attempt=token,
                                     phase="Road corridor candidates ready for review", timings=timing_map())
            outcome = "completed" if published else "not_published"
            return published
        if job.get("kind") == "network_preparation":
            model = PreparationScenario if job["scenario"].get("highway") == "auto" else Scenario
            scenario = model.model_validate(job["scenario"])
            bundle = network_preparation.prepare(scenario, progress, check, timings=timings,
                                                  extent_mode=job.get("extent_mode", "fixed"),
                                                  max_radius_km=job.get(
                                                      "max_radius_km", settings.max_study_radius_km()))
            check()
            if bundle.get("outcome") == "unavailable":
                with timings.measure("artifact_writes"):
                    artifact(job_id, "coverage", {**bundle, "sha256": digest(bundle)}, attempt=token)
                if not store.fenced_update(job_id, token, preparation_outcome="unavailable",
                                           road_coverage=bundle["coverage"]):
                    raise Interrupted()
                published = store.finish(job_id, token, [], artifact_attempt=token,
                                         phase="Road coverage checked; simulation unavailable",
                                         timings=timing_map())
                outcome = "completed" if published else "not_published"
                return published
            with timings.measure("artifact_writes"):
                artifact(job_id, "bundle", bundle, attempt=token)
                artifact(job_id, "network", bundle["network"], attempt=token)
            if not store.fenced_update(job_id, token, prepared_scenario=bundle["scenario"],
                                       network_validation=bundle["validation"], preparation_outcome="ready",
                                       road_coverage=bundle["validation"].get("coverage")):
                raise Interrupted()
            published = store.finish(job_id, token, [], artifact_attempt=token,
                                     phase="Road network ready for review", timings=timing_map())
            outcome = "completed" if published else "not_published"
            return published
        scenario = Scenario.model_validate(job["scenario"])
        if job.get("parent_run_id") or job.get("input_attempt"):
            progress("Loading immutable parent network, traffic, demand and crew plans", 0.05)
            with timings.measure("input_restore"):
                bundle = (artifact(job["parent_run_id"], "bundle") if job.get("parent_run_id")
                          else artifact(job_id, "bundle", attempt=job["input_attempt"]))
                # Checks the frozen inputs before anything is copied. The road network itself is restored only when
                # the run is simulated: an identical earlier run's result may be reused instead.
                demand = input_bundles.demand_for_bundle(bundle, scenario)
            prepared, sources, traffic = None, bundle["sources"], bundle["traffic"]
        else:
            # The public feeds need only the selected highway's districts, so a background thread fetches them
            # while the road network is downloaded and converted (or restored).
            feeds, fetching = ThreadPoolExecutor(max_workers=1, thread_name_prefix="public-feeds"), []

            def fetch_feeds(route):
                """Start fetching the public feeds for the selected highway's districts, once, in the background."""
                if not fetching:
                    fetching.append(feeds.submit(public_feeds, scenario, route["districts"], readings, timings))

            try:
                if job.get("prepared_network_id"):
                    with timings.measure("network_restore"):
                        preparation = store.get(job["prepared_network_id"])
                        if (not preparation or preparation.get("owner") != job["owner"]
                                or preparation["status"] != "completed"
                                or preparation.get("preparation_outcome", "ready") != "ready"):
                            raise ValueError("Validated road network is unavailable. Prepare the scenario again.")
                        prepared = network_preparation.restore(artifact(preparation["id"], "bundle"),
                                                               scenario)
                    restored_folder = prepared[0].parent
                else:
                    prepared = prepare_network(scenario, progress, check, timings=timings,
                                               route_selected=fetch_feeds)
                progress("Fetching fire, road, traffic, and weather observations", 0.12)
                fetch_feeds(prepared[7]["selected_route"])
                sources = wait_for(fetching[0], check)
            finally:
                # A failed or cancelled preparation does not wait for feeds still being fetched.
                feeds.shutdown(wait=False, cancel_futures=True)
            check()
            traffic = None
            if scenario.traffic_mode == "provider_speed_limits":
                progress("Capturing current highway speeds and checking directional road matches", 0.14)
                with timings.measure("tomtom_capture"):
                    traffic = asyncio.run(capture_speeds(scenario, prepared[6], check, readings=readings))
            with timings.measure("input_bundle"):
                bundle = input_bundles.freeze_inputs(scenario, prepared, sources, traffic)
                demand = input_bundles.demand_for_bundle(bundle, scenario)
        check()
        with timings.measure("artifact_writes"):
            # Publish the complete frozen input checkpoint before its derived browser artifacts. Recovery
            # after this fence reloads these exact inputs and never repeats a paid traffic/weather capture.
            artifact(job_id, "bundle", bundle, attempt=token)
            if not store.fenced_update(job_id, token, input_attempt=token):
                raise Interrupted()
            # The bundle's network is the public road network the run used (prepared[6] for a fresh run).
            for name, value in (("network", bundle["network"]), ("sources", sources)):
                artifact(job_id, name, value, attempt=token)
        # The same scenario on the same frozen inputs, model and engine gives the same result, so an identical
        # earlier run's result is reused instead of simulating again.
        reuse_key, looked_up = result_reuse.key(bundle, scenario), time.monotonic()
        result = result_reuse.find(store, reuse_key)
        reused = result is not None
        if reused:
            timings.add("result_reuse", time.monotonic() - looked_up)
        else:
            if prepared is None:
                with timings.measure("input_restore"):
                    prepared = input_bundles.restore(bundle)
                restored_folder = prepared[0].parent
            with timings.measure("simulation"):
                result = simulate(scenario, prepared, progress, check, traffic=traffic, demand=demand,
                                  timings=timings)
        progress("Saving the reproducible comparison and replay", 0.98)
        result["sources"] = sources
        result.update(schema_version=result_reuse.RESULT_SCHEMA_VERSION, input_bundle_id=bundle["bundle_id"],
                      input_bundle_sha256=bundle["sha256"], parent_run_id=job.get("parent_run_id"),
                      variant_changes=job.get("changes", {}))
        manifest = {
            "job_id": job_id, "created": job["created"], "completed": time.time(),
            "scenario": scenario.model_dump(), "network": bundle["metadata"],
            "engine": result["engine"], "engine_version": result["engine_version"],
            "traffic_mode": scenario.traffic_mode,
            "purpose": "Research demo; not validated for incident command or public evacuation guidance",
            "input_bundle_id": bundle["bundle_id"], "input_bundle_sha256": bundle["sha256"],
        }
        if reused:
            manifest["reused_result"] = result["reused_result"]
        with timings.measure("artifact_writes"):
            artifact(job_id, "result", result, attempt=token)
            artifact(job_id, "manifest", manifest, attempt=token)
        check()
        published = store.finish(job_id, token, [
            {"policy": p["policy"], **p["summary"]} for p in result["policies"]
        ], artifact_attempt=token, phase=REUSED_PHASE if reused else "Comparison ready", timings=timing_map())
        if published and not reused:
            result_reuse.remember(store, reuse_key, job, token)
        outcome = "completed" if published else "not_published"
        return published
    except Cancelled:
        outcome = "cancelled"
        store.fenced_update(job_id, token, status="cancelled", phase="Cancelled; no completed comparison",
                            timings=timing_map())
        return True
    except (Interrupted, InterruptedError):
        # Never acknowledge an unfinished SQS message. An expired lease permits recovery.
        if not lease_lost.is_set():
            current = store.get(job_id)
            store.fenced_update(job_id, token, status="cancelling" if current.get("cancel") else "queued",
                                phase="Worker stopped; queued for retry", leaseUntil=0)
        return False
    except Exception as exc:
        # The detail (tool output, provider text, paths) stays in this server log; users get public text.
        logger.exception("Simulation failed for %s", job_id)
        outcome = "failed"
        if not lease_lost.is_set():
            classification = {"error_code": exc.code} if isinstance(exc, IntegrationError) else {}
            store.fenced_update(job_id, token, status="failed", phase="Run failed", error=public_message(exc),
                                timings=timing_map(), **classification)
        return True
    finally:
        done.set()
        thread.join(timeout=2)
        if restored_folder:
            shutil.rmtree(restored_folder)
        # One structured line per job: identifiers, outcome and phase seconds only, never URLs or keys.
        logger.info("job_timings %s", json.dumps({"job_id": job_id, "kind": job.get("kind"), "outcome": outcome,
                                                  "timings": timing_map()}, sort_keys=True))


def prebuild(name, stop):
    """Prepare one demo area's road network in the network cache (copied from the shared cache, or built).

    A failure is logged and skipped, so that area's first run builds its network instead; a worker stop ends the
    preparation at its next check.
    """
    lat, lon, radius_km = EXAMPLES[name]
    timings = Timings()

    def check():
        """Stop preparing once the worker is stopping."""
        if stop.is_set():
            raise Interrupted()

    try:
        area_network(Area(lat=lat, lon=lon, radius_km=radius_km), lambda *_: None, check, timings=timings)
    except Interrupted:
        return
    except Exception:
        # Provider text and tool output stay in this server log, as for a failed run.
        logger.warning("Demo area %s road network was not prepared; its first run builds it", name, exc_info=True)
        return
    logger.info("demo_area_ready %s", json.dumps({"area": name, "timings": timings.public()}, sort_keys=True))


def loop(stop=None):
    """Run queued jobs until stopped: SQS deliveries in AWS mode, the local job table in local mode.

    While no job waits, a starting worker first prepares the road network of each demo area in
    cache.demo_areas_to_prebuild, one area at a time, so a queued job waits for at most one of those builds.
    """
    stop = stop or threading.Event()
    # The worker's own settings: in AWS mode a missing or out-of-range value stops it here, by name.
    for accessor in settings.WORKER_SETTINGS:
        accessor()
    store = Store()
    # One per worker process, so repeat runs of an area reuse its recent TomTom readings.
    readings = Readings(store)
    # Each configured demo area is prepared once, whenever the queue is empty.
    demo_areas = settings.demo_areas_to_prebuild()
    while not stop.is_set():
        try:
            if store.cloud:
                # The table stream and the scheduled outbox dispatcher deliver committed jobs (and repair missed
                # deliveries), so the worker never scans the shared table. While demo areas remain, a 1-second long
                # poll (which, unlike a short poll, sees every message) lets a waiting job go before the next build.
                messages = store.sqs.receive_message(
                    QueueUrl=settings.QUEUE, WaitTimeSeconds=1 if demo_areas else 20, MaxNumberOfMessages=1,
                    VisibilityTimeout=LEASE_SECONDS).get("Messages", [])
                for message in messages:
                    job = store.get(json.loads(message["Body"])["id"])
                    terminal = not job or job["status"] in ("completed", "failed", "cancelled")
                    if job and job.get("cancel"):
                        terminal = store.acknowledge_cancel(job["id"]) or terminal
                    if terminal or execute(store, job, stop, message["ReceiptHandle"], readings):
                        store.sqs.delete_message(QueueUrl=settings.QUEUE, ReceiptHandle=message["ReceiptHandle"])
                if not messages and demo_areas and not stop.is_set():
                    prebuild(demo_areas.pop(0), stop)
            else:
                processed = False
                for job in store.pending():
                    # Fire risk has a separate API consumer, even when both queues use local storage.
                    if job.get("kind") == "fire_risk_prepare":
                        continue
                    if stop.is_set():
                        break
                    if job["cancel"]:
                        store.acknowledge_cancel(job["id"])
                    else:
                        processed = execute(store, job, stop, readings=readings) or processed
                if not processed and demo_areas and not stop.is_set():
                    prebuild(demo_areas.pop(0), stop)
                elif not processed:
                    stop.wait(2)
        except Exception:
            logger.exception("Worker iteration failed")
            stop.wait(5)


def configure_logging():
    """Configure worker logging at INFO without httpx request lines, whose URLs carry provider keys."""
    logging.basicConfig(level=logging.INFO)
    quiet_http_client_logs()


if __name__ == "__main__":
    configure_logging()
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    loop(stopping)
