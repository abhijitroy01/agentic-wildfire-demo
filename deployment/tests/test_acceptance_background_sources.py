"""Offline source-proof regressions. HTTP/SDK/native replies are explicit fixtures, never live evidence."""
import copy
import gzip
import io
import json
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from deployment import ROOT
from deployment.acceptance import check_background_sources as proof
from deployment.acceptance import visitor
from deployment.tests.test_acceptance_background_jobs import AREA, ASSESSMENT, JOB, answer, job, terrain

SECOND = "44444444-4444-4444-8444-444444444444"
SOURCE = {
    "region_id": "us/washington", "url": "https://download.geofabrik.de/north-america/us/washington-261026.osm.pbf",
    "bytes": 123, "md5": "b"*32, "sha256": "c"*64,
    "snapshot_timestamp": "2026-10-26T20:00:00Z",
    "header": {"with_history": False, "option": {"osmosis_replication_timestamp": "2026-10-26T20:00:00Z"}},
}


def roads():
    return {
        "source": "OpenStreetMap via Geofabrik", "osm_sha256": "a"*64,
        "source_manifest": {"publisher": "Geofabrik", "snapshot_timestamp": "2026-10-26T20:00:00Z",
                            "recipe": "fixture", "osmium_version": "fixture", "sources": [copy.deepcopy(SOURCE)]},
        "meaning": "Candidate road corridors; no inference of road safety, closures or emergency access",
        "geometry": {"type": "FeatureCollection", "features": [{
            "type": "Feature", "geometry": {"type": "LineString", "coordinates": [[-122.33, 47.60], [-122.34, 47.61]]},
            "properties": {"corridor_id": "osm:way:123", "source_way_id": 123, "source": "OpenStreetMap"}}]},
        "highways": [{"corridor_id": "osm:way:123", "label": "Explicit synthetic Seattle corridor",
                      "source": "OpenStreetMap", "source_way_ids": [123]}],
    }


def road_job(identifier, status):
    return {"id": identifier, "kind": "road_discovery", "scenario": copy.deepcopy(AREA), "status": status,
            "phase": "Synthetic worker receipt", "progress": 1.0 if status == "completed" else 0.0,
            **({"result": roads()} if status == "completed" else {})}


def road_client(cancel_status="cancelled", *, leaking=False, stale=False, failed=False):
    calls, submissions = [], []
    cancellation_requested = False

    def reply(request):
        nonlocal cancellation_requested
        path = request.url.path.removeprefix("/simulation-api/v1/")
        other = "foreign" in request.headers.get("cookie", "")
        calls.append((request.method, path, other))
        if path == "research/discover" and request.method == "POST":
            submissions.append(json.loads(request.content))
            identifier = JOB if len(submissions) == 1 else SECOND
            return httpx.Response(202, json=road_job(identifier, "queued"))
        identifier = path.split("/")[2]
        if request.method == "POST" and path.endswith("/cancel"):
            cancellation_requested = True
            return httpx.Response(200, json=road_job(identifier, "failed" if failed else cancel_status))
        if other and not leaking:
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})
        status = "completed" if identifier == JOB else cancel_status if cancellation_requested else "queued"
        response = road_job(identifier, status)
        if stale and path.endswith(JOB) and len([call for call in calls if call[1] == path]) > 1:
            response["result"]["osm_sha256"] = "d"*64
        return httpx.Response(200, json=response)
    transport = httpx.MockTransport(reply)
    owner = httpx.Client(base_url="https://workspace.example/simulation-api/v1/", transport=transport)
    other = httpx.Client(base_url=owner.base_url, transport=transport, cookies={"demo": "foreign"})
    return owner, other, calls, submissions


def test_road_completion_cancel_and_saved_replay_submit_only_two_area_jobs(monkeypatch):
    monkeypatch.setattr(proof, "wait_road_discovery", lambda client, created, area, checkpoint:
                        proof.discovery_receipt(client.get(f"research/discover/{created['id']}").json(),
                                                area, created["id"]))
    saved = []
    owner, other, calls, submissions = road_client()
    with owner, other:
        result = proof.road_check(owner, other, AREA, lambda value: saved.append(copy.deepcopy(value)))
    assert result["status"] == "passed" and result["cancel_outcome"] == "cancelled"
    assert result["provenance"]["source_manifest"]["sources"][0]["sha256"] == SOURCE["sha256"]
    assert result["saved_completion"]["unchanged"] and result["saved_cancellation"]["unchanged"]
    assert [{key: value for key, value in body.items() if key != "request_id"} for body in submissions] == [AREA, AREA]
    assert len({str(UUID(body["request_id"])) for body in submissions}) == 2
    assert all("jobs" not in path and "sources" not in path and "investigation" not in path for _, path, _ in calls)
    assert len([call for call in calls if call[2]]) == 2
    assert saved[-1]["status"] == "passed"


def test_completion_winning_cancel_race_is_retained_as_partial_not_false_cancellation(monkeypatch):
    monkeypatch.setattr(proof, "wait_road_discovery", lambda client, created, area, checkpoint:
                        proof.discovery_receipt(client.get(f"research/discover/{created['id']}").json(),
                                                area, created["id"]))
    owner, other, _, submissions = road_client("completed")
    with owner, other:
        result = proof.road_check(owner, other, AREA, lambda value: None)
    assert result["status"] == "partial" and result["cancel_outcome"] == "completion_won_cancel_race"
    assert result["cancellation"]["status"] == "completed" and len(submissions) == 2


@pytest.mark.parametrize("defect", ["leaking", "stale", "failed"])
def test_saved_road_or_cancel_defects_stop_acceptance_without_provider_retry(monkeypatch, defect):
    monkeypatch.setattr(proof, "wait_road_discovery", lambda client, created, area, checkpoint:
                        proof.discovery_receipt(client.get(f"research/discover/{created['id']}").json(),
                                                area, created["id"]))
    owner, other, _, submissions = road_client(**{defect: True})
    with owner, other, pytest.raises(RuntimeError):
        proof.road_check(owner, other, AREA, lambda value: None)
    assert len(submissions) <= 2


@pytest.mark.parametrize("defect", ["publisher", "sha256", "bytes", "url", "corridor", "way", "meaning",
                                  "timestamp", "header", "credentials"])
def test_road_provenance_cannot_promote_missing_or_conflicting_source_evidence(defect):
    value = roads()
    if defect == "publisher":
        value["source_manifest"]["publisher"] = "Synthetic fixture"
    elif defect in {"sha256", "bytes", "url"}:
        value["source_manifest"]["sources"][0][defect] = {"sha256": "wrong", "bytes": 0, "url": "https://example.org/"}[defect]
    elif defect == "corridor":
        value["highways"][0]["corridor_id"] = "I-5"
    elif defect == "way":
        value["geometry"]["features"][0]["properties"]["source_way_id"] = 999
    elif defect == "timestamp":
        value["source_manifest"]["snapshot_timestamp"] = "2026-10-26"
    elif defect == "header":
        value["source_manifest"]["sources"][0]["header"]["option"]["osmosis_replication_timestamp"] = "wrong"
    elif defect == "credentials":
        value["source_manifest"]["sources"][0]["url"] += "?credential=fixture"
    else:
        value["meaning"] = "Safe open roads"
    with pytest.raises(RuntimeError):
        proof.road_provenance(value)


def numerical_fixture():
    proof.contracts()
    from fire_risk_modeler.models import LandscapeAssessmentRequest, model_input_hash
    value = terrain()
    crop = json.loads((ROOT / "fire-risk-modeler/tests/data/king-landscape.json").read_text())["landscape"]
    request = LandscapeAssessmentRequest(assessment_id=ASSESSMENT, fire_ref=value["fire_ref"], input_hash="0"*64,
                                        fire=value["fire"], weather=value["weather"],
                                        scenario=value["scenario"], landscape=crop)
    request = request.model_copy(update={"input_hash": model_input_hash(request)})
    value["input_hash"] = request.input_hash
    entry = {"assessment_id": ASSESSMENT, "fire_ref": request.fire_ref, "fire": value["fire"],
             "weather": value["weather"], "scenario": value["scenario"], "landscape_input_hash": request.input_hash}
    return request, value, {"id": JOB, "entries": [entry]}


def input_store(request, sent):
    raw = gzip.compress(request.model_dump_json().encode())
    return SimpleNamespace(get_object=lambda **kwargs: sent.append(kwargs) or {"Body": io.BytesIO(raw)})


@pytest.mark.parametrize("field,first,second", [
    ("ignition_aspect_degrees", 356.2104754686262, 356.2104755873638),
    ("ignition_aspect_degrees", 359.9999995, 0.0000004),
    ("ignition_aspect_degrees", 360.0, 0.0),
    ("ignition_slope_degrees", 23.529003201272364, 23.52900328145806),
])
def test_replay_allows_bounded_portability_only_for_derived_landscape_angles(field, first, second):
    saved, replay = terrain(), terrain()
    saved["landscape"][field], replay["landscape"][field] = first, second
    assert proof.equivalent(saved, replay)


@pytest.mark.parametrize("field,first,second", [
    ("ignition_aspect_degrees", 0.0, 0.00000101),
    ("ignition_slope_degrees", 0.0, 0.000000101),
    ("ignition_aspect_degrees", -0.0000001, 0.0),
    ("ignition_aspect_degrees", 360.0000001, 0.0),
    ("ignition_slope_degrees", -0.00000001, 0.0),
    ("ignition_slope_degrees", 90.00000001, 90.0),
])
def test_replay_rejects_excess_angle_delta_and_out_of_contract_ranges(field, first, second):
    saved, replay = terrain(), terrain()
    saved["landscape"][field], replay["landscape"][field] = first, second
    assert not proof.equivalent(saved, replay)


@pytest.mark.parametrize("field", ["ignition_aspect_degrees", "ignition_slope_degrees"])
@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf"), True, None, "0"])
def test_replay_rejects_invalid_angles_even_when_both_results_contain_them(field, invalid):
    saved, replay = terrain(), terrain()
    saved["landscape"][field] = replay["landscape"][field] = invalid
    assert not proof.equivalent(saved, replay)


@pytest.mark.parametrize("path,change", [
    (("geojson", "features", 0, "geometry", "coordinates", 0, 0, 0), lambda value: value + 1e-9),
    (("weather", "wind_speed_mps"), lambda value: value + 1e-9),
    (("weather", "wind_from_degrees"), lambda value: value + 1e-9),
    (("fire", "latitude"), lambda value: value + 1e-9),
    (("fire", "source_id"), lambda value: value + "-changed"),
    (("scenario", "horizons_minutes", 0), lambda value: value + 1e-9),
    (("landscape", "maximum_arrival_minutes"), lambda value: value + 1e-9),
    (("landscape", "manifest", "resolution_m"), lambda value: value + 1e-9),
    (("landscape", "manifest", "raster_sha256"), lambda _: "f"*64),
    (("landscape", "reached_cells"), lambda value: value + 1),
    (("landscape", "reached_cells"), lambda value: value + 1e-12),
    (("landscape", "ignition_row"), lambda value: value + 1),
    (("schema_version",), lambda _: "1"),
    (("model_version",), lambda value: value + "-changed"),
    (("input_hash",), lambda _: "f"*64),
])
def test_replay_retains_exact_geometry_source_inputs_counts_and_versions(path, change):
    saved, replay = terrain(), terrain()
    target = replay
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = change(target[path[-1]])
    assert not proof.equivalent(saved, replay)


@pytest.mark.parametrize("first,second", [
    (True, 1), (False, 0), ("1", 1), ([1], [1, 2]), ({"x": 1}, {"x": 1, "y": 2}),
    ({"x": 1}, {"y": 1}), ([1], (1,)), ({"x": float("nan")}, {"x": float("nan")}),
    (float("inf"), float("inf")), (-float("inf"), -float("inf")),
])
def test_replay_preserves_non_angle_types_and_structure(first, second):
    assert not proof.equivalent(first, second)


@pytest.mark.parametrize("number", [0, 1, 7, 15, 30, 315])
def test_replay_accepts_same_json_numeric_value_as_integer_or_float(number):
    assert proof.equivalent(number, float(number))
    assert proof.equivalent(float(number), number)
    assert proof.equivalent({"landscape": {"reached_cells": number}},
                            {"landscape": {"reached_cells": float(number)}})


def test_replay_cannot_promote_boolean_to_integer_cell_count():
    assert not proof.equivalent({"landscape": {"reached_cells": 1}},
                                {"landscape": {"reached_cells": True}})


def test_replay_angle_allowance_cannot_be_applied_to_same_named_nested_fields():
    first = {"nested": {"landscape": {"ignition_aspect_degrees": 123.0}}}
    second = {"nested": {"landscape": {"ignition_aspect_degrees": 123.0000001}}}
    assert not proof.equivalent(first, second)


def test_sealed_replay_accepts_recorded_angle_delta_without_changing_source_or_geometry(monkeypatch):
    request, saved, stored = numerical_fixture()
    saved["landscape"]["ignition_aspect_degrees"] = 356.2104754686262
    replay = copy.deepcopy(saved)
    replay["landscape"]["ignition_aspect_degrees"] = 356.2104755873638
    from fire_risk_modeler import landscape_spread
    native, reads = [], []
    parsed = proof.contracts()[2].model_validate(replay)
    monkeypatch.setattr(landscape_spread, "assess_landscape", lambda frozen: native.append(frozen) or parsed)
    original = copy.deepcopy(saved)
    report = proof.sealed_replay(input_store(request, reads), "fixture-bucket", stored, saved, 3*1024*1024)
    assert report["status"] == "passed" and report["numerical_replay"] is True
    assert saved == original and replay["geojson"] == saved["geojson"]
    assert native == [request] and len(reads) == 1


def test_sealed_replay_accepts_dynamodb_integral_numbers_without_rounding_values(monkeypatch):
    request, value, stored = numerical_fixture()
    from fire_risk_modeler import landscape_spread
    parsed = proof.contracts()[2].model_validate(value)

    def dynamodb_numbers(item):
        if type(item) is float and item.is_integer():
            return int(item)
        if isinstance(item, dict):
            return {key: dynamodb_numbers(child) for key, child in item.items()}
        if isinstance(item, list):
            return [dynamodb_numbers(child) for child in item]
        return item

    saved = dynamodb_numbers(value)
    assert type(saved["weather"]["wind_from_degrees"]) is int
    assert type(saved["scenario"]["horizons_minutes"][0]) is int
    assert type(saved["landscape"]["manifest"]["resolution_m"]) is int
    native, reads = [], []
    monkeypatch.setattr(landscape_spread, "assess_landscape", lambda frozen: native.append(frozen) or parsed)
    original = copy.deepcopy(saved)
    report = proof.sealed_replay(input_store(request, reads), "fixture-bucket", stored, saved, 3*1024*1024)
    assert report["status"] == "passed" and report["input_hash"] == request.input_hash
    assert saved == original and native == [request] and len(reads) == 1


def test_native_replay_uses_only_fixed_sealed_input_and_ignores_calculation_timestamp(monkeypatch):
    request, value, stored = numerical_fixture()
    calls, native = [], []
    from fire_risk_modeler import landscape_spread
    replay = proof.contracts()[2].model_validate({**value, "calculated_at": "2026-10-28T00:00:00Z"})
    monkeypatch.setattr(landscape_spread, "assess_landscape", lambda frozen: native.append(frozen) or replay)
    report = proof.sealed_replay(input_store(request, calls), "fixture-bucket", stored, value, 3*1024*1024)
    assert report["status"] == "passed" and report["numerical_replay"] is True and native == [request]
    assert calls == [{"Bucket": "fixture-bucket",
                      "Key": f"fire-risk/inputs/{JOB}/{ASSESSMENT}/{request.input_hash}.json.gz"}]
    assert report["input_hash"] == request.input_hash


@pytest.mark.parametrize("defect", ["input_hash", "point", "weather", "scenario", "result_geometry"])
def test_sealed_or_replayed_numerical_identity_conflicts_fail(monkeypatch, defect):
    request, value, stored = numerical_fixture()
    from fire_risk_modeler import landscape_spread
    replay = copy.deepcopy(value)
    if defect == "input_hash":
        request = request.model_copy(update={"input_hash": "f"*64})
    elif defect == "point":
        stored["entries"][0]["fire"] = {**stored["entries"][0]["fire"], "longitude": -122}
    elif defect == "weather":
        stored["entries"][0]["weather"] = {**stored["entries"][0]["weather"], "wind_speed_mps": 0}
    elif defect == "scenario":
        stored["entries"][0]["scenario"] = {**stored["entries"][0]["scenario"], "moisture_profile": "low"}
    else:
        replay["geojson"]["features"][0]["geometry"]["coordinates"][0][0][0] += 0.001
        replay["geojson"]["features"][0]["geometry"]["coordinates"][0][-1][0] += 0.001
    monkeypatch.setattr(landscape_spread, "assess_landscape", lambda _: proof.contracts()[2].model_validate(replay))
    with pytest.raises(RuntimeError):
        proof.sealed_replay(input_store(request, []), "fixture-bucket", stored, value, 3*1024*1024)


def test_source_unavailable_before_input_sealing_never_fetches_or_claims_native_replay():
    _, value, stored = numerical_fixture()
    value.update(availability="unavailable", landscape=None, geojson={"type": "FeatureCollection", "features": []},
                 failure={"code": "LANDSCAPE_REQUEST_FAILED", "message": "Explicit offline failure fixture."})
    entry = stored["entries"][0]
    entry.pop("landscape_input_hash")
    value["input_hash"] = proof.digest({"fire": entry["fire"], "weather": entry["weather"],
                                       "scenario": entry["scenario"], "failure_code": value["failure"]["code"]})
    store = SimpleNamespace(get_object=lambda **_: pytest.fail("No numerical input exists for a pre-input failure"))
    result = proof.sealed_replay(store, "fixture-bucket", stored, value, 3*1024*1024)
    assert result["status"] == "not_applicable" and result["numerical_replay"] is False
    value["input_hash"] = "f"*64
    with pytest.raises(RuntimeError, match="frozen source"):
        proof.sealed_replay(store, "fixture-bucket", stored, value, 3*1024*1024)


@pytest.mark.parametrize("defect", [None, "leak", "changed", "revision", "cache"])
def test_terrain_saved_reads_bind_original_result_owner_terminal_revision_and_cache_policy(defect):
    value = terrain(no_growth=True)
    outcome = {"assessment_id": ASSESSMENT, "result_sha256": proof.digest(value)}
    report = {"jobs": [{"job_id": JOB, "assessments": [outcome]}]}
    records = {
        JOB: {"id": JOB, "status": "completed", "revision": 1, "entries": []},
        "fire_exposure:" + ASSESSMENT: {"revision": 1 if defect == "revision" else 2,
                                      "mode": "landscape", "input_hash": value["input_hash"], "result": value},
    }
    table = SimpleNamespace(get_item=lambda **request: {"Item": copy.deepcopy(records[request["Key"]["id"]])}
                            if request["Key"]["id"] in records else {})

    def reply(request):
        if "foreign" in request.headers.get("cookie", "") and defect != "leak":
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})
        raw = job("completed", [copy.deepcopy(value)]) if "/jobs/" in request.url.path else value
        if defect == "changed" and "/jobs/" in request.url.path:
            raw["assessments"][0]["calculated_at"] = "2026-10-28T00:00:00Z"
        return httpx.Response(200, json=raw, headers={"cache-control": "public" if defect == "cache" else "no-store"})
    transport = httpx.MockTransport(reply)
    with (httpx.Client(base_url="https://fixture.example/", transport=transport) as owner,
          httpx.Client(base_url="https://fixture.example/", transport=transport,
                       cookies={"demo": "foreign"}) as other):
        if defect:
            with pytest.raises(RuntimeError):
                proof.retained_reads(owner, other, table, report)
        else:
            result = proof.retained_reads(owner, other, table, report)
            assert result["counters_unchanged"] and result["assessments"][0]["unchanged"]
            assert result["assessments"][0]["foreign_status"] == 404
            assert '"foreign"' not in json.dumps(result) and "cookie" not in json.dumps(result)


@pytest.mark.parametrize("no_growth", [False, True])
def test_real_result_contract_accepts_available_spread_or_no_growth_without_fabricated_expansion(monkeypatch, no_growth):
    value, saved = terrain(no_growth=no_growth), []
    content = answer(value)
    monkeypatch.setattr(proof, "selected_incident", lambda *_: {"fire": value["fire"]})
    monkeypatch.setattr(proof, "investigation_turn", lambda *_, **__: {
        "snapshot": {"content": content}, "tools": [], "identities": {}, "execution_receipts": [], "status": "fixture"})
    monkeypatch.setattr(proof, "verify_fire_search_center", lambda *_, **__: True)
    monkeypatch.setattr(proof, "retained_reads", lambda *_: {})
    monkeypatch.setattr(proof, "record", lambda *_: {"id": JOB, "entries": []})
    monkeypatch.setattr(proof, "sealed_replay", lambda *_: {"status": "passed", "numerical_replay": True})
    monkeypatch.setattr(proof, "fire_handoff", lambda *_: {"status": "passed"})

    def reply(request):
        return httpx.Response(200, json=job("completed", [value]) if "/jobs/" in request.url.path else value)
    with httpx.Client(base_url="https://fixture.example/", transport=httpx.MockTransport(reply)) as client:
        report = proof.fire_check(client, client, None, None, None,
                                  SimpleNamespace(settings={"fire_risk_landscape_request_max_kib": 3072}),
                                  (38.5816, -121.4944), 0, None, lambda state: saved.append(copy.deepcopy(state)))
    assert report["status"] == "passed" and report["terrain"]["available"] == 1
    assert report["outcomes"] == ["no_growth" if no_growth else "spread"]
    assert saved[-1]["status"] == "passed"


def test_first_actual_esri_candidate_is_selected_without_a_canned_ignition_or_retry(monkeypatch):
    calls = []
    response = {"features": [
        {"attributes": {"IncidentTypeCategory": "WF", "IncidentName": "Contained fixture", "IrwinID": "a",
                        "PercentContained": 100}, "geometry": {"x": -122.3, "y": 47.6}},
        {"attributes": {"IncidentTypeCategory": "WF", "IncidentName": "Overseas fixture", "IrwinID": "b"},
         "geometry": {"x": 10, "y": 50}},
        {"attributes": {"IncidentTypeCategory": "WF", "IncidentName": "Actual-source reply fixture", "IrwinID": "c"},
         "geometry": {"x": -122.314159, "y": 47.612345}},
    ]}
    import urllib.request
    monkeypatch.setattr(urllib.request.OpenerDirector, "open",
                        lambda _self, request, **_: calls.append(request.full_url) or
                        io.BytesIO(json.dumps(response).encode()))
    selected = proof.selected_incident((38.5816, -121.4944))
    assert selected["fire"]["latitude"] == 47.612345 and selected["fire"]["longitude"] == -122.314159
    assert selected["fire"]["source_id"] == "esri:IrwinID:c" and selected["candidate_index"] == 0
    assert len(calls) == 1


def test_all_typed_unavailable_fire_outcomes_are_retained_but_do_not_pass_capacity(monkeypatch):
    value, saved = terrain(available=False), []
    content = answer(value)
    monkeypatch.setattr(proof, "selected_incident", lambda *_: {"fire": value["fire"]})
    monkeypatch.setattr(proof, "investigation_turn", lambda *_, **__: {
        "snapshot": {"content": content}, "tools": [], "identities": {}, "execution_receipts": [], "status": "fixture"})
    monkeypatch.setattr(proof, "verify_fire_search_center", lambda *_, **__: True)
    monkeypatch.setattr(proof, "retained_reads", lambda *_: {})
    monkeypatch.setattr(proof, "record", lambda *_: {"id": JOB, "entries": []})
    monkeypatch.setattr(proof, "sealed_replay", lambda *_: {"status": "not_applicable"})
    monkeypatch.setattr(proof, "fire_handoff", lambda *_: {"status": "passed"})

    def reply(request):
        return httpx.Response(200, json=job("completed", [value]) if "/jobs/" in request.url.path else value)
    with (httpx.Client(base_url="https://fixture.example/", transport=httpx.MockTransport(reply)) as client,
          pytest.raises(RuntimeError, match="No actual available terrain")):
        proof.fire_check(client, client, None, None, None,
                         SimpleNamespace(settings={"fire_risk_landscape_request_max_kib": 3072}),
                         (38.5816, -121.4944), 0, None, lambda report: saved.append(copy.deepcopy(report)))
    assert saved[-1]["status"] == "failed" and saved[-1]["outcomes"] == ["unavailable"]
    assert saved[-1]["terrain"]["available"] == 0


def test_submit_is_required_before_visitors_sdk_or_provider_work(monkeypatch, tmp_path):
    monkeypatch.setattr(proof.boto3, "Session", lambda **_: pytest.fail("No AWS session without explicit submit"))
    monkeypatch.setattr(visitor, "visitor_client", lambda *_: pytest.fail("No browser session without submit"))
    with pytest.raises(SystemExit):
        proof.main(["--scope", "all", "--output", str(tmp_path / "report.json")])
    assert not (tmp_path / "report.json").exists()
