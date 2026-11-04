"""Fire modeler private authentication, typed boundaries and calculation capacity fencing."""
import math
import threading

from fastapi.testclient import TestClient
from fire_risk_modeler import service


def test_private_contract_auth_and_readonly_health(private_key, make_request):
    with TestClient(service.create_app()) as client:
        assert client.get("/health").json()["status"] == "ok"
        body = make_request().model_dump(mode="json")
        assert client.post("/v1/exposure/assessments", json=body).status_code == 401
        response = client.post("/v1/exposure/assessments", json=body,
                               headers={"Authorization": f"Bearer {private_key}"})
        assert response.status_code == 200
        assert response.json()["assessment_id"] == body["assessment_id"]
        assert response.json()["availability"] == "available"
        assert "location" not in response.headers
        assert client.get(f"/v1/exposure/assessments/{body['assessment_id']}").status_code == 404


def test_hash_mismatch_and_invalid_payload_do_not_calculate(private_key, make_request, monkeypatch):
    def refused(_body):
        raise AssertionError("Invalid requests must not calculate")
    monkeypatch.setattr(service, "calculate", refused)
    with TestClient(service.create_app()) as client:
        headers = {"Authorization": f"Bearer {private_key}"}
        body = make_request().model_dump(mode="json")
        body["input_hash"] = "f" * 64
        assert client.post("/v1/exposure/assessments", json=body, headers=headers).status_code == 409
        response = client.post("/v1/exposure/assessments", json={"fire_ref": "submitted-secret"},
                               headers=headers)
        assert response.status_code == 422
        assert "submitted-secret" not in response.text


def test_oversize_body_refused_before_parsing(private_key, monkeypatch):
    monkeypatch.setenv("FIRE_RISK_REQUEST_BODY_MAX_KIB", "16")
    with TestClient(service.create_app()) as client:
        response = client.post("/v1/exposure/assessments", content="x" * (16 * 1024 + 1),
                               headers={"Authorization": f"Bearer {private_key}"})
        assert response.status_code == 413


def test_timeout_keeps_capacity_until_work_stops(private_key, make_request, monkeypatch):
    started, release = threading.Event(), threading.Event()
    monkeypatch.setenv("FIRE_RISK_CALCULATION_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("FIRE_RISK_CALCULATIONS_PER_PROCESS", "1")
    original = service.calculate

    def blocked(body):
        started.set()
        release.wait(5)
        return original(body)
    monkeypatch.setattr(service, "calculate", blocked)
    try:
        with TestClient(service.create_app()) as client:
            headers = {"Authorization": f"Bearer {private_key}"}
            body = make_request().model_dump(mode="json")
            response = client.post("/v1/exposure/assessments", json=body, headers=headers)
            assert started.is_set() and response.status_code == 503
            assert client.post("/v1/exposure/assessments", json=body, headers=headers).status_code == 429
            release.set()
    finally:
        release.set()


def test_private_landscape_endpoint_sealed_inputs_and_typed_failure(private_key):
    from test_fire_risk_landscape import request_for
    with TestClient(service.create_app()) as client:
        body = request_for().model_dump(mode="json")
        assert client.post("/v1/landscape/assessments", json=body).status_code == 401
        headers = {"Authorization": f"Bearer {private_key}"}
        response = client.post("/v1/landscape/assessments", json=body, headers=headers)
        assert response.status_code == 200
        result = response.json()
        assert result["schema_version"] == "2" and result["mode"] == "landscape"
        assert result["availability"] == "available" and result["landscape"]["spread_cells"] > 0
        # The immutable hash is checked before decompressing/provider/native work.
        body["landscape"]["raster_data"] = "bad"
        assert client.post("/v1/landscape/assessments", json=body, headers=headers).status_code == 409


def test_authenticated_landscape_response_uses_expanded_declared_byte_limit(private_key, monkeypatch):
    from fire_risk_modeler.landscape_spread import assess_landscape
    from fire_risk_modeler.models import PolygonGeometry
    from test_fire_risk_landscape import request_for

    request = request_for()
    result = assess_landscape(request)
    # An explicitly synthetic dense outline isolates HTTP byte admission; native/grid behavior has its own tests.
    ring = [[request.fire.longitude + .01 * math.cos(index * math.tau / 4000),
             request.fire.latitude + .01 * math.sin(index * math.tau / 4000)] for index in range(4000)]
    result.geojson.features[0].geometry = PolygonGeometry(coordinates=[[*ring, ring[0]]])
    assert 128 * 1024 < len(result.model_dump_json().encode()) < 256 * 1024
    monkeypatch.setattr(service, "calculate", lambda _request: result)
    with TestClient(service.create_app()) as client:
        headers = {"Authorization": f"Bearer {private_key}"}
        response = client.post("/v1/landscape/assessments", json=request.model_dump(mode="json"), headers=headers)
        assert response.status_code == 200
        assert response.json()["availability"] == "available"
        assert response.json()["geojson"]["features"][0]["geometry"]["coordinates"][0] == [*ring, ring[0]]
        monkeypatch.setenv("FIRE_RISK_RESPONSE_MAX_KIB", "128")
        refused = client.post("/v1/landscape/assessments", json=request.model_dump(mode="json"), headers=headers)
        assert refused.status_code == 503 and refused.json()["error"]["code"] == "MODEL_RESULT_TOO_LARGE"
