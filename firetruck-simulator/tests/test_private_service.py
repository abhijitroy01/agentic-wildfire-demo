"""Private firetruck service: service key and owner checks, the evidence store's conditions, the run loop and the
engine's exercise behavior (dispatch timing, unit roles, water telemetry, alerts and distances)."""
import asyncio
import copy
import json
import os
import random
import re
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock
from uuid import uuid4

os.environ.setdefault("ENVIRONMENT", "local")
os.environ["FIRETRUCK_SERVICE_KEY"] = "test-key-" + "x"*64
# Tests set operational limits through the environment; images have no deployment/settings.toml.
os.environ["FIRETRUCK_EVIDENCE_EVENTS_MAX"] = "50"
os.environ["FIRETRUCK_RUN_LIFETIME_MINUTES"] = "120"
os.environ["FIRETRUCK_RUNS_PER_TASK"] = "50"

from api import private
from api.models.incident import EventType, FireLocation, TimelineEvent
from api.models.truck import (
    ActivityStatus,
    FireTruck,
    PumpStatus,
    TruckUpdateSimplified,
    TruckWater,
    UnitType,
)
from botocore.exceptions import ClientError
from config import get_settings
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from simulator.engine import SimulationEngine
from simulator.movement import calculate_distance_feet, move_point

OWNER, OTHER = "a"*64, "b"*64
BODY = {"fire_location": {"latitude": 34.04, "longitude": -118.52}}


def condition_failed():
    """Return the error DynamoDB raises when a ConditionExpression is false."""
    return ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "false"}}, "UpdateItem")


class FakeTable:
    """In-memory DynamoDB table for the condition and update expressions EvidenceStore uses."""

    def __init__(self):
        self.items = {}

    def check(self, expression, item, values, names):
        for clause in expression.split(" AND "):
            if clause.startswith("attribute_not_exists("):
                holds = item is None
            elif clause.startswith("attribute_exists("):
                holds = item is not None
            else:
                name, operator, value = re.fullmatch(r"([#\w]+)(>=|=)(:\w+)", clause.strip()).groups()
                current = (item or {}).get(names.get(name, name))
                holds = current is not None and (current >= values[value] if operator == ">="
                                                 else current == values[value])
            if not holds:
                raise condition_failed()

    def put_item(self, Item, ConditionExpression):
        self.check(ConditionExpression, self.items.get(Item["id"]), {}, {})
        self.items[Item["id"]] = copy.deepcopy(Item)

    def get_item(self, Key, ConsistentRead):
        assert ConsistentRead
        item = self.items.get(Key["id"])
        return {"Item": copy.deepcopy(item)} if item else {}

    def update_item(self, Key, UpdateExpression, ConditionExpression, ExpressionAttributeNames,
                    ExpressionAttributeValues, ReturnValues=None):
        names, values = ExpressionAttributeNames, ExpressionAttributeValues
        item = self.items.get(Key["id"])
        self.check(ConditionExpression, item, values, names)
        replaced = {}
        for assignment in UpdateExpression.removeprefix("SET ").split(","):
            name, value = (part.strip() for part in assignment.split("="))
            name = names.get(name, name)
            if name in item:
                replaced[name] = item[name]
            item[name] = values[value]
        return {"Attributes": replaced} if ReturnValues == "UPDATED_OLD" else {}


class FakeBody:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def close(self):
        pass


class FakeS3:
    """In-memory S3 bucket for evidence snapshots."""

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")
        return {"Body": FakeBody(self.objects[(Bucket, Key)])}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key))


def snapshot_keys(s3, run_id):
    """List the stored snapshot object keys of one run."""
    return [key for _, key in s3.objects if key.startswith(f"firetruck/{run_id}/")]


class EngineClock:
    """Stands in for simulator.engine's datetime: now() returns a time the test advances by hand."""

    def __init__(self):
        self.current = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)

    def now(self, tz=None):
        return self.current

    def advance(self, seconds):
        self.current += timedelta(seconds=seconds)


class PrivateServiceTests(unittest.TestCase):
    def setUp(self):
        self.table, self.s3 = FakeTable(), FakeS3()
        private.store = private.EvidenceStore(table=self.table, s3=self.s3, bucket="evidence")
        self.headers = {"Authorization": "Bearer "+os.environ["FIRETRUCK_SERVICE_KEY"], "X-Firetruck-Owner": OWNER}

    def latest(self, run_id):
        """Return the snapshot a run row currently points at."""
        return json.loads(self.s3.objects[("evidence", self.table.items[run_id]["snapshot_key"])])

    def test_service_key_owner_binding_and_idempotent_start(self):
        run_id = str(uuid4())
        path = "/internal/runs/"+run_id
        other = {**self.headers, "X-Firetruck-Owner": OTHER}
        with TestClient(private.app) as browser:
            self.assertEqual(browser.put(path, json=BODY).status_code, 401)
            # A non-ASCII Authorization header is an ordinary failed authentication, not a server error.
            accented = {"Authorization": "Bearer \u00e9".encode("latin-1")}
            self.assertEqual(browser.put(path, headers=accented, json=BODY).status_code, 401)
            self.assertEqual(browser.put(path, headers={"Authorization": self.headers["Authorization"]},
                                         json=BODY).status_code, 400)
            self.assertEqual(browser.put(path, headers=self.headers, json=BODY).status_code, 200)
            self.assertEqual(browser.put(path, headers=self.headers, json=BODY).json()["session_id"], run_id)
            self.assertEqual(browser.put(path, headers=other, json=BODY).status_code, 409)
            self.assertEqual(browser.put(path, headers=self.headers, json={**BODY, "initial_truck_count": 6}
                                         ).status_code, 409)
            result = browser.get(path, headers=self.headers).json()
            self.assertEqual(result["source_mode"], "synthetic_firetruck_simulation")
            self.assertTrue(result["events"])
            self.assertEqual(result["omitted_events"], 0)
            self.assertEqual(browser.get(path, headers=other).status_code, 404)
            self.assertEqual(browser.delete(path, headers=other).status_code, 404)
            self.assertFalse(self.table.items[run_id]["stop"])
            self.assertEqual(browser.get("/api/simulation/list").status_code, 404)
        # Graceful task shutdown persists evidence as interrupted, not completed.
        self.assertEqual(self.latest(run_id)["status"], "interrupted")
        self.assertEqual(len(snapshot_keys(self.s3, run_id)), 1)

    def test_owner_stops_a_live_run(self):
        run_id = str(uuid4())
        with TestClient(private.app) as browser:
            browser.put("/internal/runs/"+run_id, headers=self.headers, json=BODY)
            stopping = browser.delete("/internal/runs/"+run_id, headers=self.headers).json()
            self.assertEqual(stopping["status"], "stopping")
            self.assertTrue(self.table.items[run_id]["stop"])

    def test_a_run_that_cannot_start_is_failed_not_left_starting(self):
        run_id = str(uuid4())
        path = "/internal/runs/"+run_id
        with TestClient(private.app) as browser:
            with mock.patch.object(SimulationEngine, "start", side_effect=ValueError("bad position")), \
                    self.assertLogs("api.private", "ERROR"):
                self.assertEqual(browser.put(path, headers=self.headers, json=BODY).status_code, 500)
            self.assertEqual(self.table.items[run_id]["status"], "failed")
            self.assertEqual(browser.get(path, headers=self.headers).status_code, 410)
            self.assertEqual(browser.put(path, headers=self.headers, json=BODY).json()["status"], "failed")
            self.assertEqual(browser.delete(path, headers=self.headers).json()["status"], "failed")
        self.assertEqual(private.tasks, {})

    def test_store_conditions_on_create_attempt_lease_and_stop(self):
        store, run_id = private.store, str(uuid4())
        self.assertTrue(store.create(run_id, BODY, "attempt-1", OWNER))
        self.assertFalse(store.create(run_id, BODY, "attempt-2", OWNER))
        for configuration, owner in ((BODY, OTHER), ({"other": True}, OWNER)):
            with self.assertRaises(HTTPException) as conflict:
                store.create(run_id, configuration, "attempt-3", owner)
            self.assertEqual(conflict.exception.status_code, 409)
        with self.assertRaises(HTTPException) as starting:
            store.read(store.get(run_id))
        self.assertEqual(starting.exception.status_code, 409)
        with self.assertRaises(private.LeaseLost):
            store.publish(run_id, "attempt-2", {"status": "running", "n": 0})
        self.assertNotIn("snapshot_key", self.table.items[run_id])
        store.publish(run_id, "attempt-1", {"status": "running", "n": 1})
        first = self.table.items[run_id]["snapshot_key"]
        store.publish(run_id, "attempt-1", {"status": "running", "n": 2})
        # The replaced snapshot is deleted once the row points at the new one.
        self.assertEqual(snapshot_keys(self.s3, run_id), [self.table.items[run_id]["snapshot_key"]])
        self.assertNotEqual(self.table.items[run_id]["snapshot_key"], first)
        # A stalled owner whose lease expired cannot write again; readers show the run interrupted.
        self.table.items[run_id]["lease_until"] = int(time.time()) - 1
        current = self.table.items[run_id]["snapshot_key"]
        with self.assertRaises(private.LeaseLost):
            store.publish(run_id, "attempt-1", {"status": "running", "n": 3})
        self.assertEqual(self.table.items[run_id]["snapshot_key"], current)
        self.assertEqual(snapshot_keys(self.s3, run_id), [current])
        self.assertEqual(self.table.items[run_id]["status"], "running")
        read = store.read(store.get(run_id))
        self.assertEqual((read["status"], read["n"], read["stop_requested"]), ("interrupted", 2, False))
        store.stop(run_id)
        self.assertTrue(store.read(store.get(run_id))["stop_requested"])
        with self.assertRaises(ClientError):
            store.stop(str(uuid4()))
        with self.assertRaises(HTTPException) as missing:
            store.get(str(uuid4()))
        self.assertEqual(missing.exception.status_code, 404)

    def test_a_read_racing_the_next_update_returns_the_newer_snapshot(self):
        store, run_id = private.store, str(uuid4())
        store.create(run_id, BODY, "attempt-1", OWNER)
        store.publish(run_id, "attempt-1", {"status": "running", "n": 1})
        # A reader took the row just before the owner's next update replaced (and deleted) the snapshot it names.
        stale = store.get(run_id)
        store.publish(run_id, "attempt-1", {"status": "running", "n": 2})
        self.assertEqual(store.read(stale)["n"], 2)
        current = store.get(run_id)
        with mock.patch.object(store, "get", side_effect=[stale, current]), TestClient(private.app) as browser:
            response = browser.get("/internal/runs/"+run_id, headers=self.headers)
        self.assertEqual((response.status_code, response.json()["n"]), (200, 2))
        # A snapshot missing for any other reason still fails the read.
        self.s3.objects.clear()
        with self.assertRaises(ClientError):
            store.read(store.get(run_id))

    def run_loop(self, engine, attempt, started=None):
        """Create the run row for an engine and run its update loop to completion."""
        private.store.create(engine.session_id, BODY, attempt, OWNER)
        if started is not None:
            self.table.items[engine.session_id]["started"] = started
        asyncio.run(private.simulate(engine, attempt))

    def started_engine(self, interval=0):
        """Return a started engine with its own run ID and the given update interval in seconds."""
        engine = SimulationEngine(FireLocation(latitude=34.04, longitude=-118.52), update_interval_seconds=interval)
        engine.session_id = str(uuid4())
        asyncio.run(engine.start())
        return engine

    def test_run_loop_stops_on_request_and_at_its_lifetime(self):
        engine = self.started_engine()
        private.store.create(engine.session_id, BODY, "attempt-1", OWNER)
        private.store.stop(engine.session_id)
        asyncio.run(private.simulate(engine, "attempt-1"))
        stopped = self.latest(engine.session_id)
        self.assertEqual(stopped["status"], "stopped")
        self.assertIsNotNone(stopped["final_stats"])
        engine = self.started_engine()
        # FIRETRUCK_RUN_LIFETIME_MINUTES decides when a run stops; a run 10 minutes old meets a 10-minute lifetime.
        short = get_settings().model_copy(update={"firetruck_run_lifetime_minutes": 10})
        with mock.patch.object(private, "get_settings", return_value=short):
            self.run_loop(engine, "attempt-1", started=int(time.time()) - 10 * 60)
        self.assertEqual(self.latest(engine.session_id)["status"], "stopped")
        self.assertFalse(engine.is_running)
        self.assertEqual(private.tasks, {})

    def test_run_loop_ticks_the_engine_and_records_each_new_phase(self):
        """FTRK-2: each loop update runs SimulationEngine.update, so past 180 s the phase history gains INITIAL_ATTACK."""
        clock = EngineClock()
        with mock.patch("simulator.engine.datetime", clock):
            engine = self.started_engine()
            private.store.create(engine.session_id, BODY, "attempt-1", OWNER)
            dispatched = len(engine.timeline_events)
            read, reads = self.table.get_item, []

            def next_update(**request):
                """The loop reads its run row once per update: move the engine clock a minute; stop at the fourth."""
                reads.append(request)
                clock.advance(60)
                if len(reads) == 4:
                    private.store.stop(engine.session_id)
                return read(**request)

            self.table.get_item = next_update
            asyncio.run(private.simulate(engine, "attempt-1"))
        final = self.latest(engine.session_id)
        self.assertEqual(final["status"], "stopped")
        self.assertEqual(final["phase_history"], [{"phase": "INITIAL_DISPATCH", "elapsed_seconds": 0},
                                                  {"phase": "INITIAL_ATTACK", "elapsed_seconds": 180}])
        self.assertEqual(final["incident_elapsed_seconds"], 180)
        self.assertGreater(len(final["events"]), dispatched)

    def test_a_task_refuses_runs_past_its_runs_per_task_limit(self):
        one = get_settings().model_copy(update={"firetruck_runs_per_task": 1})
        with mock.patch.object(private, "get_settings", return_value=one), TestClient(private.app) as browser:
            self.assertEqual(browser.put("/internal/runs/"+str(uuid4()), headers=self.headers, json=BODY).status_code,
                             200)
            refused = str(uuid4())
            self.assertEqual(browser.put("/internal/runs/"+refused, headers=self.headers, json=BODY).status_code, 429)
            self.assertNotIn(refused, self.table.items)
            existing = next(iter(self.table.items))
            self.assertEqual(browser.put("/internal/runs/"+existing, headers=self.headers, json=BODY).status_code,
                             200, "An idempotent replay must succeed at full capacity.")

    def test_concurrent_starts_reserve_capacity_before_the_durable_create_returns(self):
        async def exercise():
            entered, release = asyncio.Event(), threading.Event()
            original = private.store.create
            loop = asyncio.get_running_loop()

            def delayed_create(*args):
                result = original(*args)
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(timeout=5):
                    raise TimeoutError("The test did not release the store.")
                return result

            one = get_settings().model_copy(update={"firetruck_runs_per_task": 1})
            request = private.StartSimulationRequest(**BODY)
            run_id = uuid4()
            with mock.patch.object(private, "get_settings", return_value=one), \
                    mock.patch.object(private.store, "create", side_effect=delayed_create):
                first = asyncio.create_task(private.start(run_id, request, OWNER))
                try:
                    await asyncio.wait_for(entered.wait(), timeout=5)
                    with self.assertRaises(HTTPException) as refusal:
                        await private.start(uuid4(), request, OWNER)
                    self.assertEqual(refusal.exception.status_code, 429)
                    self.assertEqual(len(private.starting), 1)
                finally:
                    release.set()
                await first
            for task in list(private.tasks.values()):
                task.cancel()
            await asyncio.gather(*list(private.tasks.values()), return_exceptions=True)
            self.assertFalse(private.starting)
        asyncio.run(exercise())

    def test_run_loop_ends_quietly_when_another_attempt_owns_the_run(self):
        engine = self.started_engine()
        private.store.create(engine.session_id, BODY, "attempt-1", OWNER)
        asyncio.run(private.simulate(engine, "attempt-2"))
        self.assertNotIn("snapshot_key", self.table.items[engine.session_id])
        self.assertFalse(engine.is_running)

    def test_cancelled_run_saves_interrupted_evidence_and_stays_cancelled(self):
        engine = self.started_engine(interval=30)
        private.store.create(engine.session_id, BODY, "attempt-1", OWNER)

        async def cancel_after_first_snapshot():
            task = asyncio.create_task(private.simulate(engine, "attempt-1"))
            while "snapshot_key" not in self.table.items[engine.session_id]:
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(cancel_after_first_snapshot())
        self.assertEqual(self.latest(engine.session_id)["status"], "interrupted")

    def test_running_snapshots_keep_the_latest_events_and_final_ones_keep_all(self):
        engine = self.started_engine()
        limit = get_settings().firetruck_evidence_events_max
        now = datetime.now(UTC)
        for number in range(limit + 5):
            engine.timeline_events.append(TimelineEvent(
                time="00:00:01", elapsed_seconds=1, event_type=EventType.ARRIVAL, message=f"event {number}",
                timestamp=now))
        total = len(engine.timeline_events)
        running = private.snapshot(engine, "running", [])
        self.assertEqual(len(running["events"]), limit)
        self.assertEqual(running["omitted_events"], total - limit)
        self.assertEqual(running["events"][-1]["message"], f"event {limit + 4}")
        for status in private.FINAL:
            final = private.snapshot(engine, status, [])
            self.assertEqual((len(final["events"]), final["omitted_events"]), (total, 0))

    def test_all_phases_and_stop_summary_are_preserved(self):
        async def exercise():
            engine = SimulationEngine(FireLocation(latitude=34.04, longitude=-118.52))
            await engine.start()
            history = []
            for elapsed, phase in [(0, "INITIAL_DISPATCH"), (180, "INITIAL_ATTACK"),
                    (600, "BUILDING_RESPONSE"), (1200, "FULL_RESPONSE"),
                    (1800, "SUSTAINED_OPS"), (3600, "ROTATION")]:
                engine.elapsed_seconds = elapsed
                engine._update_phase()
                self.assertEqual(engine.phase.value, phase)
                history.append({"phase": phase, "elapsed_seconds": elapsed})
            stats = await engine.stop()
            result = private.snapshot(engine, "stopped", history, stats)
            self.assertEqual(len(result["phase_history"]), 6)
            self.assertEqual(result["final_stats"]["duration_seconds"], 3600)
            self.assertEqual(len(result["trucks"]), 4)
            self.assertTrue(result["events"])
        asyncio.run(exercise())


class ModelContractTests(unittest.TestCase):
    def test_timestamps_are_required_and_the_engine_sets_aware_ones(self):
        self.assertTrue(TimelineEvent.model_fields["timestamp"].is_required())
        self.assertTrue(FireTruck.model_fields["last_update"].is_required())
        with self.assertRaises(ValidationError):
            TimelineEvent(time="00:00:00", elapsed_seconds=0, event_type=EventType.DISPATCH, message="m")
        engine = SimulationEngine(FireLocation(latitude=34.04, longitude=-118.52))
        asyncio.run(engine.start())
        try:
            self.assertTrue(engine.timeline_events and engine.truck_manager.get_all_trucks())
            stamps = [event.timestamp for event in engine.timeline_events]
            stamps += [truck.last_update for truck in engine.truck_manager.get_all_trucks()]
            self.assertTrue(all(stamp.utcoffset() is not None for stamp in stamps))
        finally:
            asyncio.run(engine.stop())

    def test_a_fire_name_is_bounded_like_the_brokers(self):
        self.assertEqual(len(FireLocation(latitude=34.04, longitude=-118.52, name="n" * 160).name), 160)
        with self.assertRaises(ValidationError):
            FireLocation(latitude=34.04, longitude=-118.52, name="n" * 161)


class EngineTests(unittest.TestCase):
    def test_update_follows_its_clock_and_keeps_every_event_it_returns(self):
        """FTRK-2: the deployed tick, SimulationEngine.update, at 2 s updates through 10 minutes.

        Elapsed time comes from the engine clock, the phase follows the elapsed-time schedule, due units are
        dispatched up to the maximum, every event an update returns is kept in the timeline after the initial
        dispatches, and the fire grows at the moderate rate for the elapsed time.
        """
        self.addCleanup(random.setstate, random.getstate())
        random.seed(42)
        clock = EngineClock()
        returned, phases = [], {}
        with mock.patch("simulator.engine.datetime", clock):
            engine = SimulationEngine(FireLocation(latitude=38.57944, longitude=-121.49085))
            asyncio.run(engine.start())
            dispatched, acres = list(engine.timeline_events), engine.estimated_acres

            async def exercise():
                for elapsed in range(2, 10 * 60 + 1, 2):
                    clock.advance(2)
                    returned.extend(await engine.update())
                    self.assertEqual(engine.elapsed_seconds, elapsed)
                    phases.setdefault(engine.phase.value, elapsed)
            asyncio.run(exercise())
        self.assertEqual(phases, {"INITIAL_DISPATCH": 2, "INITIAL_ATTACK": 180, "BUILDING_RESPONSE": 600})
        self.assertEqual(engine.truck_manager.get_truck_count(), 12)
        kinds = {event.event_type for event in returned}
        self.assertTrue({EventType.TRUCK_ADDED, EventType.ARRIVAL, EventType.RESUPPLY_START} <= kinds, kinds)
        self.assertEqual(engine.timeline_events, dispatched + returned)
        growth = get_settings().fire_growth_moderate_acres_per_hour * 600 / 3600
        self.assertAlmostEqual(engine.estimated_acres, acres + growth)

    def test_late_updates_still_dispatch_every_due_truck_up_to_the_maximum(self):
        async def exercise():
            engine = SimulationEngine(FireLocation(latitude=34.04, longitude=-118.52), max_truck_count=6)
            await engine.start()
            # Updates seldom land on a scheduled second: at 47 s the 45 s rescue unit is due.
            engine.elapsed_seconds = 47
            await engine._check_truck_additions([])
            self.assertIn("RESCUE-5", engine.truck_manager.trucks)
            engine.elapsed_seconds = 200
            await engine._check_truck_additions([])
            self.assertEqual(engine.truck_manager.get_truck_count(), 6)
        asyncio.run(exercise())

    def test_fires_near_the_antimeridian_and_poles_keep_trucks_on_valid_longitudes(self):
        # Five miles east of 179.99 E crosses the antimeridian (the unwrapped value was 180.077).
        self.assertAlmostEqual(move_point(34.0, 179.99, 5*5280, 90)[1], -179.92, places=2)
        for latitude, longitude in ((34.0, 179.99), (89.99, 170.0), (-89.99, -179.99), (0.0, 180.0)):
            engine = SimulationEngine(FireLocation(latitude=latitude, longitude=longitude))
            asyncio.run(engine.start())
            for truck in engine.truck_manager.get_all_trucks():
                self.assertLessEqual(abs(truck.location.longitude), 180)

    def test_seeded_exercise_stages_support_units_and_publishes_consistent_telemetry(self):
        """MODEL-02: a seeded 10-minute exercise (UI defaults, 2 s updates) publishes role-correct, valid telemetry.

        Command and rescue units stage with the pump off and never draw water; the other types reach suppression.
        At every update the water telemetry validates against TruckWater, only suppression units run the pump,
        water alerts name only suppression units below 40%, each published distance matches its published
        position and no staged unit is in the danger zone. Water alerts do fire, engines and brush units start
        their first resupply within 90 s of arrival, and the evidence label names what is not modeled.
        """
        self.maxDiff = None  # a failure shows every support unit's end state
        fire = FireLocation(latitude=38.57944, longitude=-121.49085)
        self.addCleanup(random.setstate, random.getstate())
        random.seed(42)
        engine = SimulationEngine(fire, initial_truck_count=4, max_truck_count=12)
        invalid_water, pump_outside_suppression, stale_distances, staged_in_danger = [], [], [], []
        water_alerts, false_alerts = [], []
        arrived, first_resupply, suppressing = {}, {}, set()

        async def exercise():
            await engine.start()
            for elapsed in range(2, 10 * 60 + 1, 2):
                engine.elapsed_seconds = elapsed
                await engine._check_truck_additions([])
                for truck_id in list(engine.truck_manager.trucks):
                    engine.truck_manager.update_truck(truck_id, elapsed, 2)
                trucks = engine.truck_manager.get_all_trucks()
                for truck in trucks:
                    status = truck.operational.activity_status
                    published = TruckUpdateSimplified.from_fire_truck(truck)
                    if status != ActivityStatus.EN_ROUTE:
                        arrived.setdefault(truck.truck_id, elapsed)
                    if status == ActivityStatus.SUPPRESSION:
                        suppressing.add(truck.truck_id)
                    elif status == ActivityStatus.RESUPPLY:
                        first_resupply.setdefault(truck.truck_id, elapsed)
                    pump = truck.water.pump_status, truck.water.pump_pressure_psi, truck.water.flow_rate_gpm
                    if status != ActivityStatus.SUPPRESSION and pump != (PumpStatus.OFF, 0, 0):
                        pump_outside_suppression.append((elapsed, truck.truck_id, status.value, *pump))
                    try:
                        TruckWater.model_validate(truck.water.model_dump())
                    except ValidationError:
                        invalid_water.append((elapsed, truck.truck_id, truck.water.flow_rate_gpm))
                    gap = abs(published.distance_to_fire_ft - calculate_distance_feet(
                        published.latitude, published.longitude, fire.latitude, fire.longitude))
                    if gap > 1:
                        stale_distances.append((elapsed, truck.truck_id, round(gap)))
                    if status == ActivityStatus.STAGING and published.in_danger_zone:
                        staged_in_danger.append((elapsed, truck.truck_id))
                for text in engine.get_recommendations():
                    if "water" not in text and "resupply" not in text:
                        continue
                    named = [truck for truck in trucks if text.startswith(f"{truck.call_sign} ")]
                    if (len(named) == 1 and named[0].operational.activity_status == ActivityStatus.SUPPRESSION
                            and named[0].water.tank_level_pct < 40):
                        water_alerts.append((elapsed, text))
                    else:
                        false_alerts.append((elapsed, text))
        asyncio.run(exercise())

        trucks = engine.truck_manager.get_all_trucks()
        self.assertEqual({truck.unit_type for truck in trucks}, set(UnitType), "every unit type takes part")
        support = {truck.truck_id for truck in trucks if truck.unit_type in (UnitType.COMMAND, UnitType.RESCUE)}
        with self.subTest("command and rescue units end staged with the pump off, a full tank and no resupply"):
            ended = {truck.truck_id: {"status": truck.operational.activity_status, "pump": truck.water.pump_status,
                                      "flow_gpm": truck.water.flow_rate_gpm,
                                      "pressure_psi": truck.water.pump_pressure_psi,
                                      "tank_pct": truck.water.tank_level_pct,
                                      "deployed_gal": truck.water.total_water_deployed_gal,
                                      "resupply_trips": truck.operational.resupply_count}
                     for truck in trucks if truck.truck_id in support}
            staged = {"status": ActivityStatus.STAGING, "pump": PumpStatus.OFF, "flow_gpm": 0, "pressure_psi": 0,
                      "tank_pct": 100, "deployed_gal": 0, "resupply_trips": 0}
            self.assertEqual(ended, dict.fromkeys(support, staged))
        with self.subTest("engines, tankers and brush units reach suppression"):
            self.assertEqual({truck.truck_id for truck in trucks} - support - suppressing, set())
        with self.subTest("water telemetry validates against TruckWater at every update"):
            flows = sorted({flow for *_, flow in invalid_water})
            self.assertEqual(len(invalid_water), 0, f"failing flows {flows} GPM; first {invalid_water[:3]}")
        with self.subTest("units outside suppression have the pump off at 0 psi and 0 GPM"):
            self.assertEqual(len(pump_outside_suppression), 0, f"first {pump_outside_suppression[:3]}")
        with self.subTest("water alerts fire and name only suppression units below 40%"):
            self.assertEqual(len(false_alerts), 0, f"first {false_alerts[:3]}")
            self.assertGreater(len(water_alerts), 0, "no water alert named a suppression unit below 40%")
        with self.subTest("each published distance is within 1 ft of its published position"):
            worst = max((gap for *_, gap in stale_distances), default=0)
            self.assertEqual(len(stale_distances), 0, f"worst gap {worst} ft; first {stale_distances[:3]}")
        with self.subTest("staged units are never in the danger zone"):
            self.assertEqual(staged_in_danger, [])
        with self.subTest("engines and brush units start their first resupply within 90 s of arrival"):
            delays = {truck.truck_id: first_resupply.get(truck.truck_id, float("inf")) - arrived[truck.truck_id]
                      for truck in trucks if truck.unit_type in (UnitType.ENGINE, UnitType.BRUSH)}
            self.assertLessEqual(max(delays.values()), 90, delays)
        with self.subTest("the evidence label names fixed phases, no crew rotation and illustrative values"):
            limitations = private.snapshot(engine, "stopped", [])["limitations"]
            for clause in ("fixed elapsed-time schedule", "crew rotation is not modeled",
                           "fire growth, weather and safety values are illustrative"):
                self.assertIn(clause, limitations)


if __name__ == "__main__":
    unittest.main()
