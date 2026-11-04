"""Coverage decisions publish once under the worker lease and cannot start a traffic run."""
import copy
import json
import logging
import threading

import pytest
from traffic_simulator import network_preparation, worker
from traffic_simulator.models import PreparationScenario
from traffic_simulator.persistence import digest
from traffic_simulator.store import Store
from traffic_simulator.testing import create_job


def roadless_outcome(scenario):
    return {"outcome": "unavailable", "scenario": scenario.model_dump(), "coverage": {
        "reason_code": "NO_DRIVABLE_ROADS", "message": "Only hiking paths are mapped.",
        "extent_mode": "auto", "requested_radius_km": None, "searched_radius_km": 15,
        "study_bounds": [[-122, 46, -121, 47]], "attempts": [{"radius_km": 15, "status": "unavailable"}],
        "source_snapshot": None,
    }}


def preparation_job(store):
    job = create_job(store, "alice", PreparationScenario(radius_km=4).model_dump())
    store.update(job["id"], kind="network_preparation", extent_mode="auto")
    return store.get(job["id"])


def test_unavailable_preparation_completes_with_immutable_coverage_only(monkeypatch, caplog):
    store, observed = Store(), []
    job = preparation_job(store)
    outcome = roadless_outcome(PreparationScenario.model_validate(job["scenario"]))

    def prepare(scenario, progress, check, **options):
        check()
        observed.append(options)
        return copy.deepcopy(outcome)

    monkeypatch.setattr(network_preparation, "prepare", prepare)
    monkeypatch.setattr(worker, "simulate", lambda *_: pytest.fail("Coverage must not start traffic policies"))
    caplog.set_level(logging.INFO)
    assert worker.execute(store, job, threading.Event())
    completed = store.get(job["id"])
    assert completed["status"] == "completed" and completed["preparation_outcome"] == "unavailable"
    assert completed["road_coverage"] == outcome["coverage"]
    assert observed[0]["extent_mode"] == "auto" and observed[0]["max_radius_km"] == 15
    artifact = store.artifact(job["id"], "coverage")
    assert artifact == {**outcome, "sha256": digest(outcome)}
    for name in ("bundle", "network", "result"):
        with pytest.raises(FileNotFoundError):
            store.artifact(job["id"], name)
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert not worker.execute(store, job, threading.Event()), "completed delivery must not be executed again"
    assert store.artifact(job["id"], "coverage") == artifact and store.get(job["id"]) == completed


def test_cancelled_preparation_does_not_publish_coverage(monkeypatch):
    store = Store()
    job = preparation_job(store)

    def prepare(scenario, progress, check, **_):
        store.cancel_run(job["id"])
        return roadless_outcome(scenario)

    monkeypatch.setattr(network_preparation, "prepare", prepare)
    assert worker.execute(store, job, threading.Event())
    cancelled = store.get(job["id"])
    assert cancelled["status"] == "cancelled" and "artifact_attempt" not in cancelled
    assert "preparation_outcome" not in cancelled
    assert not list(store.root.glob(f"runs/{job['id']}/**/coverage.json.gz"))


def test_lost_lease_cannot_publish_coverage(monkeypatch):
    store = Store()
    job = preparation_job(store)

    def prepare(scenario, progress, check, **_):
        store.update(job["id"], leaseToken="replacement-owner")
        return roadless_outcome(scenario)

    monkeypatch.setattr(network_preparation, "prepare", prepare)
    assert not worker.execute(store, job, threading.Event())
    interrupted = store.get(job["id"])
    assert "artifact_attempt" not in interrupted and "preparation_outcome" not in interrupted
    assert not list(store.root.glob(f"runs/{job['id']}/**/coverage.json.gz"))


def test_provider_failure_retains_failed_outcome_and_no_coverage_artifact(monkeypatch):
    from traffic_simulator.errors import IntegrationError

    store = Store()
    job = preparation_job(store)

    def prepare(*_, **__):
        raise IntegrationError("OSM_SOURCE_UNAVAILABLE", "Canonical road source unavailable.", 503)

    monkeypatch.setattr(network_preparation, "prepare", prepare)
    assert worker.execute(store, job, threading.Event())
    failed = store.get(job["id"])
    assert failed["status"] == "failed" and failed["error_code"] == "OSM_SOURCE_UNAVAILABLE"
    assert "preparation_outcome" not in failed and "artifact_attempt" not in failed
    assert not list(store.root.glob(f"runs/{job['id']}/**/coverage.json.gz"))
    assert "sha256" not in json.dumps(failed)
