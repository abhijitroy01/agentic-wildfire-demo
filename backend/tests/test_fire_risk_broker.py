"""Source identity, one-time enrichment, retained reads and completed chat authority."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import boto3
import pytest
from fire_risk_modeler.exposure import assess_exposure
from fire_risk_modeler.models import ExposureAssessmentRequest, FireObservation, model_input_hash
from traffic_simulator import settings as simulator_settings
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import budget_day, commit
from traffic_simulator.store import Store

from backend import fire_risk_broker as broker
from backend import fire_risk_jobs as jobs
from backend import runtime_client
from backend.tests.builders import client


def content(latitude=0.0, longitude=179.99):
    return {"response": "Observed fire", "location": {"latitude": 47.6, "longitude": -122.3},
            "mapData": {"locations": [{
                "latitude": latitude, "longitude": longitude,
                "fire": {"name": "Fixture fire", "sourceId": "esri:IRWINID:fixture"}}]}}


def current_weather(latitude=0.0, longitude=179.99, *, age=0):
    return {"queryLocation": {"latitude": latitude, "longitude": longitude},
            "timestamp": (datetime.now(UTC)-timedelta(seconds=age)).isoformat(),
            "wind": {"speed": {"value": 10, "unit": "mph"},
                     "direction": {"value": 270, "unit": "degrees"}},
            "temperature": {"value": 68, "unit": "F"},
            "relativeHumidity": {"value": 30, "unit": "percent"}}


def request():
    value = ExposureAssessmentRequest(
        assessment_id=str(uuid4()), fire_ref="observed", input_hash="0"*64,
        fire=FireObservation(latitude=0.0, longitude=179.99), parameters=broker.parameters())
    return value.model_copy(update={"input_hash": model_input_hash(value)})


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.delenv("WEATHER_FUNCTION_ARN", raising=False)
    monkeypatch.delenv("FIRE_RISK_SERVICE_URL", raising=False)
    monkeypatch.delenv("FIRE_RISK_SERVICE_KEY", raising=False)


def test_actual_fire_point_zero_and_dateline_are_used_and_search_center_is_excluded():
    selected = broker.observed_fires(content())
    assert (selected[0][1].latitude, selected[0][1].longitude) == (0.0, 179.99)
    assert selected[0][1].source_id == "esri:IRWINID:fixture"
    assert broker.observed_fires({"location": {"latitude": 47.6, "longitude": -122.3}}) == []
    assert broker.distance_km((0, 179.99), (0, -179.99)) < 3


@pytest.mark.parametrize("coordinate", [float("nan"), float("inf"), True, 91, "0"])
def test_invalid_observed_fire_coordinates_are_excluded(coordinate):
    assert broker.observed_fires(content(latitude=coordinate)) == []


def test_weather_normalization_uses_units_freshness_and_point_provenance():
    fire = request().fire
    weather = broker.normalize_weather(current_weather(), fire)
    assert weather.availability == "available"
    assert weather.wind_speed_mps == pytest.approx(4.4704)
    assert weather.wind_from_degrees == 270
    assert weather.temperature_c == 20
    assert weather.relative_humidity_percent == 30
    assert broker.normalize_weather(current_weather(age=10000), fire).availability == "stale"
    assert broker.normalize_weather(current_weather(latitude=47.6, longitude=-122.3), fire) is None
    assert broker.normalize_weather({**current_weather(), "queryLocation": {}}, fire) is None
    invalid = current_weather()
    invalid["wind"]["speed"]["unit"] = "furlongs"
    assert broker.normalize_weather(invalid, fire).wind_speed_mps is None


def test_weather_batch_invokes_dtn_in_configured_region_with_bounded_io(monkeypatch):
    function = "arn:aws:lambda:eu-west-1:123456789012:function:fixture-weather"
    readings = [{"latitude": 0.0, "longitude": 179.99, "currentWeather": current_weather()}]
    body = Mock()
    body.read.return_value = json.dumps({"locations": readings}).encode()
    weather_client = Mock()
    weather_client.invoke.return_value = {"Payload": body}
    sdk_client = Mock(return_value=weather_client)
    monkeypatch.setattr(boto3, "client", sdk_client)
    monkeypatch.setattr(simulator_settings, "REGION", "eu-west-1")
    monkeypatch.setenv("WEATHER_FUNCTION_ARN", function)
    monkeypatch.setattr(broker.time, "time", lambda: 100.0)
    fires = [FireObservation(latitude=0.0, longitude=179.99),
             FireObservation(latitude=-33.9, longitude=151.2)]

    assert broker.fetch_weather(fires, 102.0) == readings
    args, options = sdk_client.call_args
    assert args == ("lambda",) and options["region_name"] == "eu-west-1"
    config = options["config"]
    assert config.connect_timeout == config.read_timeout == 2.0
    assert config.retries == {"total_max_attempts": 1}
    weather_client.invoke.assert_called_once_with(
        FunctionName=function, InvocationType="RequestResponse", Payload=json.dumps({"locations": [
            {"latitude": 0.0, "longitude": 179.99}, {"latitude": -33.9, "longitude": 151.2},
        ]}).encode())
    body.read.assert_called_once_with(int(broker.api_settings().fire_risk_limit("response_max_kib"))*1024+1)
    body.close.assert_called_once_with()


def test_same_answer_returns_one_durable_reference_without_repeating_weather_or_model(
        tmp_path, isolated, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(broker, "invoke_model", lambda *_: pytest.fail("Enqueue ran a model"))
    monkeypatch.setattr(broker, "fetch_weather", lambda *_: pytest.fail("Enqueue fetched weather"))
    first = broker.enrich_answer(store, "owner", "thread", "attempt", content(), time.time()-1)
    second = broker.enrich_answer(store, "owner", "thread", "attempt", content(), time.time()+10)
    assert first["fireExposureData"] == second["fireExposureData"] == []
    assert first["fireExposureJobs"] == second["fireExposureJobs"]
    job = store.get(first["fireExposureJobs"][0]["job_id"])
    assert job["status"] == "queued"
    assert len(store.list("owner")) == 1
    assert job["entries"][0]["fire"]["latitude"] == 0
    assert job["entries"][0]["fire"]["longitude"] == 179.99


def test_batch_weather_matches_each_requested_fire_even_when_fires_are_nearby(
        tmp_path, isolated, monkeypatch):
    answer = content(latitude=47.6, longitude=-122.3)
    second = {"latitude": 47.6, "longitude": -122.31,
              "fire": {"name": "Second fixture fire", "sourceId": "esri:IRWINID:second"}}
    answer["mapData"]["locations"].append(second)
    readings = [
        {"latitude": 47.6, "longitude": -122.31, "currentWeather": current_weather(47.6, -122.31)},
        {"latitude": 47.6, "longitude": -122.3, "currentWeather": current_weather(47.6, -122.3)},
    ]
    readings[0]["currentWeather"]["wind"]["speed"]["value"] = 20
    monkeypatch.setattr(broker.api_settings(), "weather_function", lambda: "configured-fixture")
    monkeypatch.setattr(broker, "fetch_weather", lambda *_: readings)
    monkeypatch.setattr(broker, "invoke_model", lambda value, _: (
        broker.build_unavailable_result(value, ["Fixture scenario"]) if value.mode == "surface_fire"
        else assess_exposure(value)))
    store = Store(tmp_path)
    refs = broker.enrich_answer(store, "owner", "thread", "batch", answer, time.time()+10)["fireExposureJobs"]
    job = store.get(refs[0]["job_id"])
    assert store.claim(job["id"], "fixture-worker")
    entries = jobs.frozen_weather(store, job, "fixture-worker")
    by_longitude = {entry["fire"]["longitude"]: entry["weather"] for entry in entries}
    assert by_longitude[-122.3]["wind_speed_mps"] == pytest.approx(4.4704)
    assert by_longitude[-122.31]["wind_speed_mps"] == pytest.approx(8.9408)
    assert all(longitude == weather["query_longitude"] for longitude, weather in by_longitude.items())


def test_retained_read_is_owned_expiring_and_has_no_external_calls(tmp_path, isolated, monkeypatch, api_app):
    store = Store(tmp_path)
    result = broker.assess_once(store, "owner", request(), time.time()+10)
    monkeypatch.setattr(broker, "invoke_model", lambda *_: pytest.fail("A saved read ran a model"))
    monkeypatch.setattr(broker, "fetch_weather", lambda *_: pytest.fail("A saved read fetched weather"))
    path = "/api/v1/fire-exposure/assessments/"+result["assessment_id"]
    with client(api_app(store, "owner", broker.create_fire_exposure_router)) as browser:
        assert browser.get(path).json() == result
    with client(api_app(store, "other", broker.create_fire_exposure_router)) as browser:
        assert browser.get(path).status_code == 404
    receipt = store.get("fire_exposure:"+result["assessment_id"])
    receipt.update(revision=3, expires=0)
    commit(store, [(receipt, 2)])
    with client(api_app(store, "owner", broker.create_fire_exposure_router)) as browser:
        assert browser.get(path).status_code == 404


def test_concurrent_duplicate_intents_spend_and_invoke_once(tmp_path, isolated, monkeypatch):
    store, value, calls = Store(tmp_path), request(), []
    def invoke(value, _):
        calls.append(value.assessment_id)
        time.sleep(0.03)
        return assess_exposure(value)
    monkeypatch.setattr(broker, "invoke_model", invoke)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: broker.assess_once(store, "owner", value, time.time()+10), range(4)))
    assert calls == [value.assessment_id]
    assert store.get(f"budget:fire_risk_assessments:{budget_day()[0]}")["count"] == 1
    assert store.get("fire_exposure:"+value.assessment_id)["revision"] == 2


def test_expired_queued_assessment_does_not_spend_or_call(tmp_path, isolated, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(broker, "invoke_model", lambda *_: pytest.fail("Expired assessment called the model"))
    result = broker.assess_once(store, "owner", request(), time.time()-1)
    assert result["availability"] == "unavailable"
    assert store.get(f"budget:fire_risk_assessments:{budget_day()[0]}") is None


def test_same_intent_with_changed_hash_is_refused_without_new_work(tmp_path, isolated, monkeypatch):
    store, value = Store(tmp_path), request()
    broker.assess_once(store, "owner", value, time.time()-1)
    monkeypatch.setattr(broker, "invoke_model", lambda *_: pytest.fail("Conflicting intent calculated again"))
    with pytest.raises(IntegrationError, match="Assessment inputs changed"):
        broker.assess_once(store, "owner", value.model_copy(update={"input_hash": "f"*64}), time.time()+10)
    assert store.get(f"budget:fire_risk_assessments:{budget_day()[0]}") is None


def test_retained_result_does_not_duplicate_request_and_oversize_is_unavailable(
        tmp_path, isolated, monkeypatch):
    store, value = Store(tmp_path), request()
    # A large model representation is valid JSON but must never enter a DynamoDB item unchanged.
    result = assess_exposure(value)
    result.assumptions = ["x"*400000]
    monkeypatch.setattr(broker, "invoke_model", lambda *_: result)
    saved_result = broker.assess_once(store, "owner", value, time.time()+10)
    receipt = store.get("fire_exposure:"+value.assessment_id)
    assert "request" not in receipt
    assert receipt["parameters"] == value.parameters.model_dump(mode="json")
    assert broker.retained_size(receipt) <= broker.MAX_RETAINED_BYTES
    assert saved_result["availability"] == "unavailable"
    assert (saved_result["fire"]["latitude"], saved_result["fire"]["longitude"]) == (0, 179.99)


def test_missing_or_failing_model_keeps_source_fire(tmp_path, isolated, monkeypatch):
    def fail(*_):
        raise RuntimeError("secret-provider-details")
    monkeypatch.setattr(broker, "invoke_model", fail)
    answer = broker.enrich_answer(Store(tmp_path), "owner", "thread", "attempt", content(), time.time()+10)
    assert answer["mapData"] == content()["mapData"]
    assert answer["fireExposureData"] == []
    assert len(answer["fireExposureJobs"]) == 1
    assert "secret-provider-details" not in json.dumps(answer)


def test_late_calculation_is_fenced_by_terminal_receipt(tmp_path, isolated, monkeypatch):
    store = Store(tmp_path)
    def slow(value, _):
        time.sleep(0.08)
        return assess_exposure(value)
    monkeypatch.setattr(broker, "invoke_model", slow)
    answer = broker.enrich_answer(store, "owner", "thread", "attempt", content(), time.time()+0.02)
    time.sleep(0.12)
    assert answer["fireExposureData"] == []
    job = store.get(answer["fireExposureJobs"][0]["job_id"])
    assert job["status"] == "queued" and job["assessment_ids"] == []


def test_final_chat_snapshot_has_server_assessments_before_run_finished(
        demo, isolated, monkeypatch, signing_secret):
    snapshots = [
        {"type": "STATE_SNAPSHOT", "snapshot": {"content": {
            **content(), "fireExposureData": [{"forged": True}],
            "fireExposureError": {"code": "RISK_PREPARATION_FAILED", "message": "forged-error"},
            "fireExposureJobs": [{"job_id": "forged-job"}]}}},
        {"type": "RUN_FINISHED"}]
    monkeypatch.setattr(runtime_client, "runtime_events", lambda *_: iter(snapshots))
    with client(demo[0]) as browser:
        browser.post("/api/v1/demo/session")
        response = browser.post("/api/v1/investigation/turns", json={
            "question": "Show fires here.", "thread_id": "fixture", "context_envelope": {}})
    frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert frames[-1]["type"] == "RUN_FINISHED"
    final = frames[-2]["snapshot"]["content"]
    assert final["fireExposureData"] == []
    assert len(final["fireExposureJobs"]) == 1
    assert demo[1].get(final["fireExposureJobs"][0]["job_id"])["status"] == "queued"
    assert "forged" not in response.text
    assert "fireExposureError" not in final
    assert final["mapData"] == content()["mapData"]


def test_failed_terrain_enqueue_retains_source_and_exposes_only_fixed_public_error(
        demo, isolated, monkeypatch, signing_secret):
    from backend import fire_risk_jobs
    from backend.fire_risk_contracts import FireRiskPreparationError

    snapshots = [
        {"type": "STATE_SNAPSHOT", "snapshot": {"content": {
            **content(), "fireExposureError": {"message": "forged-error"}}}},
        {"type": "RUN_FINISHED"}]
    monkeypatch.setattr(runtime_client, "runtime_events", lambda *_: iter(snapshots))

    def unavailable(*_args):
        raise RuntimeError("secret-upstream-response-and-url")

    monkeypatch.setattr(fire_risk_jobs, "enqueue", unavailable)
    with client(demo[0]) as browser:
        browser.post("/api/v1/demo/session")
        response = browser.post("/api/v1/investigation/turns", json={
            "question": "Show fires here.", "thread_id": "fixture", "context_envelope": {}})
    frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert frames[-1]["type"] == "RUN_FINISHED"
    final = frames[-2]["snapshot"]["content"]
    assert final["fireExposureError"] == FireRiskPreparationError.unavailable().model_dump(mode="json")
    assert final["fireExposureData"] == final["fireExposureJobs"] == []
    assert final["mapData"] == content()["mapData"]
    assert "forged-error" not in response.text and "secret-upstream" not in response.text


def test_successful_server_enqueue_clears_prior_untrusted_handoff_error(tmp_path, isolated):
    answer = broker.enrich_answer(Store(tmp_path), "owner", "thread", "attempt",
                                 {**content(), "fireExposureError": {"message": "forged-error"}}, time.time()+10)
    assert "fireExposureError" not in answer and len(answer["fireExposureJobs"]) == 1
