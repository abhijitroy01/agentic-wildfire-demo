"""Artifact retries remain within one fenced operation and stop on cancellation or permanent failures."""
import threading

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from traffic_simulator import artifact_io, worker
from traffic_simulator.errors import IntegrationError
from traffic_simulator.store import Store
from traffic_simulator.testing import create_job


def test_retry_identical_write_without_repeating_road_discovery(tmp_path, monkeypatch):
    store = Store(tmp_path)
    job = create_job(store, "alice", {"lat": 47.6, "lon": -122.3, "radius_km": 4})
    store.update(job["id"], kind="road_discovery")
    original, acquired, writes = store.artifact, [], []
    result = {"geometry": {"type": "FeatureCollection", "features": []}, "highways": []}
    monkeypatch.setattr(worker, "discover", lambda *_args, **_kwargs: acquired.append(1) or result)
    monkeypatch.setattr(artifact_io, "pause", lambda _, check: check())

    def flaky(*args, **kwargs):
        writes.append((args, kwargs))
        if len(writes) == 1:
            raise EndpointConnectionError(endpoint_url="https://storage.example.invalid")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "artifact", flaky)
    assert worker.execute(store, store.get(job["id"]), threading.Event())
    assert store.get(job["id"])["status"] == "completed"
    assert len(acquired) == 1 and len(writes) == 2 and writes[0] == writes[1]
    assert original(job["id"], "result") == result


def test_frozen_bundle_read_recovers_without_changing_input_attempt(monkeypatch):
    calls, checkpoint = [], {"bundle_id": "retained"}
    monkeypatch.setattr(artifact_io, "pause", lambda _, check: check())

    def read():
        calls.append("original-attempt")
        if len(calls) < 3:
            raise EndpointConnectionError(endpoint_url="https://storage.example.invalid")
        return checkpoint
    assert artifact_io.retained(read, lambda: None) is checkpoint
    assert calls == ["original-attempt"] * 3


def test_exhausted_storage_recovery_keeps_a_safe_typed_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(artifact_io, "pause", lambda _, check: check())

    def write():
        calls.append(1)
        raise EndpointConnectionError(endpoint_url="https://private.example.invalid")
    with pytest.raises(IntegrationError) as failure:
        artifact_io.retained(write, lambda: None)
    assert failure.value.code == "ARTIFACT_STORE_UNAVAILABLE"
    assert failure.value.retryable and "private" not in failure.value.message and len(calls) == 3


def test_cancellation_during_storage_backoff_prevents_the_next_write(monkeypatch):
    checks, writes = [], []

    def check():
        checks.append(1)
        if len(checks) > 1:
            raise worker.Cancelled()

    def write():
        writes.append(1)
        raise EndpointConnectionError(endpoint_url="https://storage.example.invalid")
    monkeypatch.setattr(artifact_io, "pause", lambda _, check: check())
    with pytest.raises(worker.Cancelled):
        artifact_io.retained(write, check)
    assert len(writes) == 1


@pytest.mark.parametrize("failure", [
    FileNotFoundError(), ValueError("bad stored evidence"),
    ClientError({"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}}, "PutObject"),
])
def test_permanent_artifact_failures_are_not_retried(failure):
    writes = []

    def write():
        writes.append(1)
        raise failure
    with pytest.raises(type(failure)):
        artifact_io.retained(write, lambda: None)
    assert len(writes) == 1
