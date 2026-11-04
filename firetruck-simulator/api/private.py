"""Private, authenticated firetruck service with durable evidence.

Only the application broker can reach this service. Run IDs are assigned by the
broker, which also sends the owner digest that binds each run to one visitor:
reads and stops for any other owner get 404. Any replica can read/stop a run;
the creating replica owns its update loop while its lease lasts. A lost task
leaves an explicitly interrupted run, never a fake completion.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from uuid import UUID, uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from config import get_settings
from config.settings import SERVICE_KEY_MIN_LENGTH
from fastapi import Depends, FastAPI, Header, HTTPException
from simulator.engine import SimulationEngine

from api.models.simulation import StartSimulationRequest
from api.models.truck import TruckUpdateSimplified

logger = logging.getLogger(__name__)
# A protocol timing, not an operational setting: how long a replica owns a run without publishing.
LEASE_SECONDS = 60
# The broker's owner digest (SHA-256 hex of the visitor's subject) binds each run to one visitor.
OWNER = re.compile(r"[0-9a-f]{64}")
# Statuses after which a run publishes nothing more; their snapshot keeps every timeline event.
FINAL = ("stopped", "interrupted")
tasks = {}
engines = {}
starting = {}


class LeaseLost(Exception):
    """This replica no longer owns the run: another attempt took it over or its lease expired."""


def authenticate(authorization: str = Header(default="")):
    """Require the broker's bearer service key; comparing bytes makes any other header a 401, never a 500."""
    configured = get_settings().service_key
    key = configured.get_secret_value() if configured else ""
    if len(key) < SERVICE_KEY_MIN_LENGTH or not hmac.compare_digest(authorization.encode(), f"Bearer {key}".encode()):
        raise HTTPException(401, "Service authentication required")


def run_owner(x_firetruck_owner: str = Header(default="")) -> str:
    """Return the broker's owner digest (X-Firetruck-Owner header) that every run route requires."""
    if not OWNER.fullmatch(x_firetruck_owner):
        raise HTTPException(400, "Run owner required")
    return x_firetruck_owner


def same_owner(item, owner) -> bool:
    """Return whether a run row was created for this owner digest (constant-time comparison)."""
    return hmac.compare_digest(str(item.get("owner", "")).encode(), owner.encode())


class EvidenceStore:
    """Run rows (attempt, lease, owner, stop flag) in DynamoDB and evidence snapshots in S3."""

    def __init__(self, table=None, s3=None, bucket=None):
        """Use the given table, S3 client and bucket, else the ones the settings name (ECS requires them)."""
        if table is None:
            settings = get_settings()
            config = Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2})
            table = boto3.resource("dynamodb", region_name=settings.aws_region,
                                   config=config).Table(settings.evidence_table)
            s3 = boto3.client("s3", region_name=settings.aws_region, config=config)
            bucket = settings.evidence_bucket
        self.table, self.s3, self.bucket = table, s3, bucket

    def get(self, run_id):
        """Read a run row with a consistent read; 404 when it does not exist."""
        item = self.table.get_item(Key={"id": run_id}, ConsistentRead=True).get("Item")
        if not item:
            raise HTTPException(404, "Simulation not found")
        return item

    def create(self, run_id, configuration, attempt, owner):
        """Insert a run row bound to its owner; False when that owner already created it with these inputs."""
        item = {"id": run_id, "configuration": json.dumps(configuration, sort_keys=True), "owner": owner,
                "attempt": attempt, "status": "starting", "stop": False,
                "started": int(time.time()), "lease_until": int(time.time())+LEASE_SECONDS}
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(id)")
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            previous = self.get(run_id)
            if previous["configuration"] != item["configuration"] or not same_owner(previous, owner):
                raise HTTPException(409, "Run ID was already used with different inputs") from None
            return False
        return True

    def existing(self, run_id, configuration, owner):
        """Return an identical owned start's receipt, or None when this ID is unused."""
        item = self.table.get_item(Key={"id": run_id}, ConsistentRead=True).get("Item")
        if item is None:
            return None
        if item["configuration"] != json.dumps(configuration, sort_keys=True) or not same_owner(item, owner):
            raise HTTPException(409, "Run ID was already used with different inputs")
        return {"session_id": run_id, "status": item["status"]}

    def publish(self, run_id, attempt, snapshot):
        """Store a snapshot and point the run at it while this attempt holds an unexpired lease.

        The snapshot it replaces is then deleted. Raises LeaseLost, changing nothing, when another
        attempt owns the run or the lease expired (readers already show it interrupted).
        """
        payload = json.dumps(snapshot, allow_nan=False, separators=(",", ":")).encode()
        key = f"firetruck/{run_id}/{hashlib.sha256(payload).hexdigest()}.json"
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=payload, ContentType="application/json")
        now = int(time.time())
        try:
            replaced = self.table.update_item(
                Key={"id": run_id}, ConditionExpression="attempt=:attempt AND lease_until>=:now",
                UpdateExpression="SET snapshot_key=:key, #s=:status, lease_until=:lease",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":attempt": attempt, ":now": now, ":key": key,
                                           ":status": snapshot["status"], ":lease": now+LEASE_SECONDS},
                ReturnValues="UPDATED_OLD").get("Attributes", {}).get("snapshot_key")
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # Remove the object just written, unless the run already points at identical content.
            if (self.table.get_item(Key={"id": run_id}, ConsistentRead=True).get("Item") or {}).get(
                    "snapshot_key") != key:
                self.discard(run_id, key)
            raise LeaseLost(run_id) from None
        if replaced and replaced != key:
            self.discard(run_id, replaced)

    def discard(self, run_id, key):
        """Delete a snapshot object no run points at; on failure the bucket's lifecycle rule removes it."""
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        except ClientError:
            logger.warning("Could not delete an unreferenced snapshot of firetruck run %s", run_id)

    def fail(self, run_id, attempt):
        """Mark a run that could not start as failed, so it never stays 'starting'."""
        self.table.update_item(Key={"id": run_id}, ConditionExpression="attempt=:attempt",
                               UpdateExpression="SET #s=:failed", ExpressionAttributeNames={"#s": "status"},
                               ExpressionAttributeValues={":attempt": attempt, ":failed": "failed"})

    def read(self, item):
        """Return a run's latest snapshot; an expired lease on a live run reads as interrupted.

        publish() deletes the snapshot it replaces, so when an update lands between the row read and the snapshot
        read, the run's row is read again, once, and its newer snapshot is returned.
        """
        if item["status"] == "failed":
            raise HTTPException(410, "Simulation could not start")
        if not item.get("snapshot_key"):
            if item["lease_until"] < time.time():
                raise HTTPException(410, "Simulation interrupted before its first snapshot")
            raise HTTPException(409, "Simulation is starting")
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=item["snapshot_key"])
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchKey":
                raise
            current = self.get(item["id"])
            if current.get("snapshot_key") == item["snapshot_key"]:
                raise
            item = current
            response = self.s3.get_object(Bucket=self.bucket, Key=item["snapshot_key"])
        try:
            snapshot = json.loads(response["Body"].read())
        finally:
            response["Body"].close()
        if item["status"] in ("starting", "running") and item["lease_until"] < time.time():
            snapshot["status"] = "interrupted"
            snapshot["interruption"] = "Service stopped updating; last saved evidence is shown."
        snapshot["stop_requested"] = bool(item.get("stop"))
        return snapshot

    def stop(self, run_id):
        """Ask the replica that owns the run to stop it at its next update."""
        self.table.update_item(Key={"id": run_id}, ConditionExpression="attribute_exists(id)",
                               UpdateExpression="SET #stop=:yes",
                               ExpressionAttributeNames={"#stop": "stop"},
                               ExpressionAttributeValues={":yes": True})


store = None


def evidence():
    """Return the process's evidence store, created on first use."""
    global store
    if store is None:
        store = EvidenceStore()
    return store


def snapshot(engine, status, phases, final_stats=None):
    """Build one evidence snapshot; a running one keeps the latest FIRETRUCK_EVIDENCE_EVENTS_MAX events."""
    events = engine.timeline_events
    if status not in FINAL:
        events = events[-get_settings().firetruck_evidence_events_max:]
    return {
        "session_id": engine.session_id, "status": status,
        "captured_at": datetime.now(UTC).isoformat(),
        "incident_elapsed_seconds": engine.elapsed_seconds, "phase": engine.phase.value,
        "phase_history": phases,
        "trucks": [TruckUpdateSimplified.from_fire_truck(t).model_dump(mode="json")
                   for t in engine.truck_manager.get_all_trucks()],
        "events": [event.model_dump(mode="json") for event in events],
        # Earlier events this snapshot does not carry; the final snapshot of a finished run omits none.
        "omitted_events": len(engine.timeline_events) - len(events),
        "summary": engine.get_simulation_summary().model_dump(mode="json"),
        "final_stats": final_stats,
        "source_mode": "synthetic_firetruck_simulation",
        "limitations": ("Synthetic suppression activity and resource telemetry. Phases follow a fixed elapsed-time "
                        "schedule; crew rotation is not modeled; fire growth, weather and safety values are "
                        "illustrative. Not validated containment or safety."),
    }


def publish_snapshot(run_id, attempt, engine, status, phases, final_stats=None):
    """Build and publish one snapshot; run through asyncio.to_thread so serializing stays off the loop."""
    evidence().publish(run_id, attempt, snapshot(engine, status, list(phases), final_stats))


async def interrupted(engine, attempt, phases):
    """Stop the engine and save a final interrupted snapshot, best effort (an expired lease reads alike)."""
    try:
        await engine.stop()
        await asyncio.to_thread(publish_snapshot, engine.session_id, attempt, engine, "interrupted", phases)
    except Exception:  # noqa: BLE001 - persistence may be what failed; readers fall back to the lease
        logger.warning("Could not save the interrupted snapshot of firetruck run %s", engine.session_id)


async def simulate(engine, attempt):
    """Advance a run and publish evidence each update until it stops, hits its lifetime or loses its lease."""
    run_id = engine.session_id
    # FIRETRUCK_RUN_LIFETIME_MINUTES (deployment/settings.toml): a run this old is stopped with its final evidence.
    lifetime_seconds = get_settings().firetruck_run_lifetime_minutes * 60
    phases = [{"phase": engine.phase.value, "elapsed_seconds": 0}]
    final_stats = None
    status = "running"
    try:
        await asyncio.to_thread(publish_snapshot, run_id, attempt, engine, status, phases)
        while engine.is_running:
            await asyncio.sleep(engine.update_interval)
            control = await asyncio.to_thread(evidence().get, run_id)
            if control["attempt"] != attempt:
                return
            if control.get("stop") or time.time()-int(control["started"]) >= lifetime_seconds:
                final_stats = await engine.stop()
                status = "stopped"
            else:
                await engine.update()
            if phases[-1]["phase"] != engine.phase.value:
                phases.append({"phase": engine.phase.value, "elapsed_seconds": engine.elapsed_seconds})
            await asyncio.to_thread(publish_snapshot, run_id, attempt, engine, status, phases, final_stats)
    except LeaseLost:
        logger.info("Firetruck run %s is no longer owned by this replica", run_id)
        await engine.stop()
    except asyncio.CancelledError:
        await interrupted(engine, attempt, phases)
        raise
    except Exception:  # Any failure ends the run as interrupted, never as completed.
        logger.exception("Firetruck run %s stopped updating", run_id)
        await interrupted(engine, attempt, phases)
    finally:
        tasks.pop(run_id, None)
        engines.pop(run_id, None)


@asynccontextmanager
async def lifespan(_):
    """Validate settings and, on ECS, build the evidence store at startup; on shutdown stop every run.

    A task missing its table, bucket, Region or service key fails here, before /health answers, so the ECS
    deployment circuit breaker rolls it back. Cancelled runs each save interrupted evidence.
    """
    if get_settings().environment == "ecs":
        evidence()
    yield
    pending = list(tasks.values())
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health():
    """Report that the service process is up (container and load-balancer health checks)."""
    return {"status": "ok"}


@app.put("/internal/runs/{run_id}", dependencies=[Depends(authenticate)])
async def start(run_id: UUID, body: StartSimulationRequest, owner: str = Depends(run_owner)):
    """Start a run once for its owner; repeating the same request returns the run's status."""
    if body.initial_truck_count > body.max_truck_count:
        raise HTTPException(422, "Initial truck count exceeds maximum")
    run_id, attempt = str(run_id), str(uuid4())
    configuration = body.model_dump(mode="json")
    previous = await asyncio.to_thread(evidence().existing, run_id, configuration, owner)
    if previous:
        return previous
    # A duplicate can arrive before the first create reaches DynamoDB.
    if pending := starting.get(run_id):
        await pending.wait()
        previous = await asyncio.to_thread(evidence().existing, run_id, configuration, owner)
        if previous:
            return previous
        raise HTTPException(503, "Simulation start was interrupted; retry")
    # FIRETRUCK_RUNS_PER_TASK (deployment/settings.toml): runs this task simulates at once.
    if len(tasks) + len(starting) >= get_settings().firetruck_runs_per_task:
        raise HTTPException(429, "Service capacity reached")
    # Reserve before yielding. Both pending starts and running tasks consume a slot.
    pending = starting[run_id] = asyncio.Event()
    engine = None
    try:
        created = await asyncio.to_thread(evidence().create, run_id, configuration, attempt, owner)
        if not created:
            return await asyncio.to_thread(evidence().existing, run_id, configuration, owner)
        engine = SimulationEngine(**body.model_dump(exclude={"fire_location"}),
                                  fire_location=body.fire_location)
        engine.session_id = run_id
        await engine.start()
        # Commit initial evidence before acknowledging creation.
        phases = [{"phase": engine.phase.value, "elapsed_seconds": 0}]
        await asyncio.to_thread(publish_snapshot, run_id, attempt, engine, "running", phases)
        engines[run_id] = engine
        tasks[run_id] = asyncio.create_task(simulate(engine, attempt))
    except HTTPException:
        raise
    except asyncio.CancelledError:
        if engine is not None:
            await engine.stop()
        with suppress(Exception):
            await asyncio.to_thread(evidence().fail, run_id, attempt)
        raise
    except Exception:  # A run that cannot start is recorded as failed, never left 'starting'.
        logger.exception("Firetruck run %s could not start", run_id)
        if engine is not None:
            await engine.stop()
        with suppress(Exception):
            await asyncio.to_thread(evidence().fail, run_id, attempt)
        raise HTTPException(500, "Simulation could not start") from None
    finally:
        starting.pop(run_id, None)
        pending.set()
    return {"session_id": run_id, "status": "running"}


@app.get("/internal/runs/{run_id}", dependencies=[Depends(authenticate)])
async def get(run_id: UUID, owner: str = Depends(run_owner)):
    """Return a run's latest evidence to its owner; any other owner gets 404."""
    item = await asyncio.to_thread(evidence().get, str(run_id))
    if not same_owner(item, owner):
        raise HTTPException(404, "Simulation not found")
    return await asyncio.to_thread(evidence().read, item)


@app.delete("/internal/runs/{run_id}", dependencies=[Depends(authenticate)])
async def stop(run_id: UUID, owner: str = Depends(run_owner)):
    """Ask a live run to stop for its owner; any other owner gets 404."""
    item = await asyncio.to_thread(evidence().get, str(run_id))
    if not same_owner(item, owner):
        raise HTTPException(404, "Simulation not found")
    if item["status"] in ("starting", "running") and item["lease_until"] >= time.time():
        await asyncio.to_thread(evidence().stop, str(run_id))
        return {"session_id": str(run_id), "status": "stopping"}
    return {"session_id": str(run_id), "status": "interrupted" if
            item["status"] in ("starting", "running") else item["status"]}
