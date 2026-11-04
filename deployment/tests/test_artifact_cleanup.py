"""Artifact collection protects live references, fresh uploads and concurrent publication."""
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "infra/lib/artifact_cleanup.py"


def collector(monkeypatch, records, modified, *, appears_on_recheck=False, delete_errors=False):
    identifier = str(uuid4())
    table = Mock()
    reads = 0

    def get_item(Key, ConsistentRead):
        nonlocal reads
        assert ConsistentRead
        if Key["id"].startswith("artifact_cleanup:"):
            return {}
        reads += 1
        return {"Item": records or {"id": "new"}} if records or (appears_on_recheck and reads > 1) else {}

    table.get_item.side_effect = get_item
    s3 = Mock()
    s3.list_objects_v2.side_effect = [
        {"CommonPrefixes": [{"Prefix": f"runs/{identifier}/"}]},
        {"CommonPrefixes": []}, {"CommonPrefixes": []}]
    s3.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": f"runs/{identifier}/attempt/result.json.gz", "LastModified": modified}]}]
    s3.delete_objects.return_value = {"Errors": [{"Code": "AccessDenied"}]} if delete_errors else {}
    spec = importlib.util.spec_from_file_location("artifact_cleanup_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("JOBS_TABLE", "fixture")
    monkeypatch.setenv("ARTIFACTS_BUCKET", "fixture")
    monkeypatch.setattr(module, "boto3", SimpleNamespace(
        resource=lambda name: SimpleNamespace(Table=lambda name: table), client=lambda name: s3))
    return module, table, s3


@pytest.mark.parametrize("records,recent,appears,expected", [
    ({"expires": 0}, False, False, 0),  # TTL deletion is asynchronous; age never overrides live metadata.
    ({}, True, False, 0),             # A fresh orphan may still be awaiting its metadata commit.
    ({}, False, True, 0),             # A record appeared during the S3 list.
    ({}, False, False, 1),            # An old unreferenced attempt is safe to collect.
])
def test_only_old_unreferenced_artifacts_are_collected(monkeypatch, records, recent, appears, expected):
    modified = datetime.now(UTC) - timedelta(hours=1 if recent else 48)
    module, table, s3 = collector(monkeypatch, records, modified, appears_on_recheck=appears)
    assert module.handler({}, None) == {"deleted": expected}
    assert s3.delete_objects.call_count == expected
    assert table.update_item.call_count == 3, "Each root has its own durable scan cursor."
    for call in table.update_item.call_args_list:
        assert call.kwargs["UpdateExpression"] == "SET #cursor=:cursor"
        assert call.kwargs["ExpressionAttributeNames"] == {"#cursor": "cursor"}


def test_partial_delete_failure_keeps_the_cursor_for_retry(monkeypatch):
    module, table, _ = collector(monkeypatch, {}, datetime.now(UTC) - timedelta(days=2), delete_errors=True)
    with pytest.raises(RuntimeError, match="same page"):
        module.handler({}, None)
    table.update_item.assert_not_called()


@pytest.mark.parametrize("reference", ["job", "assessment", "none"])
def test_landscape_inputs_outlive_both_job_and_assessment_references(monkeypatch, reference):
    module, table, s3 = collector(monkeypatch, {}, datetime.now(UTC) - timedelta(days=2))
    job_id, assessment_id = str(uuid4()), str(uuid4())
    prefix = f"fire-risk/inputs/{job_id}/"
    s3.list_objects_v2.side_effect = [
        {"CommonPrefixes": []}, {"CommonPrefixes": []}, {"CommonPrefixes": [{"Prefix": prefix}]}]
    s3.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": f"{prefix}{assessment_id}/{'a'*64}.json.gz",
                       "LastModified": datetime.now(UTC) - timedelta(days=2)}]}]
    live = job_id if reference == "job" else f"fire_exposure:{assessment_id}"
    table.get_item.side_effect = lambda Key, ConsistentRead: (
        {"Item": {"id": live}} if reference != "none" and Key["id"] == live else {})
    assert module.handler({}, None) == {"deleted": int(reference == "none")}
    assert s3.delete_objects.call_count == int(reference == "none")
