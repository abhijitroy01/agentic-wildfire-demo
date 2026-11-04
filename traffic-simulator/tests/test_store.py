import json
import re
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError
from traffic_simulator import store as job_store
from traffic_simulator.errors import IntegrationError
from traffic_simulator.models import Scenario
from traffic_simulator.persistence import commit
from traffic_simulator.store import Store, plain
from traffic_simulator.testing import create_job

# The store's clock in the AWS-mode condition checks: conditions compare whole seconds, `updated` keeps the fraction.
NOW = 1_790_000_000.25


@pytest.fixture
def cloud(monkeypatch):
    """Return a Store in AWS mode over a Mock DynamoDB table, with the store module's clock fixed at NOW."""
    monkeypatch.setattr(job_store, "time", SimpleNamespace(time=lambda: NOW))
    store = Store.__new__(Store)
    store.cloud, store.table = True, Mock()
    return store


def refused_write(store, write):
    """Return what a write returns when DynamoDB refuses its condition; any other DynamoDB error must propagate."""
    store.table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
    refused = write()
    store.table.update_item.side_effect = ClientError({"Error": {"Code": "ThrottlingException"}}, "UpdateItem")
    with pytest.raises(ClientError):
        write()
    store.table.update_item.side_effect = None
    return refused


def test_duplicate_workers_cannot_claim_the_same_run(tmp_path):
    store = Store(tmp_path)
    job = create_job(store, "alice", Scenario().model_dump())
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda n: store.claim(job["id"], str(n)), range(8)))
    assert sum(claims) == 1
    store.update(job["id"], leaseUntil=0)
    assert store.claim(job["id"], "recovery")
    assert not store.claim(job["id"], "duplicate")


def test_artifacts_are_roundtrippable_and_paths_are_bounded(tmp_path):
    store = Store(tmp_path)
    job = create_job(store, "alice", {})
    store.artifact(job["id"], "result", {"a": [1, 2, 3]})
    assert store.artifact(job["id"], "result") == {"a": [1, 2, 3]}
    with pytest.raises(ValueError):
        store.artifact("../../etc", "result")
    with pytest.raises(ValueError):
        store.artifact(job["id"], "../../etc")


def test_cancel_and_old_worker_cannot_publish_completion(tmp_path):
    store = Store(tmp_path)
    job = create_job(store, "alice", {})
    assert store.claim(job["id"], "worker")
    assert not store.finish(job["id"], "wrong-worker", [])
    store.update(job["id"], cancel=True, status="cancelled")
    assert not store.finish(job["id"], "worker", [])
    assert store.get(job["id"])["status"] == "cancelled"


def test_cancel_never_overwrites_a_finished_run(tmp_path):
    store = Store(tmp_path)
    job = create_job(store, "alice", {})
    assert store.claim(job["id"], "worker")
    assert store.finish(job["id"], "worker", [])
    assert store.cancel_run(job["id"])["status"] == "completed"


def test_attempt_artifacts_are_immutable_after_publication(tmp_path):
    store = Store(tmp_path)
    job = create_job(store, "alice", {})
    first, second = str(uuid4()), str(uuid4())
    assert store.claim(job["id"], first)
    store.artifact(job["id"], "result", {"attempt": first}, attempt=first)
    store.update(job["id"], leaseUntil=0)
    assert store.claim(job["id"], second)
    store.artifact(job["id"], "result", {"attempt": second}, attempt=second)
    assert store.finish(job["id"], second, [], artifact_attempt=second)
    store.artifact(job["id"], "result", {"stale": True}, attempt=first)
    assert not store.finish(job["id"], first, [], artifact_attempt=first)
    assert not store.fenced_update(job["id"], first, status="failed")
    assert store.artifact(job["id"], "result") == {"attempt": second}


def test_cancellation_waits_for_worker_and_cannot_overwrite_completion(tmp_path):
    store = Store(tmp_path)
    job = create_job(store, "alice", {})
    token = str(uuid4())
    assert store.claim(job["id"], token)
    assert store.cancel_run(job["id"])["status"] == "cancelling"
    assert not store.acknowledge_cancel(job["id"])
    assert not store.finish(job["id"], token, [])
    store.update(job["id"], leaseUntil=0)
    assert store.acknowledge_cancel(job["id"])



def test_daily_budgets_count_per_utc_day_and_scope_and_never_pass_the_limit(tmp_path):
    store = Store(tmp_path)
    day = 1_790_000_000  # 2026-09-21 UTC, mid-day
    with ThreadPoolExecutor(max_workers=8) as pool:
        spent = list(pool.map(lambda _: store.spend("tomtom_calls", 5, now=day), range(12)))
    assert spent.count(True) == 5 and spent.count(False) == 7
    # The next UTC day starts a new counter; a scope (a viewer IP) counts separately and its key holds a digest.
    assert store.spend("tomtom_calls", 5, now=day + 86400)
    assert store.spend("demo_sessions", 1, scope="198.51.100.7", now=day)
    assert not store.spend("demo_sessions", 1, scope="198.51.100.7", now=day)
    assert store.spend("demo_sessions", 1, scope="203.0.113.9", now=day)
    with store.db() as db:
        keys = [row[0] for row in db.execute("SELECT id FROM jobs WHERE id LIKE 'budget:%'")]
    assert "budget:tomtom_calls:2026-09-21" in keys and not any("198.51.100.7" in key for key in keys)
    counter = store.get("budget:tomtom_calls:2026-09-21")
    assert counter["count"] == 5 and counter["expires"] == 1_790_035_200 + 86400  # a day after the date ends
    assert not store.spend("geocoding_requests", 0, now=day), "A limit of 0 refuses every use."
    # Counters are not jobs: the local worker never picks them up.
    assert store.pending() == []


def test_dynamodb_numbers_preserve_integer_precision_and_fractional_coordinates():
    result = plain({"revision": Decimal(1), "nested": [
        Decimal(9007199254740993), Decimal("34.04118"), Decimal("-118.54988")],
        "flag": True, "missing": None})
    assert type(result["revision"]) is int
    assert result["nested"] == [9007199254740993, 34.04118, -118.54988]
    assert result["flag"] is True and result["missing"] is None


def test_cloud_daily_budget_is_one_conditional_counter_update_with_a_ttl():
    table = Mock()
    store = Store.__new__(Store)
    store.cloud, store.table = True, table
    assert store.spend("demo_sessions", 20, scope="198.51.100.7", now=1_790_000_000)
    update = table.update_item.call_args.kwargs
    assert update["Key"]["id"].startswith("budget:demo_sessions:2026-09-21:")
    assert "198.51.100.7" not in json.dumps(update)
    assert update["UpdateExpression"] == "SET #k=:kind, #e=:expires ADD #c :one"
    assert update["ConditionExpression"] == "attribute_not_exists(#c) OR #c < :limit"
    assert update["ExpressionAttributeValues"] == {":kind": "budget", ":expires": 1_790_035_200 + 86400,
                                                   ":one": 1, ":limit": 20}
    table.update_item.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
    assert not store.spend("demo_sessions", 20, scope="198.51.100.7")
    table.update_item.side_effect = ClientError({"Error": {"Code": "ThrottlingException"}}, "UpdateItem")
    with pytest.raises(ClientError):
        store.spend("demo_sessions", 20)


def test_cloud_claim_takes_only_a_queued_or_expired_lease_of_a_run_not_cancelled(cloud):
    assert cloud.claim("job", "token")
    assert cloud.table.update_item.call_args.kwargs == {
        "Key": {"id": "job"},
        "UpdateExpression": "SET #s=:r, leaseUntil=:until, leaseToken=:token",
        "ConditionExpression": "(#s=:q OR (#s=:r AND leaseUntil < :now)) AND cancel=:no",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":r": "running", ":q": "queued", ":until": int(NOW) + 180,
                                      ":now": int(NOW), ":token": "token", ":no": False}}
    assert refused_write(cloud, lambda: cloud.claim("job", "token")) is False


def test_cloud_worker_writes_are_fenced_by_the_lease_token_and_an_unexpired_lease(cloud):
    cloud.heartbeat("job", "token")
    assert cloud.table.update_item.call_args.kwargs == {
        "Key": {"id": "job"},
        "UpdateExpression": "SET #k0=:v0, #k1=:v1",
        "ConditionExpression": "leaseToken=:token AND leaseUntil>=:now",
        "ExpressionAttributeNames": {"#k0": "leaseUntil", "#k1": "updated"},
        "ExpressionAttributeValues": {":v0": int(NOW) + 180, ":v1": Decimal(str(NOW)), ":token": "token",
                                      ":now": int(NOW)}}
    assert refused_write(cloud, lambda: cloud.fenced_update("job", "token", phase="Simulating")) is False
    cloud.table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
    with pytest.raises(RuntimeError, match="Worker lease lost"):
        cloud.heartbeat("job", "token")


def test_cloud_finish_publishes_only_for_the_lease_holder_of_a_running_run_not_cancelled(cloud):
    assert cloud.finish("job", "token", [{"policy": "baseline"}], artifact_attempt="attempt",
                        timings={"simulate": 1.5})
    request = cloud.table.update_item.call_args.kwargs
    # The condition's status placeholder must name status, whatever position status has among the values.
    placeholder = re.fullmatch(r"leaseToken=:token AND cancel=:no AND (#k\d+)=:running AND leaseUntil>=:now",
                               request["ConditionExpression"]).group(1)
    assert request["ExpressionAttributeNames"][placeholder] == "status"
    assert request == {
        "Key": {"id": "job"},
        "UpdateExpression": "SET #k0=:v0, #k1=:v1, #k2=:v2, #k3=:v3, #k4=:v4, #k5=:v5, #k6=:v6",
        "ConditionExpression": "leaseToken=:token AND cancel=:no AND #k0=:running AND leaseUntil>=:now",
        "ExpressionAttributeNames": {"#k0": "status", "#k1": "progress", "#k2": "phase", "#k3": "summaries",
                                     "#k4": "updated", "#k5": "artifact_attempt", "#k6": "timings"},
        "ExpressionAttributeValues": {":v0": "completed", ":v1": 1, ":v2": "Comparison ready",
                                      ":v3": [{"policy": "baseline"}], ":v4": Decimal(str(NOW)),
                                      ":v5": "attempt", ":v6": {"simulate": Decimal("1.5")}, ":token": "token",
                                      ":no": False, ":running": "running", ":now": int(NOW)}}
    assert refused_write(cloud, lambda: cloud.finish("job", "token", [])) is False


def test_cloud_cancel_marks_only_a_queued_running_or_cancelling_run(cloud):
    cloud.table.get_item.return_value = {"Item": {"id": "job", "status": "cancelling", "cancel": True}}
    assert cloud.cancel_run("job")["status"] == "cancelling"
    assert cloud.table.update_item.call_args.kwargs == {
        "Key": {"id": "job"},
        "UpdateExpression": "SET cancel=:yes, #s=:c, phase=:p",
        "ConditionExpression": "#s IN (:q,:r,:c)",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":yes": True, ":q": "queued", ":r": "running", ":c": "cancelling",
                                      ":p": "Cancellation requested"}}
    cloud.table.get_item.assert_called_with(Key={"id": "job"}, ConsistentRead=True)
    # A run that finished first keeps its status: the refused condition is the answer, not an error.
    cloud.table.get_item.return_value = {"Item": {"id": "job", "status": "completed", "cancel": False}}
    assert refused_write(cloud, lambda: cloud.cancel_run("job"))["status"] == "completed"


def test_cloud_cancel_is_acknowledged_only_after_the_worker_lease_lapses(cloud):
    cancelling = {"id": "job", "status": "cancelling", "cancel": True, "leaseUntil": int(NOW) + 10}
    for current in (cancelling, {**cancelling, "cancel": False, "leaseUntil": int(NOW) - 1}):
        cloud.table.get_item.return_value = {"Item": current}
        assert not cloud.acknowledge_cancel("job")
    cloud.table.update_item.assert_not_called()
    cloud.table.get_item.return_value = {"Item": {**cancelling, "leaseUntil": int(NOW) - 1}}
    assert cloud.acknowledge_cancel("job")
    assert cloud.table.update_item.call_args.kwargs == {
        "Key": {"id": "job"},
        "UpdateExpression": "SET #s=:done, phase=:p",
        "ConditionExpression": "cancel=:yes AND #s=:c AND (attribute_not_exists(leaseUntil) OR leaseUntil<:now)",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":yes": True, ":c": "cancelling", ":done": "cancelled", ":p": "Cancelled",
                                      ":now": int(NOW)}}
    assert refused_write(cloud, lambda: cloud.acknowledge_cancel("job")) is False


def test_cloud_retention_only_extends_the_expiry_of_an_existing_record(cloud):
    cloud.retain("record", 1_800_000_000)
    assert cloud.table.update_item.call_args.kwargs == {
        "Key": {"id": "record"},
        "UpdateExpression": "SET expires=:until ADD #r :one",
        "ConditionExpression": "attribute_exists(id) AND (attribute_not_exists(expires) OR expires<:until)",
        "ExpressionAttributeNames": {"#r": "revision"},
        "ExpressionAttributeValues": {":until": 1_800_000_000, ":one": 1}}
    # A later expiry already holds: the refused condition is not an error while the record exists.
    cloud.table.get_item.return_value = {"Item": {"id": "record", "expires": 1_900_000_000}}
    assert refused_write(cloud, lambda: cloud.retain("record", 1_800_000_000)) is None
    cloud.table.get_item.return_value = {}
    cloud.table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
    with pytest.raises(FileNotFoundError):
        cloud.retain("record", 1_800_000_000)


def test_invalid_scenario_cannot_start():
    for override in ({"lat": float("nan")}, {"vehicles": 100000}, {"unexpected": True},
                     {"policies": ["baseline", "baseline"]}, {"departure_minutes": 40, "duration_minutes": 5},
                     {"highway": "$(arbitrary-command)"}):
        with pytest.raises(ValidationError):
            Scenario(**override)


def test_us_hazard_inputs_preserve_fractional_meters():
    scenario = Scenario(hazard_radius_m=500 * 0.3048, hazard_growth_m_min=10 * 0.3048)
    assert scenario.hazard_radius_m == pytest.approx(152.4)
    assert scenario.hazard_growth_m_min == pytest.approx(3.048)
    for values in ({"hazard_radius_m": 3000.1}, {"hazard_growth_m_min": 100.1},
                   {"hazard_radius_m": float("nan")}):
        with pytest.raises(ValidationError):
            Scenario(**values)


NOW = 1_790_000_000
CLOCK = NOW + 0.5
# Each cloud lease and cancel write: the call, then the exact UpdateItem arguments DynamoDB receives.
LEASE_WRITES = {
    "claim": (lambda store: store.claim("job", "token"), {
        "UpdateExpression": "SET #s=:r, leaseUntil=:until, leaseToken=:token",
        "ConditionExpression": "(#s=:q OR (#s=:r AND leaseUntil < :now)) AND cancel=:no",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":r": "running", ":q": "queued", ":until": NOW + 180, ":now": NOW,
                                      ":token": "token", ":no": False}}),
    "fenced_update": (lambda store: store.fenced_update("job", "token", leaseUntil=NOW + 180), {
        "UpdateExpression": "SET #k0=:v0, #k1=:v1",
        "ConditionExpression": "leaseToken=:token AND leaseUntil>=:now",
        "ExpressionAttributeNames": {"#k0": "leaseUntil", "#k1": "updated"},
        "ExpressionAttributeValues": {":v0": NOW + 180, ":v1": Decimal(str(CLOCK)), ":token": "token", ":now": NOW}}),
    "finish": (lambda store: store.finish("job", "token", [{"policy": "baseline", "exited_vehicles": 3}],
                                          artifact_attempt="attempt", timings={"total": 1.5}), {
        "UpdateExpression": "SET #k0=:v0, #k1=:v1, #k2=:v2, #k3=:v3, #k4=:v4, #k5=:v5, #k6=:v6",
        "ConditionExpression": "leaseToken=:token AND cancel=:no AND #k0=:running AND leaseUntil>=:now",
        "ExpressionAttributeNames": {"#k0": "status", "#k1": "progress", "#k2": "phase", "#k3": "summaries",
                                     "#k4": "updated", "#k5": "artifact_attempt", "#k6": "timings"},
        "ExpressionAttributeValues": {":v0": "completed", ":v1": 1, ":v2": "Comparison ready",
                                      ":v3": [{"policy": "baseline", "exited_vehicles": 3}],
                                      ":v4": Decimal(str(CLOCK)), ":v5": "attempt", ":v6": {"total": Decimal("1.5")},
                                      ":token": "token", ":no": False, ":running": "running", ":now": NOW}}),
    "cancel_run": (lambda store: store.cancel_run("job"), {
        "UpdateExpression": "SET cancel=:yes, #s=:c, phase=:p", "ConditionExpression": "#s IN (:q,:r,:c)",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":yes": True, ":q": "queued", ":r": "running", ":c": "cancelling",
                                      ":p": "Cancellation requested"}}),
    "acknowledge_cancel": (lambda store: store.acknowledge_cancel("job"), {
        "UpdateExpression": "SET #s=:done, phase=:p",
        "ConditionExpression": "cancel=:yes AND #s=:c AND (attribute_not_exists(leaseUntil) OR leaseUntil<:now)",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":yes": True, ":c": "cancelling", ":done": "cancelled", ":p": "Cancelled",
                                      ":now": NOW}}),
    "retain": (lambda store: store.retain("job", NOW + 86400), {
        "UpdateExpression": "SET expires=:until ADD #r :one",
        "ConditionExpression": "attribute_exists(id) AND (attribute_not_exists(expires) OR expires<:until)",
        "ExpressionAttributeNames": {"#r": "revision"}, "ExpressionAttributeValues": {":until": NOW + 86400,
                                                                                      ":one": 1}}),
}


def cloud_store(monkeypatch):
    """Return a cloud-mode Store over a Mock table whose job is a cancelled run with an expired lease."""
    monkeypatch.setattr("traffic_simulator.store.time", SimpleNamespace(time=lambda: CLOCK))
    store = Store.__new__(Store)
    store.cloud, store.table = True, Mock()
    store.table.get_item.return_value = {"Item": {"id": "job", "cancel": True, "leaseUntil": Decimal(NOW - 1)}}
    return store


@pytest.mark.parametrize("operation", sorted(LEASE_WRITES))
def test_cloud_lease_and_cancel_writes_send_their_exact_conditions(monkeypatch, operation):
    """Each conditional write sends one exact UpdateItem; a failed condition is a refusal, other errors raise."""
    write, expected = LEASE_WRITES[operation]
    store = cloud_store(monkeypatch)
    write(store)
    assert store.table.update_item.call_args.kwargs == {"Key": {"id": "job"}, **expected}
    store.table.update_item.side_effect = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}},
                                                      "UpdateItem")
    if operation == "retain":
        write(store)  # The record exists, so its expiry was already later; nothing to raise.
        store.table.get_item.return_value = {}
        with pytest.raises(FileNotFoundError):
            write(store)
    elif operation == "cancel_run":
        # A run that finished meanwhile is left alone and read back as it is.
        assert write(store) == {"id": "job", "cancel": True, "leaseUntil": NOW - 1}
    else:
        assert write(store) is False
    store.table.update_item.side_effect = ClientError({"Error": {"Code": "ThrottlingException"}}, "UpdateItem")
    with pytest.raises(ClientError):
        write(store)


@pytest.mark.parametrize("code", ["NoSuchKey", "NotFound", "404", "AccessDenied"])
def test_cloud_missing_artifacts_match_local_absence_without_hiding_permissions(code):
    store = Store.__new__(Store)
    store.cloud, store.bucket, store.s3 = True, "fixture", Mock()
    store.table = Mock()
    store.table.get_item.return_value = {}
    store.s3.get_object.side_effect = ClientError({"Error": {"Code": code}}, "GetObject")
    with pytest.raises(ClientError if code == "AccessDenied" else FileNotFoundError):
        store.artifact(str(uuid4()), "bundle")


def test_referenced_metadata_retention_is_monotonic_and_invalidates_older_writers(tmp_path):
    store = Store(tmp_path)
    job = dict(create_job(store, "alice", {}), revision=1)
    store.put_record(job)
    store.retain(job["id"], job["expires"] + 86400)
    revision = store.get(job["id"])["revision"]
    store.retain(job["id"], job["expires"])
    assert store.get(job["id"])["expires"] == job["expires"] + 86400
    assert store.get(job["id"])["revision"] == revision
    with pytest.raises(IntegrationError, match="State changed"):
        commit(store, [(dict(job, revision=job["revision"] + 1), job["revision"])])
    assert store.get(job["id"])["expires"] == job["expires"] + 86400
    with pytest.raises(FileNotFoundError):
        store.retain(str(uuid4()), job["expires"])
