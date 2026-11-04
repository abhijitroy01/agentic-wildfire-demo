from backend.traffic_lab import service as traffic_lab_service

"""Exercise the real boto3 number codecs across cloud reads, transactions and outbox dispatch."""
import copy
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError
from traffic_simulator.errors import IntegrationError
from traffic_simulator.models import Scenario
from traffic_simulator.persistence import commit
from traffic_simulator.store import Store

from backend.evacuation import service
from backend.evacuation.contracts import SessionPatch, SessionRequest
from backend.tests.builders import prepared, submission


class FakeDynamo:
    """In-memory DynamoDB table, transaction client and SQS queue behind a cloud-mode Store."""

    def __init__(self):
        self.items = {}
        self.decoder = TypeDeserializer()
        self.table = SimpleNamespace(
            name="test", get_item=self.get_item, query=self.query, update_item=self.update_item,
            scan=Mock(side_effect=AssertionError("Requests must never scan the shared table")))
        self.ddb = SimpleNamespace(transact_write_items=Mock(side_effect=self.transact))
        self.sqs = SimpleNamespace(send_message=Mock())

    def store(self):
        store = Store.__new__(Store)
        store.cloud, store.table, store.ddb, store.sqs = True, self.table, self.ddb, self.sqs
        return store

    def sent(self):
        return [json.loads(call.kwargs["MessageBody"])["id"] for call in self.sqs.send_message.call_args_list]

    def get_item(self, *, Key, ConsistentRead):
        return {"Item": copy.deepcopy(self.items.get(Key["id"]))}

    def query(self, *, ExpressionAttributeValues, Limit, **_):
        owned = [i for i in self.items.values() if i.get("owner") == ExpressionAttributeValues[":o"]]
        return {"Items": copy.deepcopy(sorted(owned, key=lambda i: i["created"], reverse=True)[:Limit])}

    def update_item(self, *, Key, ExpressionAttributeValues, ExpressionAttributeNames=None, **_):
        """Apply the Store write named by the values it binds, never by its expression text; others fail."""
        values, names, refused = ExpressionAttributeValues, ExpressionAttributeNames or {}, ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        match sorted(values):
            case [":one", ":until"]:
                # Store.retain: extend an existing record's expiry, never shorten it.
                item = self.items[Key["id"]]
                if item["expires"] >= values[":until"]:
                    raise refused
                item.update(expires=values[":until"], revision=item.get("revision", 0) + values[":one"])
            case [":expires", ":kind", ":limit", ":one"]:
                # Store.spend: a daily budget counter, incremented only while it is below its limit.
                counter = self.items.setdefault(Key["id"], {"id": Key["id"], "count": 0})
                if counter["count"] >= values[":limit"]:
                    raise refused
                counter.update(kind=values[":kind"], expires=values[":expires"],
                               count=counter["count"] + values[":one"])
            case [":no"]:
                # Store.send_outbox: the job is queued, so its outbox flag and entry are cleared.
                self.items[Key["id"]]["outbox_pending"] = values[":no"]
                self.items[Key["id"]].pop("pending_outbox", None)
            case bound if bound == sorted(f":v{i}" for i in range(len(values))):
                # Store.update: set_values binds each field as #k<i>=:v<i>.
                for i in range(len(values)):
                    self.items[Key["id"]][names[f"#k{i}"]] = values[f":v{i}"]
            case bound:
                raise AssertionError(f"FakeDynamo has no Store write that binds {bound}")

    def transact(self, *, TransactItems):
        # AWS's numeric wire representation and Decimal readback are intentional.
        pending = []
        for item in TransactItems:
            put = item["Put"]
            document = {key: self.decoder.deserialize(value) for key, value in put["Item"].items()}
            previous = self.items.get(document["id"])
            expected = put.get("ExpressionAttributeValues", {}).get(":r")
            if ((expected is None and previous is not None)
                    or (expected is not None and (
                        previous is None or previous["revision"] != self.decoder.deserialize(expected)))):
                raise ClientError({"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems")
            pending.append(document)
        self.items.update({value["id"]: value for value in pending})


def test_cloud_read_modify_write_and_idempotency_use_exact_revisions():
    fake = FakeDynamo()
    store = fake.store()
    request = SessionRequest(request_id=uuid4(), entry_mode="lab")
    session = service.create_session(store, "owner", request)
    session_id = session["session_id"]
    patch = SessionPatch(request_id=uuid4(), expected_revision=1,
                         policy_id="baseline", replay_time_s=1.25)
    updated = service.patch_session(store, "owner", session_id, patch)
    assert updated["revision"] == 2 and type(updated["revision"]) is int
    assert updated["replay_time_s"] == 1.25
    assert fake.items[f"session:{session_id}"]["revision"] == Decimal(2)
    calls = fake.ddb.transact_write_items.call_count
    assert service.patch_session(store, "owner", session_id, patch) == updated
    assert fake.ddb.transact_write_items.call_count == calls
    assert service.create_session(store, "owner", request) == session
    stale = store.get(f"session:{session_id}")
    stale["revision"] = 3
    with pytest.raises(IntegrationError) as conflict:
        commit(store, [(stale, 1)])
    assert conflict.value.code == "REVISION_CONFLICT"
    assert store.get(f"session:{session_id}")["revision"] == 2


def test_requests_dispatch_only_their_own_outbox_entry_and_replays_send_nothing(monkeypatch):
    fake = FakeDynamo()
    store = fake.store()
    monkeypatch.setattr("traffic_simulator.traffic.configured", lambda: True)
    _, _, draft = prepared(store)
    request = submission(draft)
    run = service.submit(store, "alice", request)
    assert service.submit(store, "alice", request) == run
    job = traffic_lab_service.create_research_job(store, "alice", Scenario().model_dump())
    assert fake.sent() == [draft["preparation_id"], run["run_id"], job["id"]]
    fake.table.scan.assert_not_called()
    assert not any(fake.items[i]["outbox_pending"] for i in fake.sent())


def test_failed_dispatch_keeps_the_committed_outbox_for_the_scheduled_dispatcher(monkeypatch):
    fake = FakeDynamo()
    store = fake.store()
    fake.sqs.send_message.side_effect = ClientError({"Error": {"Code": "ServiceUnavailable"}}, "SendMessage")
    job = traffic_lab_service.create_research_job(store, "alice", Scenario().model_dump())
    assert fake.items[job["id"]]["outbox_pending"] is True
    fake.table.scan.assert_not_called()
