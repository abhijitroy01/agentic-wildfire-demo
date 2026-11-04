"""Local SQLite and AWS DynamoDB/S3 share the same job ownership contract.

Each method keeps its local and cloud branch side by side, so the two can be compared: every conditional
DynamoDB write has a local twin that applies the same rule inside one SQLite write transaction.
"""
import gzip
import json
import sqlite3
import time
import uuid
from decimal import Decimal
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from traffic_simulator import settings
from traffic_simulator.persistence import DAY_SECONDS, budget_day, digest, dynamo_value

# A worker's job lease in seconds; its heartbeat renews the lease and the run queue message's visibility every 30 s.
LEASE_SECONDS = 180


def plain(value):
    """Convert DynamoDB Decimal values to int or float, keeping integers exact."""
    # DynamoDB returns every number as Decimal, including integer revisions and
    # counters. Preserve integers exactly for conditional writes and API types.
    def encode(item):
        if isinstance(item, Decimal):
            return int(item) if item == item.to_integral_value() else float(item)
        raise TypeError(f"Unsupported stored value: {type(item).__name__}")
    return json.loads(json.dumps(value, default=encode, allow_nan=False))


def set_values(values):
    """Return the DynamoDB SET expression, attribute names and values that write `values` (#k<i>=:v<i>, in order)."""
    return ("SET " + ", ".join(f"#k{i}=:v{i}" for i in range(len(values))),
            {f"#k{i}": name for i, name in enumerate(values)},
            {f":v{i}": dynamo_value(value) for i, value in enumerate(values.values())})


class Store:
    def __init__(self, root: Path | None = None):
        self.root = root or settings.DATA
        self.root.mkdir(parents=True, exist_ok=True)
        self.cloud = settings.CLOUD and root is None
        if self.cloud:
            self.table = boto3.resource("dynamodb", region_name=settings.REGION).Table(settings.TABLE)
            self.ddb = boto3.client("dynamodb", region_name=settings.REGION)
            self.s3 = boto3.client("s3", region_name=settings.REGION)
            self.sqs = boto3.client("sqs", region_name=settings.REGION)
        else:
            with self.db() as db:
                db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner TEXT, body TEXT)")

    def db(self):
        """Open the local SQLite job database in WAL mode."""
        con = sqlite3.connect(self.root / "jobs.sqlite", timeout=20)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def conditional_update(self, **update):
        """Run one conditional DynamoDB UpdateItem: False when its condition fails; any other error raises."""
        try:
            self.table.update_item(**update)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def local_update(self, job_id, change):
        """Apply change(record) to one local record in a single write transaction; return whether it was written.

        change gets the stored record (None when there is none), edits it in place and returns True to write it or
        False to leave it unchanged.
        """
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM jobs WHERE id=?", (job_id,)).fetchone()
            record = json.loads(row[0]) if row else None
            if not change(record):
                return False
            db.execute("UPDATE jobs SET body=? WHERE id=?", (json.dumps(record), job_id))
            return True

    def get(self, job_id):
        """Read one job record (a consistent read in DynamoDB), or None when it does not exist."""
        if self.cloud:
            return plain(self.table.get_item(Key={"id": job_id}, ConsistentRead=True).get("Item"))
        with self.db() as db:
            row = db.execute("SELECT body FROM jobs WHERE id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, owner):
        """List an owner's 30 most recent job records, newest first."""
        if self.cloud:
            response = self.table.query(
                IndexName="owner-created", KeyConditionExpression="#o=:o",
                ExpressionAttributeNames={"#o": "owner"},
                ExpressionAttributeValues={":o": owner}, ScanIndexForward=False, Limit=30)
            return [j for j in plain(response["Items"]) if "status" in j]
        with self.db() as db:
            rows = db.execute("SELECT body FROM jobs WHERE owner=?", (owner,)).fetchall()
        return sorted((j for r in rows if "status" in (j := json.loads(r[0]))),
                      key=lambda j: j["created"], reverse=True)[:30]

    def fenced_update(self, job_id, token, **values):
        """All worker writes, including errors and progress, are fenced by the lease."""
        values["updated"] = time.time()
        if self.cloud:
            expression, names, assigned = set_values(values)
            return self.conditional_update(
                Key={"id": job_id}, UpdateExpression=expression,
                ConditionExpression="leaseToken=:token AND leaseUntil>=:now", ExpressionAttributeNames=names,
                ExpressionAttributeValues={**assigned, ":token": token, ":now": int(time.time())})

        def fenced(job):
            if not job or job.get("leaseToken") != token or job.get("leaseUntil", 0) < time.time():
                return False
            job.update(values)
            return True
        return self.local_update(job_id, fenced)

    def update(self, job_id, **values):
        """Set fields on a job record and refresh its updated time."""
        values["updated"] = time.time()
        if self.cloud:
            expression, names, assigned = set_values(values)
            self.table.update_item(Key={"id": job_id}, UpdateExpression=expression,
                                   ExpressionAttributeNames=names, ExpressionAttributeValues=assigned)
            return

        def assign(job):
            if job is None:
                raise KeyError(job_id)
            job.update(values)
            return True
        self.local_update(job_id, assign)

    def cancel_run(self, job_id):
        """Cancellation cannot overwrite a concurrent completion."""
        if self.cloud:
            self.conditional_update(
                Key={"id": job_id}, UpdateExpression="SET cancel=:yes, #s=:c, phase=:p",
                ConditionExpression="#s IN (:q,:r,:c)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":yes": True, ":q": "queued", ":r": "running",
                                           ":c": "cancelling", ":p": "Cancellation requested"})
        else:
            def cancel(job):
                if not job or job.get("status") not in ("queued", "running", "cancelling"):
                    return False
                job.update(cancel=True, status="cancelling", phase="Cancellation requested")
                return True
            self.local_update(job_id, cancel)
        return self.get(job_id)

    def acknowledge_cancel(self, job_id):
        """Only acknowledge when no active worker can still execute the run."""
        current = self.get(job_id)
        if not current or not current.get("cancel") or current.get("leaseUntil", 0) >= time.time():
            return False
        if self.cloud:
            return self.conditional_update(
                Key={"id": job_id}, UpdateExpression="SET #s=:done, phase=:p",
                ConditionExpression="cancel=:yes AND #s=:c AND (attribute_not_exists(leaseUntil) OR leaseUntil<:now)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":yes": True, ":c": "cancelling", ":done": "cancelled",
                                           ":p": "Cancelled", ":now": int(time.time())})

        def acknowledge(job):
            if not job or not job.get("cancel") or job.get("leaseUntil", 0) >= time.time():
                return False
            job.update(status="cancelled", phase="Cancelled")
            return True
        return self.local_update(job_id, acknowledge)

    def retain(self, identifier, until):
        """Extend TTL monotonically and invalidate older compare-and-swap snapshots."""
        if self.cloud:
            extended = self.conditional_update(
                Key={"id": str(identifier)}, UpdateExpression="SET expires=:until ADD #r :one",
                ConditionExpression="attribute_exists(id) AND (attribute_not_exists(expires) OR expires<:until)",
                ExpressionAttributeNames={"#r": "revision"},
                ExpressionAttributeValues={":until": until, ":one": 1})
            if not extended and not self.get(str(identifier)):
                raise FileNotFoundError("Referenced metadata expired.")
            return

        def extend(value):
            if value is None:
                raise FileNotFoundError("Referenced metadata expired.")
            if value.get("expires", 0) >= until:
                return False
            value.update(expires=until, revision=value.get("revision", 0) + 1)
            return True
        self.local_update(str(identifier), extend)

    def dispatch_outbox(self, job_id):
        """Send one committed job's pending outbox entry to SQS, by key; never scans the shared table.

        The table stream and the scheduled outbox dispatcher Lambda (deployment/infra/lib/outbox_dispatcher.py)
        deliver the same entry and repair missed ones; leases make delivery idempotent, and a replayed request
        whose job is no longer pending sends nothing.
        """
        if self.cloud and (self.get(job_id) or {}).get("outbox_pending"):
            self.send_outbox(job_id)

    def send_outbox(self, job_id):
        """Enqueue one job ID for the worker and clear its outbox flag."""
        job = self.get(job_id)
        if job and job.get("kind") == "fire_risk_prepare":
            import os
            queue = os.environ["FIRE_RISK_QUEUE_URL"]
        else:
            queue = settings.QUEUE
        self.sqs.send_message(QueueUrl=queue, MessageBody=json.dumps({"id": job_id}))
        self.table.update_item(Key={"id": job_id}, UpdateExpression="SET outbox_pending=:no REMOVE pending_outbox",
                               ExpressionAttributeValues={":no": False})

    def put_record(self, record):
        """Write a namespaced metadata record whole, replacing any earlier one with its id; it never lists as a job.

        The record carries no owner or created attribute, so neither the owner index nor pending() includes it.
        """
        if self.cloud:
            self.table.put_item(Item=dynamo_value(record))
            return
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?)", (record["id"], None, json.dumps(record)))

    def spend(self, budget, limit, *, scope="", now=None):
        """Count one use of a shared daily budget (per UTC day) in the jobs table; False once `limit` are counted.

        One conditional update, so concurrent API processes and workers never count past the limit. The key holds
        only the budget name, the date and a digest of the scope (for example a viewer IP); the counter expires
        through the table's TTL one day after its date ends. A limit of 0 refuses every use.
        """
        if limit <= 0:
            return False
        day, next_day = budget_day(now)
        key = f"budget:{budget}:{day}" + (f":{digest(scope)}" if scope else "")
        expires = next_day + DAY_SECONDS
        if self.cloud:
            return self.conditional_update(
                Key={"id": key}, UpdateExpression="SET #k=:kind, #e=:expires ADD #c :one",
                ConditionExpression="attribute_not_exists(#c) OR #c < :limit",
                ExpressionAttributeNames={"#k": "kind", "#e": "expires", "#c": "count"},
                ExpressionAttributeValues={":kind": "budget", ":expires": expires, ":one": 1, ":limit": limit})
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM jobs WHERE id=?", (key,)).fetchone()
            counter = json.loads(row[0]) if row else {"id": key, "kind": "budget", "count": 0, "expires": expires}
            if counter["count"] >= limit:
                return False
            counter["count"] += 1
            db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?)", (key, None, json.dumps(counter)))
            return True

    def claim(self, job_id, token):
        """Conditional lease; duplicate deliveries cannot run concurrently."""
        now = int(time.time())
        if self.cloud:
            return self.conditional_update(
                Key={"id": job_id},
                UpdateExpression="SET #s=:r, leaseUntil=:until, leaseToken=:token",
                ConditionExpression="(#s=:q OR (#s=:r AND leaseUntil < :now)) AND cancel=:no",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":r": "running", ":q": "queued", ":until": now + LEASE_SECONDS,
                                           ":now": now, ":token": token, ":no": False})

        def lease(job):
            if not job:
                return False
            eligible = job["status"] == "queued" or (
                job["status"] == "running" and job.get("leaseUntil", 0) < now)
            if not eligible or job["cancel"]:
                return False
            job.update(status="running", leaseUntil=now + LEASE_SECONDS, leaseToken=token)
            return True
        return self.local_update(job_id, lease)

    def heartbeat(self, job_id, token):
        """Extend the worker's job lease by LEASE_SECONDS (180 s), raising if another attempt took it."""
        if not self.fenced_update(job_id, token, leaseUntil=int(time.time()) + LEASE_SECONDS):
            raise RuntimeError("Worker lease lost")

    def finish(self, job_id, token, summaries, *, artifact_attempt=None, phase="Comparison ready", timings=None):
        """Publish completion, with the job's phase timings map, only while still owning a non-cancelled run."""
        values = {"status": "completed", "progress": 1, "phase": phase,
                  "summaries": summaries, "updated": time.time()}
        if artifact_attempt:
            values["artifact_attempt"] = artifact_attempt
        if timings is not None:
            values["timings"] = timings
        if self.cloud:
            expression, names, assigned = set_values(values)
            # The condition names the stored status through its own placeholder, wherever status sits in values.
            status = next(placeholder for placeholder, name in names.items() if name == "status")
            return self.conditional_update(
                Key={"id": job_id}, UpdateExpression=expression,
                ConditionExpression=f"leaseToken=:token AND cancel=:no AND {status}=:running AND leaseUntil>=:now",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues={**assigned, ":token": token, ":no": False, ":running": "running",
                                           ":now": int(time.time())})

        def complete(job):
            if (not job or job.get("leaseToken") != token or job.get("cancel") or job.get("status") != "running"
                    or job.get("leaseUntil", 0) < time.time()):
                return False
            job.update(values)
            return True
        return self.local_update(job_id, complete)

    def pending(self):
        """List local jobs still queued, running or cancelling, oldest first."""
        with self.db() as db:
            rows = db.execute("SELECT body FROM jobs ORDER BY rowid").fetchall()
        return [j for (body,) in rows if (j := json.loads(body)).get("owner") and j.get("status") in
                ("queued", "running", "cancelling")]

    def artifact(self, job_id, name, value=None, *, attempt=None):
        """Read or write a named job artifact (result, sources, network, manifest or bundle)."""
        # Both values originate in server code, never arbitrary user paths.
        uuid.UUID(job_id)
        if name not in {"result", "sources", "network", "manifest", "bundle", "coverage"}:
            raise ValueError("Unknown artifact")
        if value is None and attempt is None:
            attempt = (self.get(job_id) or {}).get("artifact_attempt")
        if attempt:
            uuid.UUID(attempt)
        prefix = f"attempts/{attempt}/" if attempt else ""
        key = f"runs/{job_id}/{prefix}{name}.json.gz"
        if value is not None:
            raw = gzip.compress(json.dumps(value, separators=(",", ":"), allow_nan=False).encode())
            if self.cloud:
                self.s3.put_object(Bucket=settings.BUCKET, Key=key, Body=raw,
                                   ContentType="application/json", ContentEncoding="gzip")
            else:
                path = self.root / key
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix(".tmp")
                temp.write_bytes(raw)
                temp.replace(path)
            return
        if self.cloud:
            try:
                response = self.s3.get_object(Bucket=settings.BUCKET, Key=key)
            except ClientError as exc:
                if exc.response["Error"]["Code"] in {"NoSuchKey", "NotFound", "404"}:
                    raise FileNotFoundError("Saved artifact is unavailable") from None
                raise
            try:
                raw = response["Body"].read()
            finally:
                response["Body"].close()
        else:
            raw = (self.root / key).read_bytes()
        return json.loads(gzip.decompress(raw))
