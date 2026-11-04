"""Acceptance reads saved background jobs; synthetic DTO fixtures never invoke a source or native model."""
import builtins
import copy
import json
from pathlib import Path

import httpx
import pytest

from deployment.acceptance import background_jobs as checks
from deployment.acceptance.visitor import AcceptanceError

ROOT = Path(__file__).resolve().parents[2]
JOB = "11111111-1111-4111-8111-111111111111"
ASSESSMENT = "22222222-2222-4222-8222-222222222222"
OTHER_JOB = "33333333-3333-4333-8333-333333333333"
REFERENCE = {"job_id": JOB, "fire_refs": ["fixture-fire-ref"]}
AREA = {"lat": 47.60395, "lon": -122.3305, "radius_km": 2}


def terrain(*, available=True, no_growth=False):
    result = json.loads((ROOT / "backend/tests/data/fire-risk-protocol.json").read_text())
    result.update(assessment_id=ASSESSMENT, fire_ref=REFERENCE["fire_refs"][0], input_hash="a"*64,
                  assumptions=["Synthetic acceptance DTO fixture; no source or native work."])
    result["fire"].update(source_id="esri:fixture", source_timestamp="2026-10-27T12:00:00+00:00")
    if not available:
        result.update(availability="unavailable", landscape=None, weather=None, geojson={
            "type": "FeatureCollection", "features": []}, failure={
                "code": "LANDSCAPE_COVERAGE_MISSING", "message": "Offline fixture has no landscape."})
    elif no_growth:
        result["weather"].update(wind_speed_mps=0, wind_from_degrees=None)
        result["landscape"].update(reached_cells=1, spread_cells=0)
        shape = copy.deepcopy(result["geojson"]["features"][0]["geometry"])
        for feature in result["geojson"]["features"]:
            feature["geometry"] = copy.deepcopy(shape)
            feature["properties"]["wind_from_degrees"] = None
    return result


def answer(result=None):
    fire = (result or terrain())["fire"]
    return {"mapData": {"locations": [{
        "latitude": fire["latitude"], "longitude": fire["longitude"],
        "fire": {"name": fire["name"], "sourceId": fire["source_id"], "lastUpdated": fire["source_timestamp"]}},
        {"latitude": AREA["lat"], "longitude": AREA["lon"], "name": "Seattle search area"}]},
        "fireExposureJobs": [copy.deepcopy(REFERENCE)], "fireExposureData": []}


def job(status="running", assessments=None):
    return {**copy.deepcopy(REFERENCE), "status": status, "phase": "Synthetic terrain fixture",
            "progress": 1.0 if status == "completed" else 0.25,
            "assessments": assessments if assessments is not None else [], "error": None}


def fake_terrain_client(states, result, sent):
    iterator = iter(states)

    def reply(request):
        sent.append(request)
        if request.url.path == f"/fire-exposure/jobs/{JOB}":
            return httpx.Response(200, json=next(iterator))
        if request.url.path == f"/fire-exposure/assessments/{ASSESSMENT}":
            return httpx.Response(200, json=result)
        pytest.fail(f"Unexpected work request: {request.method} {request.url.path}")
    return httpx.Client(base_url="https://workspace.example/", transport=httpx.MockTransport(reply))


def test_terrain_rejoins_slow_saved_work_without_deadline_or_new_post(monkeypatch):
    result, sent, pauses, saved = terrain(no_growth=True), [], [], []
    content = answer(result)
    original = copy.deepcopy(content)
    monkeypatch.setattr(checks.time, "sleep", pauses.append)
    monkeypatch.setattr(checks.time, "monotonic", lambda: pytest.fail("An arbitrary job deadline was consulted"))
    states = [job("queued"), *[job("running") for _ in range(40)], job("cancelling"),
              job("completed", [result])]
    with fake_terrain_client(states, result, sent) as client:
        report = checks.wait_terrain_jobs(client, content, require_current=True,
                                         checkpoint=lambda value: saved.append(copy.deepcopy(value)))
    assert report["status"] == "completed" and report["available"] == 1 and report["unavailable"] == 0
    outcome = report["jobs"][0]["assessments"][0]
    assert outcome["outcome"] == "no_growth" and outcome["weather"]["wind_speed_mps"] == 0
    assert outcome["weather"]["wind_from_degrees"] is None
    assert outcome["horizons"] == [15, 30, 60]
    assert len(set(outcome["geometry_sha256"])) == 1
    assert content == original
    assert len(pauses) == 42 and all(value == checks.POLL_SECONDS for value in pauses)
    assert all(request.method == "GET" and request.extensions["timeout"]["read"] == checks.TIMEOUT_SECONDS
               for request in sent)
    assert any(entry["jobs"][0]["status"] == "cancelling" for entry in saved)


@pytest.mark.parametrize("status", ["queued", "running", "cancelling", "failed", "cancelled"])
def test_unfinished_or_failed_job_cannot_expose_partial_success(status):
    reference = checks.contracts()[0].model_validate(REFERENCE)
    with pytest.raises(RuntimeError, match="provisional success"):
        checks.terrain_receipt(job(status, [terrain()]), reference, checks.observed_fires(answer()))


@pytest.mark.parametrize("defect", ["job", "refs", "duplicate", "missing", "extra"])
def test_terrain_results_cover_exactly_the_saved_intent(defect):
    reference = checks.contracts()[0].model_validate(REFERENCE)
    raw = job("completed", [terrain()])
    if defect == "job":
        raw["job_id"] = OTHER_JOB
    elif defect == "refs":
        raw["fire_refs"] = ["another-fire"]
    elif defect == "duplicate":
        raw["assessments"].append(copy.deepcopy(raw["assessments"][0]))
    elif defect == "missing":
        raw["assessments"] = []
    else:
        raw["assessments"][0]["fire_ref"] = "another-fire"
    with pytest.raises(RuntimeError, match="different saved job|exactly the saved"):
        checks.terrain_receipt(raw, reference, checks.observed_fires(answer()))


@pytest.mark.parametrize("field,value", [
    ("latitude", 47.60395), ("longitude", -122.3305), ("source_id", "another-esri"),
    ("source_timestamp", "2026-10-27T13:00:00Z"), ("name", "Another fire"),
])
def test_terrain_cannot_rebind_to_a_city_another_fire_or_new_observation(field, value):
    result = terrain()
    result["fire"][field] = value
    with pytest.raises(RuntimeError, match="original source fire"):
        checks.terrain_outcome(result, checks.observed_fires(answer()))


@pytest.mark.parametrize("defect", ["wind_missing", "bearing_missing", "horizons", "cell_count", "no_growth_shape"])
def test_available_terrain_requires_honest_wind_growth_and_grid_receipts(defect):
    result = terrain(no_growth=defect == "no_growth_shape")
    if defect == "wind_missing":
        result["weather"]["wind_speed_mps"] = None
    elif defect == "bearing_missing":
        result["weather"]["wind_from_degrees"] = None
    elif defect == "horizons":
        result["geojson"]["features"].pop()
    elif defect == "cell_count":
        result["landscape"]["spread_cells"] += 1
    else:
        result["geojson"]["features"][1]["geometry"]["coordinates"][0][1][0] += 0.0001
    with pytest.raises(RuntimeError, match="measured wind|horizons|cell accounting|no-growth"):
        checks.terrain_outcome(result, checks.observed_fires(answer()))


def test_typed_unavailable_is_retained_without_claiming_spread_or_safe_land(monkeypatch):
    result, sent = terrain(available=False), []
    monkeypatch.setattr(checks.time, "sleep", lambda _: pytest.fail("Terminal result was polled again"))
    with fake_terrain_client([job("completed", [result])], result, sent) as client:
        report = checks.wait_terrain_jobs(client, answer(result), require_current=True)
    outcome = report["jobs"][0]["assessments"][0]
    assert report["available"] == 0 and report["unavailable"] == 1
    assert outcome["outcome"] == "unavailable" and outcome["horizons"] == []
    assert outcome["failure"]["code"] == "LANDSCAPE_COVERAGE_MISSING"
    assert "landscape" not in outcome and len(sent) == 2


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_terminal_failed_or_cancelled_jobs_keep_the_actual_failure(monkeypatch, terminal):
    sent, saved = [], []
    with (fake_terrain_client([job(terminal)], terrain(), sent) as client,
          pytest.raises(RuntimeError, match=f"reached {terminal}")):
        checks.wait_terrain_jobs(client, answer(), checkpoint=lambda value: saved.append(copy.deepcopy(value)))
    assert saved[-1]["status"] == terminal and len(sent) == 1
    assert "assessments" not in saved[-1]["jobs"][0]


def test_saved_assessment_get_must_equal_the_completed_job():
    result, sent = terrain(), []
    changed = copy.deepcopy(result)
    changed["input_hash"] = "b"*64
    with (fake_terrain_client([job("completed", [result])], changed, sent) as client,
          pytest.raises(RuntimeError, match="retained assessment GET")):
        checks.wait_terrain_jobs(client, answer(result))
    assert all(request.method == "GET" for request in sent)


def test_transport_failure_never_becomes_an_unavailable_model_result():
    sent, saved = [], []

    def reply(request):
        sent.append(request)
        return httpx.Response(503, json={"error": {"code": "UNAVAILABLE"}})
    with (httpx.Client(base_url="https://workspace.example/", transport=httpx.MockTransport(reply)) as client,
          pytest.raises(AcceptanceError, match="HTTP 503")):
        checks.wait_terrain_jobs(client, answer(), checkpoint=saved.append)
    assert len(sent) == 1 and not saved


def test_historical_saved_answer_is_explicit_and_never_recalculated():
    result = terrain(available=False)
    result.update(schema_version="1", mode="proximity", model_version="wind-exposure-1.0.0",
                  landscape=None, scenario=None, failure=None, warnings=["Saved historical unavailable result."])
    content = answer(result)
    content.pop("fireExposureJobs")
    content["fireExposureData"] = [result]
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("Historical answer performed work"))) as client:
        report = checks.wait_terrain_jobs(client, content)
        assert report["status"] == "historical" and report["historical_assessment_count"] == 1
        with pytest.raises(RuntimeError, match="new source answer"):
            checks.wait_terrain_jobs(client, content, require_current=True)


def test_city_only_answer_has_no_fire_work():
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("City lookup created terrain work"))) as client:
        assert checks.wait_terrain_jobs(client, {"mapData": {"locations": [{"name": "Seattle", **AREA}]}},
                                       require_current=True)["status"] == "not_requested"


def discovery(status="queued"):
    receipt = {"id": JOB, "kind": "road_discovery", "status": status, "phase": "Offline extraction fixture",
               "progress": 1 if status == "completed" else 0.25, "scenario": copy.deepcopy(AREA),
               "created": 1, "error": None}
    if status == "completed":
        receipt["result"] = {"geometry": {"type": "FeatureCollection", "features": []}, "highways": [],
                             "source_manifest": {"publisher": "Geofabrik"}, "area": copy.deepcopy(AREA)}
    return receipt


def test_road_discovery_202_stores_only_area_then_gets_saved_completion(monkeypatch):
    sent, saved, replies = [], [], iter([discovery("running")] * 40 + [discovery("completed")])
    monkeypatch.setattr(checks.time, "sleep", lambda _: None)
    monkeypatch.setattr(checks.time, "monotonic", lambda: pytest.fail("An arbitrary discovery deadline was consulted"))

    def reply(request):
        sent.append(request)
        if request.method == "POST":
            submitted = json.loads(request.content)
            assert request.url.path == "/research/discover"
            assert {name: value for name, value in submitted.items() if name != "request_id"} == AREA
            assert len(submitted["request_id"]) == 36
            return httpx.Response(202, json=discovery())
        assert request.url.path == f"/research/discover/{JOB}"
        return httpx.Response(200, json=next(replies))
    with httpx.Client(base_url="https://workspace.example/", transport=httpx.MockTransport(reply)) as client:
        created = checks.submit_road_discovery(client, AREA)
        result = checks.wait_road_discovery(client, created, AREA, checkpoint=saved.append)
    assert result["status"] == "completed" and result["result"]["highways"] == []
    assert sent[0].method == "POST" and all(request.method == "GET" for request in sent[1:])
    assert "highway" not in created["scenario"]
    assert all(request.extensions["timeout"]["read"] == checks.TIMEOUT_SECONDS for request in sent[1:])
    assert saved[-1]["status"] == "completed"


@pytest.mark.parametrize("defect", ["job", "area", "scenario", "provisional", "result_area"])
def test_discovery_cannot_load_another_area_a_run_scenario_or_provisional_roads(defect):
    result = discovery("completed" if defect == "result_area" else "running")
    if defect == "job":
        result["id"] = OTHER_JOB
    elif defect == "area":
        result["scenario"]["lat"] += 1
    elif defect == "scenario":
        result["scenario"]["highway"] = "State 191"
    elif defect == "provisional":
        result["result"] = {"geometry": {"type": "FeatureCollection", "features": []}, "highways": []}
    else:
        result["result"]["area"]["lon"] += 1
    with pytest.raises(RuntimeError, match="saved job|Unfinished|usable saved area"):
        checks.discovery_receipt(result, AREA, JOB)


def test_road_discovery_rejects_the_old_immediate_200_shape():
    with (httpx.Client(base_url="https://workspace.example/", transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"geometry": {}, "highways": []}))) as client,
          pytest.raises(RuntimeError, match="asynchronous 202")):
        checks.submit_road_discovery(client, AREA)


def test_contract_validation_does_not_import_http_host_or_provider_configuration(monkeypatch):
    original_import = builtins.__import__

    def pure_import(name, *args, **kwargs):
        if name in ("backend.app", "backend.api_settings"):
            pytest.fail("Acceptance DTO validation imported the HTTP host or provider settings")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", pure_import)
    assert checks.contracts()[0].model_validate(REFERENCE).job_id == JOB
