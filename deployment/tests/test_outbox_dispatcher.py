"""Outbox dispatcher: partial stream failures and pending-index repair, which never scans the table."""
import importlib.util
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

DISPATCHER = Path(__file__).resolve().parents[1] / "infra/lib/outbox_dispatcher.py"
TABLE_NAME = "fixture-jobs"
QUEUE_URL = "https://sqs.us-west-2.amazonaws.com/111111111111/fixture-runs"
RISK_QUEUE_URL = "https://sqs.us-west-2.amazonaws.com/111111111111/fixture-risk"


class FakeTable:
    """In-memory Jobs table with consistent reads, outbox-clearing updates and the pending-index query."""

    def __init__(self, jobs, pages=()):
        self.jobs = jobs
        self.pages = list(pages)
        self.reads = []
        self.queries = []
        self.updates = []

    def get_item(self, Key, ConsistentRead):
        assert ConsistentRead is True
        self.reads.append(Key["id"])
        job = self.jobs.get(Key["id"])
        return {"Item": dict(job)} if job else {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues):
        assert UpdateExpression == "SET outbox_pending=:no REMOVE pending_outbox"
        assert ExpressionAttributeValues == {":no": False}
        self.updates.append(Key["id"])
        self.jobs[Key["id"]]["outbox_pending"] = False
        self.jobs[Key["id"]].pop("pending_outbox", None)

    def scan(self, **request):
        raise AssertionError("steady-state repair never scans the table")

    def query(self, **request):
        assert request["IndexName"] == "pending-outbox"
        self.queries.append(request)
        return self.pages[len(self.queries) - 1]


class FakeQueue:
    """SQS stub that records sent job IDs and throttles the chosen ones."""

    def __init__(self, failing=()):
        self.failing = set(failing)
        self.sent = []
        self.destinations = []

    def send_message(self, QueueUrl, MessageBody):
        assert QueueUrl in (QUEUE_URL, RISK_QUEUE_URL)
        job_id = json.loads(MessageBody)["id"]
        if job_id in self.failing:
            raise ClientError({"Error": {"Code": "Throttling", "Message": "fixture"}}, "SendMessage")
        self.sent.append(job_id)
        self.destinations.append((job_id, QueueUrl))
        return {"MessageId": "fixture"}


def stream_record(job_id, sequence):
    """Build a DynamoDB stream record for one job, as Lambda delivers it."""
    return {"eventName": "MODIFY", "dynamodb": {"Keys": {"id": {"S": job_id}}, "SequenceNumber": sequence}}


@pytest.fixture
def invoke(monkeypatch):
    """Load the Lambda source by file path and run handler() against in-memory fakes, never AWS."""
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("JOBS_TABLE", TABLE_NAME)
    monkeypatch.setenv("QUEUE_URL", QUEUE_URL)
    monkeypatch.setenv("FIRE_RISK_QUEUE_URL", RISK_QUEUE_URL)
    root = logging.getLogger()
    level = root.level
    spec = importlib.util.spec_from_file_location("outbox_dispatcher_under_test", DISPATCHER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def run(event, table, queue):
        def resource(name):
            assert name == "dynamodb"
            return SimpleNamespace(Table=lambda table_name: table if table_name == TABLE_NAME else None)

        def client(name):
            assert name == "sqs"
            return queue

        monkeypatch.setattr(module, "boto3", SimpleNamespace(resource=resource, client=client))
        return module.handler(event, None)

    yield run
    root.setLevel(level)  # the Lambda module sets the root logger level at import


def test_risk_preparation_outbox_uses_its_dedicated_queue(invoke):
    table = FakeTable({
        "risk": {"id": "risk", "kind": "fire_risk_prepare", "outbox_pending": True},
        "roads": {"id": "roads", "kind": "road_discovery", "outbox_pending": True},
    })
    queue = FakeQueue()
    result = invoke({"Records": [stream_record("risk", "1"), stream_record("roads", "2")]}, table, queue)
    assert result == {"batchItemFailures": []}
    assert queue.destinations == [("risk", RISK_QUEUE_URL), ("roads", QUEUE_URL)]
    assert table.updates == ["risk", "roads"]


def test_stream_batch_reports_only_the_failed_record_and_clears_dispatched_outboxes(invoke, caplog):
    table = FakeTable({"job-a": {"id": "job-a", "outbox_pending": True},
                       "job-b": {"id": "job-b", "outbox_pending": True},
                       "job-c": {"id": "job-c", "outbox_pending": False}})
    queue = FakeQueue(failing={"job-b"})
    records = [stream_record("job-a", "100"), stream_record("job-b", "200"), stream_record("job-c", "300")]
    result = invoke({"Records": records}, table, queue)
    assert result == {"batchItemFailures": [{"itemIdentifier": "200"}]}
    assert queue.sent == ["job-a"]
    assert table.updates == ["job-a"]
    assert table.jobs["job-b"]["outbox_pending"] is True, "A failed send stays pending for retry and repair."
    assert "Outbox dispatch failed" in caplog.text
    assert "job-b" not in caplog.text, "Failure logs carry no record payload."


def test_malformed_stream_records_are_reported_or_left_to_repair_without_aborting_the_batch(invoke, caplog):
    table = FakeTable({"job-a": {"id": "job-a", "outbox_pending": True}})
    queue = FakeQueue()
    records = [
        {"dynamodb": {"SequenceNumber": "100"}},  # no Keys: retried by its sequence number
        {"eventName": "INSERT"},                   # no stream payload, so no sequence number
        {"dynamodb": None},                        # wrong shape
        stream_record("job-a", "400"),
    ]
    with caplog.at_level(logging.ERROR):
        result = invoke({"Records": records}, table, queue)
    assert result == {"batchItemFailures": [{"itemIdentifier": "100"}]}
    assert queue.sent == ["job-a"]
    assert caplog.text.count("leaving it to scheduled repair") == 2


def test_scheduled_repair_queries_pending_index_and_skips_stale_entries(invoke):
    table = FakeTable(
        {"job-a": {"id": "job-a", "outbox_pending": True}, "job-b": {"id": "job-b", "outbox_pending": True},
         "job-c": {"id": "job-c", "outbox_pending": False}},
        pages=[{"Items": [{"id": "job-a"}], "LastEvaluatedKey": {"id": "job-a"}},
               {"Items": [{"id": "job-b"}, {"id": "job-c"}]}],
    )
    queue = FakeQueue()
    assert invoke({"source": "aws.events"}, table, queue) == {"status": "reconciled"}
    assert queue.sent == ["job-a", "job-b"]
    assert table.updates == ["job-a", "job-b"]
    assert [query.get("ExclusiveStartKey") for query in table.queries] == [None, {"id": "job-a"}]
    # Only the indexed jobs are read; the completed pending-index migration record is not (W2.11, DEPL-4).
    assert table.reads == ["job-a", "job-b", "job-c"]
