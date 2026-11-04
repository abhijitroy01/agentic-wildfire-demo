"""Durable risk jobs preserve source identity, billing, leases and frozen inputs beyond a chat deadline."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fire_risk_modeler.models import (
    AssessmentResult,
    LandscapeAssessmentRequest,
    LandscapeInput,
    model_input_hash,
)
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import budget_day
from traffic_simulator.store import Store

from backend import fire_risk_broker as broker
from backend import fire_risk_jobs as jobs
from backend.tests.builders import client
from backend.tests.test_fire_risk_broker import content, current_weather

ROOT = Path(__file__).resolve().parents[2]
POINT = (47.470805726545464, -121.25588168570884)


def source_content(*, calm=False):
    answer = content(*POINT)
    weather = current_weather(*POINT)
    if calm:
        weather["wind"]["speed"]["value"] = 0
        weather["wind"]["direction"]["value"] = None
    answer["mapData"]["locations"][0]["currentWeather"] = weather
    return answer


def test_transient_landscape_read_recovers_with_same_frozen_weather(tmp_path, monkeypatch):
    from fire_risk_modeler import landscape_acquisition
    from fire_risk_modeler.landscape import LandscapeError
    store, calls = Store(tmp_path), []
    job = queued(store)

    def acquire(*args):
        calls.append(args)
        if len(calls) == 1:
            raise LandscapeError("LANDSCAPE_REQUEST_FAILED", "Canonical source timed out.", retryable=True)
        return landscape()

    monkeypatch.setattr(landscape_acquisition, "prepare_landscape", acquire)
    monkeypatch.setattr(jobs, "cancellable_wait", lambda seconds, cancel: cancel())
    monkeypatch.setattr(broker, "fetch_weather", lambda *_: pytest.fail("Source retry repeated paid weather"))
    monkeypatch.setattr(broker, "invoke_model", lambda value, _: calculated(value))
    assert jobs.execute(store, job["id"], threading.Event())
    result = jobs.read_job(store, job["id"], "owner").assessments[0]
    assert result.availability == "available"
    assert len(calls) == 2 and calls[0] == calls[1]
    assert store.get(job["id"])["entries"][0]["landscape_attempts"] == {"1500.0": 2}


@pytest.mark.parametrize("transient,attempts", [(False, 1), (True, 3)])
def test_source_failure_is_bounded_and_has_no_substitute_geometry(tmp_path, monkeypatch, transient, attempts):
    from fire_risk_modeler import landscape_acquisition
    from fire_risk_modeler.landscape import LandscapeError
    store, calls = Store(tmp_path), []
    job = queued(store)

    def acquire(*args):
        calls.append(args)
        raise LandscapeError("LANDSCAPE_REQUEST_FAILED", "Canonical source unavailable.", retryable=transient,
                             provider_status=503 if transient else 400, role="fuel", stage="crop")

    monkeypatch.setattr(landscape_acquisition, "prepare_landscape", acquire)
    monkeypatch.setattr(jobs, "cancellable_wait", lambda seconds, cancel: cancel())
    monkeypatch.setattr(broker, "fetch_weather", lambda *_: pytest.fail("Failure repeated paid weather"))
    monkeypatch.setattr(broker, "invoke_model", lambda *_: pytest.fail("Missing landscape invoked the model"))
    assert jobs.execute(store, job["id"], threading.Event())
    result = jobs.read_job(store, job["id"], "owner").assessments[0]
    assert len(calls) == attempts
    assert result.availability == "unavailable" and result.geojson.features == []


@pytest.mark.parametrize("weather_state", ["completed", "failed"])
def test_explicit_retry_is_owner_scoped_idempotent_and_keeps_original_evidence(
        tmp_path, monkeypatch, api_app, weather_state):
    from uuid import uuid4

    from fire_risk_modeler import landscape_acquisition
    from fire_risk_modeler.landscape import LandscapeError
    store = Store(tmp_path)
    job = queued(store)
    monkeypatch.setattr(landscape_acquisition, "prepare_landscape", lambda *_: (
        (_ for _ in ()).throw(LandscapeError("LANDSCAPE_REQUEST_FAILED", "Canonical source unavailable."))))
    assert jobs.execute(store, job["id"], threading.Event())
    # A settled failed batch can still contain a fresh measured sibling; retries select only usable entries.
    store.update(job["id"], weather_state=weather_state)
    old = jobs.read_job(store, job["id"], "owner").model_dump(mode="json")
    app = api_app(store, "owner", broker.create_fire_exposure_router)
    request_id = str(uuid4())
    with client(app) as browser:
        response = browser.post(f"/api/v1/fire-exposure/jobs/{job['id']}/retry",
                                json={"request_id": request_id})
        assert response.status_code == 202, response.text
        retry_id = response.json()["job_id"]
        assert browser.post(f"/api/v1/fire-exposure/jobs/{job['id']}/retry",
                            json={"request_id": request_id}).json()["job_id"] == retry_id
    with pytest.raises(IntegrationError, match="not found"):
        jobs.retry_job(store, job["id"], "other-owner", uuid4())
    monkeypatch.setattr(broker, "fetch_weather", lambda *_: pytest.fail("Explicit terrain retry repeated DTN"))
    monkeypatch.setattr(landscape_acquisition, "prepare_landscape", lambda *_: landscape())
    monkeypatch.setattr(broker, "invoke_model", lambda value, _: calculated(value))
    assert jobs.execute(store, retry_id, threading.Event())
    recovered = jobs.read_job(store, retry_id, "owner")
    assert recovered.assessments[0].availability == "available"
    assert recovered.assessments[0].assessment_id != old["assessments"][0]["assessment_id"]
    assert recovered.assessments[0].weather.model_dump(mode="json") == old["assessments"][0]["weather"]
    assert jobs.read_job(store, job["id"], "owner").model_dump(mode="json") == old


def test_calculator_admission_budget_is_retained_across_restart(tmp_path, monkeypatch):
    import httpx
    from fire_risk_modeler import landscape_acquisition
    store, acquisitions, waits = Store(tmp_path), [], []
    job = queued(store)
    monkeypatch.setattr(landscape_acquisition, "prepare_landscape",
                        lambda *args: (acquisitions.append(args), landscape())[1])

    def busy(*_):
        response = httpx.Response(429, headers={"Retry-After": "2147483647"},
                                  request=httpx.Request("POST", "http://model.internal/calculate"))
        raise httpx.HTTPStatusError("sensitive service text", request=response.request, response=response)

    monkeypatch.setattr(broker, "invoke_model", busy)

    def interrupt(delay, cancel):
        waits.append(delay)
        raise jobs.PreparationInterrupted("Intentional restart")

    monkeypatch.setattr(jobs, "cancellable_wait", interrupt)
    assert jobs.execute(store, job["id"], threading.Event()) is False
    saved = store.get(job["id"])
    entry = saved["entries"][0]
    entry["model_admission"][entry["landscape_input_hash"]] = time.time()-1
    store.update(job["id"], leaseUntil=0, entries=[entry])
    monkeypatch.setattr(broker, "fetch_weather", lambda *_: pytest.fail("Admission recovery repeated paid weather"))
    assert jobs.execute(store, job["id"], threading.Event())
    result = jobs.read_job(store, job["id"], "owner").assessments[0]
    assert len(acquisitions) == 1 and waits == [10]
    assert result.failure.code == "CALCULATION_LIMIT" and result.geojson.features == []


def queued(store, answer=None):
    reference = jobs.enqueue(store, "owner", "thread", "attempt", answer or source_content())[0]
    return store.get(reference["job_id"])


def landscape():
    fixture = ROOT / "fire-risk-modeler/tests/data/king-landscape.json"
    return LandscapeInput.model_validate(json.loads(fixture.read_text())["landscape"])


def calculated(request):
    """Protocol fixture; physical native behavior is tested in the separate modeler suite."""
    value = json.loads((Path(__file__).parent / "data/fire-risk-protocol.json").read_text())
    value.update(assessment_id=request.assessment_id, fire_ref=request.fire_ref,
                 input_hash=request.input_hash, fire=request.fire.model_dump(mode="json"),
                 weather=request.weather.model_dump(mode="json"), scenario=request.scenario.model_dump(mode="json"))
    value["landscape"]["manifest"] = request.landscape.manifest.model_dump(mode="json")
    return AssessmentResult.model_validate(value)


def test_enqueue_is_atomic_and_concurrent_duplicates_create_one_job(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(broker, "fetch_weather", lambda *_: pytest.fail("Enqueue fetched weather"))
    monkeypatch.setattr(broker, "invoke_model", lambda *_: pytest.fail("Enqueue calculated"))
    with ThreadPoolExecutor(max_workers=4) as executor:
        refs = list(executor.map(lambda _: jobs.enqueue(store, "owner", "thread", "attempt", source_content()),
                                 range(4)))
    assert all(value == refs[0] for value in refs)
    assert len(store.list("owner")) == 1
    assert store.get(refs[0][0]["job_id"])["outbox_pending"] is True


def test_measured_calm_completes_after_expired_chat_and_duplicate_delivery_does_no_work(tmp_path, monkeypatch):
    store, preparations, calculations = Store(tmp_path), [], []
    monkeypatch.setattr(broker, "fetch_weather", lambda *_: pytest.fail("Measured calm queried DTN again"))
    from fire_risk_modeler import landscape_acquisition
    monkeypatch.setattr(landscape_acquisition, "prepare_landscape", lambda *args: (
        preparations.append(args), landscape())[1])
    monkeypatch.setattr(broker, "invoke_model", lambda request, deadline: (
        calculations.append((request, deadline)), calculated(request))[1])
    answer = broker.enrich_answer(store, "owner", "thread", "attempt", source_content(calm=True), time.time()-1)
    job_id = answer["fireExposureJobs"][0]["job_id"]
    assert answer["fireExposureData"] == []
    assert jobs.execute(store, job_id, threading.Event())
    view = jobs.read_job(store, job_id, "owner")
    assert view.status == "completed"
    assert view.assessments[0].availability == "available"
    assert view.assessments[0].weather.wind_speed_mps == 0
    assert view.assessments[0].weather.wind_from_degrees is None
    assert preparations[0][0].latitude == POINT[0] and preparations[0][0].longitude == POINT[1]
    assert calculations[0][1] == float("inf")
    assert jobs.execute(store, job_id, threading.Event())
    assert len(preparations) == len(calculations) == 1
    assert store.get(f"budget:fire_risk_assessments:{budget_day()[0]}")["count"] == 1


def test_restart_reuses_sealed_input_and_once_charged_intent(tmp_path, monkeypatch):
    store = Store(tmp_path)
    job = queued(store)
    token = "first-worker"
    assert store.claim(job["id"], token)
    entry = job["entries"][0]
    weather = jobs.WeatherObservation.model_validate(entry["weather"])
    request = LandscapeAssessmentRequest(
        assessment_id=entry["assessment_id"], fire_ref=entry["fire_ref"], input_hash="0"*64,
        fire=entry["fire"], weather=weather, landscape=landscape(), scenario=jobs.scenario())
    request = request.model_copy(update={"input_hash": model_input_hash(request)})
    jobs.spend_once(store, job["id"], token, entry["assessment_id"], "fire_risk_assessments", 1000)
    jobs.input_artifact(store, job["id"], entry["assessment_id"], request.input_hash, request.model_dump(mode="json"))
    entry["landscape_input_hash"] = request.input_hash
    store.update(job["id"], entries=[entry], leaseUntil=0, weather_state="completed")
    from fire_risk_modeler import landscape_acquisition
    monkeypatch.setattr(landscape_acquisition, "prepare_landscape", lambda *_: pytest.fail("Restart fetched terrain"))
    monkeypatch.setattr(broker, "fetch_weather", lambda *_: pytest.fail("Restart fetched weather"))
    seen = []
    monkeypatch.setattr(broker, "invoke_model", lambda value, _: (seen.append(value), calculated(value))[1])
    assert jobs.execute(store, job["id"], threading.Event())
    assert seen == [request]
    assert store.get(f"budget:fire_risk_assessments:{budget_day()[0]}")["count"] == 1


def test_interrupted_weather_intent_never_bills_an_ambiguous_lambda_twice(tmp_path, monkeypatch):
    store = Store(tmp_path)
    job = queued(store, content(*POINT))
    store.update(job["id"], weather_state="invoking")
    monkeypatch.setattr(broker, "fetch_weather", lambda *_: pytest.fail("Ambiguous Lambda was repeated"))
    assert jobs.execute(store, job["id"], threading.Event())
    view = jobs.read_job(store, job["id"], "owner")
    assert view.status == "failed" and "interrupted" in view.error
    assert view.assessments == []


def test_missing_wind_has_typed_failure_and_no_substitute_geometry(tmp_path, monkeypatch):
    store = Store(tmp_path)
    job = queued(store, content(*POINT))
    monkeypatch.setattr(broker, "fetch_weather", lambda *_: [])
    monkeypatch.setattr(broker, "invoke_model", lambda *_: pytest.fail("Missing wind invoked the model"))
    assert jobs.execute(store, job["id"], threading.Event())
    result = jobs.read_job(store, job["id"], "owner").assessments[0]
    assert result.availability == "unavailable" and result.mode == "landscape"
    assert result.failure.code == "DTN_WIND_UNUSABLE"
    assert result.geojson.features == []
    assert result.fire.latitude == POINT[0] and result.fire.longitude == POINT[1]


def test_daily_charge_and_marker_are_atomic_and_lease_fenced(tmp_path):
    store = Store(tmp_path)
    job = queued(store)
    assert store.claim(job["id"], "worker")
    for _ in range(3):
        jobs.spend_once(store, job["id"], "worker", "weather", "fire_risk_weather", 1)
    assert store.get(f"budget:fire_risk_weather:{budget_day()[0]}")["count"] == 1
    with pytest.raises(IntegrationError, match="daily fire risk budget"):
        jobs.spend_once(store, job["id"], "worker", "other", "fire_risk_weather", 1)
    with pytest.raises(RuntimeError, match="lease lost"):
        jobs.spend_once(store, job["id"], "stale-worker", "third", "fire_risk_weather", 100)
    assert store.get(job["id"])["charges"] == {"weather": True}


def test_stale_worker_cannot_publish_and_reads_are_owned_expiring_provider_free(tmp_path, api_app, monkeypatch):
    store = Store(tmp_path)
    job = queued(store)
    assert store.claim(job["id"], "worker")
    entry = job["entries"][0]
    result = jobs.failed_result(entry, None, "DTN_WIND_UNUSABLE", "Fixture wind is unavailable.")
    with pytest.raises(RuntimeError, match="lease lost"):
        jobs.publish(store, job, "stale-worker", result)
    assert store.get("fire_exposure:"+result.assessment_id) is None
    jobs.publish(store, job, "worker", result)
    store.update(job["id"], assessment_ids=[result.assessment_id], status="completed", progress=1.0)
    monkeypatch.setattr(broker, "fetch_weather", lambda *_: pytest.fail("GET fetched weather"))
    monkeypatch.setattr(broker, "invoke_model", lambda *_: pytest.fail("GET invoked model"))
    path = "/api/v1/fire-exposure/jobs/"+job["id"]
    with client(api_app(store, "owner", broker.create_fire_exposure_router)) as browser:
        response = browser.get(path)
        assert response.status_code == 200
        assert response.json()["assessments"][0]["assessment_id"] == result.assessment_id
        assert "leaseToken" not in response.text and "charges" not in response.text and "owner" not in response.text
    with client(api_app(store, "other", broker.create_fire_exposure_router)) as browser:
        assert browser.get(path).status_code == 404
    store.update(job["id"], expires=0)
    with pytest.raises(IntegrationError, match="not found"):
        jobs.read_job(store, job["id"], "owner")


@pytest.mark.parametrize("status", ["queued", "running", "cancelling", "cancelled", "failed"])
def test_public_job_keeps_partial_results_private_until_completed(tmp_path, status):
    store = Store(tmp_path)
    job = queued(store)
    assert store.claim(job["id"], "worker")
    result = jobs.failed_result(job["entries"][0], None, "DTN_WIND_UNUSABLE", "Fixture missing wind.")
    jobs.publish(store, job, "worker", result)
    record = store.get("fire_exposure:"+result.assessment_id)
    store.update(job["id"], assessment_ids=[result.assessment_id], status=status)
    assert jobs.read_job(store, job["id"], "owner").assessments == []
    assert store.get("fire_exposure:"+result.assessment_id) == record and record["revision"] == 2
    store.update(job["id"], status="completed", progress=1.0)
    assert jobs.read_job(store, job["id"], "owner").assessments == [result]
