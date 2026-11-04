"""Concurrent and ambiguous retries cannot duplicate a paid address lookup."""
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from traffic_simulator import persistence
from traffic_simulator.errors import IntegrationError
from traffic_simulator.store import Store

from backend.evacuation import geocoding, service
from backend.evacuation.contracts import IncidentRequest, SessionRequest
from backend.tests.builders import resolution


def intent(store):
    session = service.create_session(store, "alice", SessionRequest(request_id=uuid4(), entry_mode="lab"))
    return IncidentRequest(request_id=uuid4(), session_id=session["session_id"], preset_id="paradise-2018")


def test_concurrent_same_request_reserves_one_lookup_and_rejoins_saved_receipt(tmp_path):
    store, entered, released, calls = Store(tmp_path), threading.Event(), threading.Event(), []
    request = intent(store)

    def resolve(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert released.wait(3)
        return resolution(*args, **kwargs)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.create_incident, store, "alice", request, resolver=resolve)
        assert entered.wait(3)
        try:
            with pytest.raises(IntegrationError) as pending:
                service.create_incident(store, "alice", request, resolver=resolve)
            assert (pending.value.code, pending.value.retryable) == ("GEOCODER_IN_PROGRESS", True)
        finally:
            released.set()
        receipt = first.result(timeout=3)
    assert service.create_incident(store, "alice", request, resolver=resolve) == receipt
    assert len(calls) == 1


def test_lookup_checkpoint_survives_failed_incident_commit_without_second_paid_invocation(tmp_path, monkeypatch):
    store, calls = Store(tmp_path), []
    request = intent(store)
    original, failed = persistence.commit, []

    def lose_commit(target, writes):
        if not failed and any(value.get("kind") == "incident" for value, _ in writes):
            failed.append(1)
            raise IntegrationError("STORE_UNAVAILABLE", "Record storage is unavailable.", 503, retryable=True)
        return original(target, writes)

    def resolve(*args, **kwargs):
        calls.append(1)
        return resolution(*args, **kwargs)

    monkeypatch.setattr(persistence, "commit", lose_commit)
    with pytest.raises(IntegrationError, match="Record storage"):
        service.create_incident(store, "alice", request, resolver=resolve)
    assert service.create_incident(store, "alice", request, resolver=resolve)["location"]["status"] == "resolved"
    assert len(calls) == 1


def test_lost_geocoder_reply_is_retained_unknown_and_never_replayed(tmp_path):
    store, calls = Store(tmp_path), []
    request = intent(store)

    def lose_reply(*_, **__):
        calls.append(1)
        raise IntegrationError("GEOCODER_UNAVAILABLE", "Provider text is not proof of invocation.", 503)

    for _ in range(2):
        with pytest.raises(IntegrationError) as failure:
            service.create_incident(store, "alice", request, resolver=lose_reply)
        assert failure.value.code == "GEOCODER_OUTCOME_UNKNOWN" and not failure.value.retryable
        assert "Provider text" not in failure.value.message
    assert len(calls) == 1
    assert not any(job.get("kind") == "incident" for job in store.list("alice"))


def test_definitive_ambiguity_remains_actionable_without_second_lookup(tmp_path):
    store, calls = Store(tmp_path), []
    request = intent(store)

    def ambiguous(*_, **__):
        calls.append(1)
        raise IntegrationError("LOCATION_AMBIGUOUS", "Include the city and street number.", 422)

    for _ in range(2):
        with pytest.raises(IntegrationError) as failure:
            service.create_incident(store, "alice", request, resolver=ambiguous)
        assert failure.value.code == "LOCATION_AMBIGUOUS" and failure.value.status == 422
    assert len(calls) == 1


def test_definitive_budget_refusal_can_retry_next_day_without_replaying_an_invocation(tmp_path, monkeypatch):
    store, spends, calls = Store(tmp_path), [], []
    request = intent(store)

    def spend(*_, **__):
        spends.append(1)
        if len(spends) == 1:
            raise IntegrationError("DAILY_LIMIT_REACHED", "Daily address limit reached.", 429,
                                   retryable=True, retry_after=2)

    def resolve(*args, **kwargs):
        calls.append(1)
        return resolution(*args, **kwargs)
    monkeypatch.setattr(geocoding.budgets, "spend_daily", spend)
    with pytest.raises(IntegrationError) as first:
        service.create_incident(store, "alice", request, resolver=resolve)
    assert first.value.code == "DAILY_LIMIT_REACHED"
    with pytest.raises(IntegrationError):
        service.create_incident(store, "alice", request, resolver=resolve)
    assert calls == [] and len(spends) == 1
    # Advance only the refusal's retained retry time; the request still has its original UUID/body.
    key = geocoding.digest(["alice", "incident_geocode", str(request.request_id)])
    row = store.get("incident_geocode:" + key)
    geocoding.commit(store, [({**row, "revision": row["revision"] + 1, "retry_at": 0}, row["revision"])])
    assert service.create_incident(store, "alice", request, resolver=resolve)["location"]["status"] == "resolved"
    assert len(spends) == 2 and len(calls) == 1
