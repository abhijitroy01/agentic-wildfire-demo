"""Saved landscape requests and late workers cannot change an owned source observation."""
import gzip
import json
import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from botocore.exceptions import ClientError
from fire_risk_modeler.models import LandscapeAssessmentRequest, model_input_hash
from traffic_simulator.store import Store

from backend import fire_risk_broker as broker
from backend import fire_risk_jobs as jobs
from backend.evacuation.locations import reported_fire_receipt
from backend.tests.test_fire_risk_broker import content, current_weather
from backend.tests.test_fire_risk_jobs import POINT, calculated, landscape, queued, source_content


def checkpoint(store, job, change=None):
    entry = job["entries"][0]
    request = LandscapeAssessmentRequest(
        assessment_id=entry["assessment_id"],
        fire_ref=entry["fire_ref"],
        input_hash="0" * 64,
        fire=entry["fire"],
        weather=entry["weather"],
        landscape=landscape(),
        scenario=jobs.scenario(),
    )
    value = request.model_dump(mode="json")
    if change:
        change(value)
    request = LandscapeAssessmentRequest.model_validate(value)
    request = request.model_copy(update={"input_hash": model_input_hash(request)})
    jobs.input_artifact(store, job["id"], entry["assessment_id"], request.input_hash,
                        request.model_dump(mode="json"))
    entry["landscape_input_hash"] = request.input_hash
    store.update(job["id"], entries=[entry], weather_state="completed")
    return request


def watch_calculations(monkeypatch):
    from fire_risk_modeler import landscape_acquisition

    acquisitions, calculations = [], []
    monkeypatch.setattr(landscape_acquisition, "prepare_landscape",
                        lambda *args: (acquisitions.append(args), landscape())[1])
    monkeypatch.setattr(broker, "invoke_model",
                        lambda request, deadline: (calculations.append(request), calculated(request))[1])
    monkeypatch.setattr(broker, "fetch_weather", lambda *_: pytest.fail("Frozen wind was queried again"))
    return acquisitions, calculations


def assert_saved_input_rejected(store, job):
    view = jobs.read_job(store, job["id"], "owner")
    assert view.status in ("completed", "failed")
    assert not any(result.availability == "available" for result in view.assessments)
    if view.assessments:
        assert view.assessments[0].failure.code == "LANDSCAPE_INPUT_INVALID"
    else:
        assert "input" in (view.error or "").lower()


def test_missing_frozen_input_fails_without_acquiring_replacement(tmp_path, monkeypatch):
    store = Store(tmp_path)
    job = queued(store)
    request = checkpoint(store, job)
    artifact = store.root / (
        f"fire-risk/inputs/{job['id']}/{job['entries'][0]['assessment_id']}/{request.input_hash}.json.gz"
    )
    artifact.unlink()
    acquisitions, calculations = watch_calculations(monkeypatch)
    assert jobs.execute(store, job["id"], threading.Event())
    assert acquisitions == calculations == []
    assert_saved_input_rejected(store, job)


@pytest.mark.parametrize("field", ["assessment_id", "fire_ref", "fire", "weather", "scenario"])
def test_valid_but_substituted_checkpoint_cannot_change_answer_source(tmp_path, monkeypatch, field):
    store = Store(tmp_path)
    job = queued(store)

    def substitute(value):
        if field == "assessment_id":
            value[field] = str(uuid4())
        elif field == "fire_ref":
            value[field] = "unrequested-fire"
        elif field == "fire":
            value[field]["latitude"] += 0.001
        elif field == "weather":
            value[field]["wind_speed_mps"] += 1
        else:
            value[field]["moisture_profile"] = (
                "low" if value[field]["moisture_profile"] != "low" else "high"
            )

    checkpoint(store, job, substitute)
    acquisitions, calculations = watch_calculations(monkeypatch)
    assert jobs.execute(store, job["id"], threading.Event())
    assert acquisitions == calculations == []
    assert_saved_input_rejected(store, job)


def test_checkpoint_contents_must_match_hash_address_before_model_invocation(tmp_path, monkeypatch):
    store = Store(tmp_path)
    job = queued(store)
    request = checkpoint(store, job)
    corrupted = request.model_dump(mode="json")
    corrupted["scenario"]["horizons_minutes"] = [10.0, 20.0, 40.0]
    artifact = store.root / (
        f"fire-risk/inputs/{job['id']}/{job['entries'][0]['assessment_id']}/{request.input_hash}.json.gz"
    )
    artifact.write_bytes(gzip.compress(json.dumps(corrupted).encode()))
    acquisitions, calculations = watch_calculations(monkeypatch)
    assert jobs.execute(store, job["id"], threading.Event())
    assert acquisitions == calculations == []
    assert_saved_input_rejected(store, job)


@pytest.mark.parametrize("interruption", ["cancel", "expired", "new_worker"])
def test_interruption_fences_late_assessment_publication(tmp_path, interruption):
    store = Store(tmp_path)
    job = queued(store)
    request = checkpoint(store, job)
    assert store.claim(job["id"], "worker")
    if interruption == "cancel":
        store.cancel_run(job["id"])
    else:
        store.update(job["id"], leaseUntil=0)
        if interruption == "new_worker":
            assert store.claim(job["id"], "replacement-worker")
    with pytest.raises(RuntimeError, match="lease lost"):
        jobs.publish(store, job, "worker", calculated(request))
    assert store.get("fire_exposure:" + request.assessment_id) is None
    if interruption == "cancel":
        assert store.get(job["id"])["status"] == "cancelling"


def test_completed_landscape_source_can_anchor_incident_without_provider_work(tmp_path, monkeypatch):
    store = Store(tmp_path)
    job = queued(store)
    request = checkpoint(store, job)
    assert store.claim(job["id"], "worker")
    jobs.publish(store, job, "worker", calculated(request))
    monkeypatch.setattr(broker, "fetch_weather", lambda *_: pytest.fail("Handoff queried DTN"))
    monkeypatch.setattr(broker, "invoke_model", lambda *_: pytest.fail("Handoff invoked a model"))
    source = reported_fire_receipt(store, "owner", request.assessment_id)
    assert source["lat"] == request.fire.latitude
    assert source["lon"] == request.fire.longitude
    assert source["source_id"] == request.fire.source_id
    assert source["source_assessment_id"] == request.assessment_id
    assert source["score"] is None


def test_queued_wind_is_rechecked_for_freshness_before_first_freeze(tmp_path, monkeypatch):
    store = Store(tmp_path)
    job = queued(store)
    entry = job["entries"][0]
    old_time = (datetime.now(UTC) - timedelta(seconds=10000)).isoformat()
    entry["weather"]["observed_at"] = old_time
    store.update(job["id"], entries=[entry])
    acquisitions, calculations = watch_calculations(monkeypatch)
    calls = []

    def readings(fires, deadline, operation_id):
        calls.append((fires, operation_id))
        return [{"latitude": POINT[0], "longitude": POINT[1], "currentWeather": current_weather(*POINT)}]

    monkeypatch.setattr(broker, "fetch_weather", readings)
    assert jobs.execute(store, job["id"], threading.Event())
    assert len(calls) == len(acquisitions) == len(calculations) == 1
    assert calculations[0].weather.observed_at != old_time
    assert calls[0][0][0].latitude == POINT[0] and calls[0][0][0].longitude == POINT[1]


def test_frozen_wind_and_scenario_do_not_change_after_queue_restart_or_settings_change(tmp_path, monkeypatch):
    store = Store(tmp_path)
    job = queued(store)
    old_time = (datetime.now(UTC) - timedelta(seconds=10000)).isoformat()
    job["entries"][0]["weather"]["observed_at"] = old_time
    store.update(job["id"], entries=job["entries"])
    request = checkpoint(store, job)
    acquisitions, calculations = watch_calculations(monkeypatch)
    replacement = "low" if request.scenario.moisture_profile != "low" else "high"
    monkeypatch.setattr(broker.api_settings(), "fire_risk_moisture_profile", lambda: replacement)
    assert jobs.execute(store, job["id"], threading.Event())
    assert acquisitions == [] and calculations == [request]
    result = jobs.read_job(store, job["id"], "owner").assessments[0]
    assert result.weather.observed_at == old_time
    assert result.scenario == request.scenario and result.input_hash == request.input_hash


def test_scenario_is_frozen_at_enqueue_even_before_raster_acquisition(tmp_path, monkeypatch):
    store = Store(tmp_path)
    job = queued(store)
    original = job["entries"][0]["scenario"]
    acquisitions, calculations = watch_calculations(monkeypatch)
    replacement = "low" if original["moisture_profile"] != "low" else "high"
    monkeypatch.setattr(broker.api_settings(), "fire_risk_moisture_profile", lambda: replacement)
    assert jobs.execute(store, job["id"], threading.Event())
    assert len(acquisitions) == len(calculations) == 1
    assert calculations[0].scenario.model_dump(mode="json") == original


def test_failed_missing_point_dtn_batch_preserves_already_measured_sibling(tmp_path, monkeypatch):
    store = Store(tmp_path)
    answer = source_content()
    second = content(POINT[0] + 0.002, POINT[1])["mapData"]["locations"][0]
    second["fire"]["sourceId"] = "esri:IRWINID:second-fixture"
    answer["mapData"]["locations"].append(second)
    job = queued(store, answer)
    original_wind = job["entries"][0]["weather"]
    acquisitions, calculations = watch_calculations(monkeypatch)
    calls = []

    def unavailable(fires, deadline, operation_id):
        calls.append(fires)
        raise ValueError("fixture-secret upstream response")

    monkeypatch.setattr(broker, "fetch_weather", unavailable)
    assert jobs.execute(store, job["id"], threading.Event())
    assert len(calls) == 1 and len(calls[0]) == 1
    assert calls[0][0].latitude == second["latitude"]
    assert len(acquisitions) == len(calculations) == 1
    assert calculations[0].weather.model_dump(mode="json") == original_wind
    results = jobs.read_job(store, job["id"], "owner").assessments
    assert results[0].availability == "available"
    assert results[1].availability == "unavailable" and results[1].failure.code == "DTN_WIND_UNUSABLE"
    assert results[1].geojson.features == []
    assert "fixture-secret" not in json.dumps([result.model_dump(mode="json") for result in results])
    assert jobs.execute(store, job["id"], threading.Event())
    assert len(calls) == 1


@pytest.mark.parametrize("stage,code", [
    ("landscape", "LANDSCAPE_REQUEST_FAILED"),
    ("model", "CALCULATION_FAILED"),
])
def test_unexpected_transport_failure_is_typed_per_fire_and_preserves_source_handoff(
    tmp_path, monkeypatch, stage, code,
):
    from fire_risk_modeler import landscape_acquisition

    store = Store(tmp_path)
    answer = source_content()
    second = content(POINT[0] + 0.002, POINT[1])["mapData"]["locations"][0]
    second["fire"]["sourceId"] = "esri:IRWINID:second-fixture"
    second["currentWeather"] = current_weather(second["latitude"], second["longitude"])
    answer["mapData"]["locations"].append(second)
    job = queued(store, answer)

    def prepare(fire, radius, limits):
        if stage == "landscape" and fire.latitude == POINT[0]:
            raise httpx.ConnectError("fixture-secret upstream URL")
        return landscape()

    def invoke(request, deadline):
        if stage == "model" and request.fire.latitude == POINT[0]:
            raise httpx.ReadError("fixture-secret upstream response")
        return calculated(request)

    monkeypatch.setattr(landscape_acquisition, "prepare_landscape", prepare)
    monkeypatch.setattr(broker, "invoke_model", invoke)
    monkeypatch.setattr(broker, "fetch_weather", lambda *_: pytest.fail("Measured sibling wind was queried again"))
    assert jobs.execute(store, job["id"], threading.Event())
    results = jobs.read_job(store, job["id"], "owner").assessments
    assert len(results) == 2
    assert results[0].availability == "unavailable" and results[0].failure.code == code
    assert results[0].geojson.features == []
    assert results[1].availability == "available"
    assert "fixture-secret" not in json.dumps([result.model_dump(mode="json") for result in results])
    source = reported_fire_receipt(store, "owner", results[0].assessment_id)
    assert source["lat"] == POINT[0] and source["lon"] == POINT[1]


@pytest.mark.parametrize("interruption", ["stop", "cancel"])
def test_interrupted_weather_delivery_remains_ambiguous_and_never_bills_again(
    tmp_path, monkeypatch, interruption,
):
    store = Store(tmp_path)
    job = queued(store, content(*POINT))
    stop, calls = threading.Event(), []

    def interrupt(module, operation, payload, maximum_bytes, check_cancel):
        assert operation == "weather"
        calls.append(payload["operation_id"])
        if interruption == "stop":
            stop.set()
        else:
            store.cancel_run(job["id"])
        check_cancel()
        pytest.fail("Interrupted transport was allowed to continue")

    monkeypatch.setattr(jobs, "run_transport", interrupt)
    jobs.execute(store, job["id"], stop)
    current = store.get(job["id"])
    assert current["weather_state"] == "invoking"
    assert current["entries"][0]["weather"] is None
    assert jobs.read_job(store, job["id"], "owner").assessments == []
    assert store.get("fire_exposure:" + job["entries"][0]["assessment_id"]) is None
    assert len(calls) == 1
    if interruption == "stop":
        store.update(job["id"], leaseUntil=0)
        assert jobs.execute(store, job["id"], threading.Event())
        view = jobs.read_job(store, job["id"], "owner")
        assert view.status == "failed" and "interrupted" in view.error
        assert len(calls) == 1
    else:
        assert current["status"] in ("cancelling", "cancelled")


def test_failed_lease_read_interrupts_local_transport_and_marks_ownership_uncertain(tmp_path, monkeypatch):
    store = Store(tmp_path)
    job = queued(store)
    assert store.claim(job["id"], "worker")
    lost = threading.Event()
    check = jobs.cancellation_check(store, job["id"], "worker", lost=lost)

    def unreadable(_identifier):
        raise ClientError({"Error": {"Code": "InternalServerError", "Message": "fixture-secret"}},
                          "GetItem")

    monkeypatch.setattr(store, "get", unreadable)
    with pytest.raises(jobs.PreparationInterrupted, match="Lease authority") as failure:
        check()
    assert lost.is_set() and "fixture-secret" not in str(failure.value)


def test_failed_lease_read_during_weather_preserves_invoking_without_provider_failure(tmp_path, monkeypatch):
    store = Store(tmp_path)
    job = queued(store, content(*POINT))
    assert store.claim(job["id"], "worker")
    original_get, reads, calls = store.get, [], []

    def transport(_module, operation, payload, _maximum_bytes, check_cancel):
        assert operation == "weather"
        calls.append(payload["operation_id"])
        reads.append("fail-next")
        check_cancel()
        pytest.fail("A transport with uncertain ownership must not return provider output")

    def temporarily_unreadable(identifier):
        if identifier == job["id"] and reads == ["fail-next"]:
            reads.append("failed")
            raise ClientError({"Error": {"Code": "InternalServerError", "Message": "fixture-secret"}},
                              "GetItem")
        return original_get(identifier)

    monkeypatch.setattr(store, "get", temporarily_unreadable)
    monkeypatch.setattr(jobs, "run_transport", transport)
    with pytest.raises(jobs.PreparationInterrupted):
        jobs.frozen_weather(store, original_get(job["id"]), "worker")
    current = original_get(job["id"])
    assert current["weather_state"] == "invoking" and current["entries"][0]["weather"] is None
    assert "weather_error" not in current["entries"][0]
    assert current["charges"]["weather"] and len(calls) == 1
    with pytest.raises(jobs.IntegrationError, match="interrupted"):
        jobs.frozen_weather(store, current, "worker")
    assert len(calls) == 1
