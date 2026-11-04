"""Compare-and-swap transactions shared by SQLite and DynamoDB.

Metadata uses the existing table, with namespaced keys and without the job GSI
attributes. Request receipts are committed in the same transaction as mutations.
Every record carries the table's `expires` TTL attribute, so DynamoDB deletes it.
Settings are read at call time: the offline contract exporter imports this module.
"""
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from traffic_simulator.errors import IntegrationError

DAY_SECONDS = 86400


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def dynamo_value(value):
    """Convert a JSON value for DynamoDB, which takes numbers as Decimal: floats keep their decimal text."""
    return json.loads(json.dumps(value), parse_float=Decimal)


def expires_at(now=None):
    """Return the TTL epoch second of a record created now: DEMO_SESSION_DAYS (capacity.demo_session_days) later."""
    from traffic_simulator import settings
    return int(time.time() if now is None else now) + settings.demo_session_days() * DAY_SECONDS


def budget_day(now=None):
    """Return the UTC date that keys today's daily budgets and the epoch second when the next UTC day starts."""
    day = datetime.fromtimestamp(time.time() if now is None else now, UTC).date()
    return day.isoformat(), int(datetime.combine(day + timedelta(days=1), datetime.min.time(), UTC).timestamp())


def commit(store, writes):
    """Each pair is (new document, expected revision or None for insert)."""
    writes = [(dict(document, pending_outbox="pending") if document.get("outbox_pending") else
               {k: v for k, v in document.items() if k != "pending_outbox"}, revision)
              for document, revision in writes]
    if store.cloud:
        serializer = TypeSerializer()
        items = []
        for document, revision in writes:
            document = dynamo_value(document)
            put = {"TableName": store.table.name,
                   "Item": {k: serializer.serialize(v) for k, v in document.items()},
                   "ConditionExpression": "attribute_not_exists(id)" if revision is None else "#r=:r"}
            if revision is not None:
                put.update(ExpressionAttributeNames={"#r": "revision"},
                           ExpressionAttributeValues={":r": serializer.serialize(revision)})
            items.append({"Put": put})
        try:
            store.ddb.transact_write_items(TransactItems=items)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "TransactionCanceledException":
                raise IntegrationError("REVISION_CONFLICT", "State changed; refresh and retry.", 409) from None
            raise
    else:
        with store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for document, revision in writes:
                row = db.execute("SELECT body FROM jobs WHERE id=?", (document["id"],)).fetchone()
                previous = json.loads(row[0]) if row else None
                if (revision is None and previous is not None) or (
                    revision is not None and (previous is None or previous.get("revision") != revision)
                ):
                    raise IntegrationError("REVISION_CONFLICT", "State changed; refresh and retry.", 409)
                db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?)",
                           (document["id"], document.get("owner"), json.dumps(document)))


def record(kind, identifier, owner, **values):
    """Build a namespaced owner record (revision 1) that the table's TTL deletes after the demo-record lifetime."""
    return {"id": f"{kind}:{identifier}", "kind": kind, "subject": owner,
            "revision": 1, "created_at": time.time(), "expires": expires_at(), **values}


def owned(store, kind, identifier, owner):
    """Return the owner's record of this kind, else 404 NOT_FOUND, whether missing or another owner's.

    The log-only reason (missing or other_owner) lets the security event log show cross-owner attempts.
    """
    value = store.get(f"{kind}:{identifier}")
    if not value or value.get("subject") != owner:
        raise IntegrationError("NOT_FOUND", f"{kind.capitalize()} not found.", 404,
                               reason="other_owner" if value else "missing")
    return value


def public(value):
    return {k: v for k, v in value.items()
            if k not in {"subject", "owner", "leaseToken", "leaseUntil", "expires", "outbox_pending",
                         "pending_outbox"}}


def mutate(store, owner, operation, request_id, body, build):
    """Retry only competing transactions. Identical requests return the saved receipt.

    build() runs again after each conflicting commit, so a builder memoizes its provider calls.
    """
    key = f"request:{digest([owner, operation, str(request_id)])}"
    body_hash = digest(body)
    for _ in range(8):
        previous = store.get(key)
        if previous:
            if previous["body_hash"] != body_hash:
                raise IntegrationError("IDEMPOTENCY_CONFLICT", "Request ID was reused with different inputs.", 409)
            return previous["response"]
        response, writes = build()
        receipt = {"id": key, "kind": "request", "subject": owner, "revision": 1,
                   "body_hash": body_hash, "response": response, "created_at": time.time(),
                   "expires": expires_at()}
        try:
            commit(store, [*writes, (receipt, None)])
            return response
        except IntegrationError as exc:
            if exc.code != "REVISION_CONFLICT":
                raise
    raise IntegrationError("REVISION_CONFLICT", "Concurrent updates; retry with the same request ID.", 409,
                           retryable=True)
