"""Component fixtures; source imports are namespaced and do not load the service."""
from uuid import uuid4

import pytest
from fire_risk_modeler import settings
from fire_risk_modeler.models import ExposureAssessmentRequest, model_input_hash


@pytest.fixture
def make_request():
    def build(*, latitude=47.6, longitude=-122.3, weather=True, **changes):
        values = {
            "assessment_id": str(uuid4()), "fire_ref": "immutable-receipt:provider-fire",
            "input_hash": "0" * 64, "fire": {"latitude": latitude, "longitude": longitude},
            "parameters": settings.default_parameters().model_dump(mode="json"),
            "weather": {
                "query_latitude": latitude, "query_longitude": longitude,
                "observed_at": "2026-10-26T12:00:00Z", "availability": "available",
                "wind_speed_mps": 10.0, "wind_from_degrees": 0.0,
            } if weather else None,
        }
        values.update(changes)
        request = ExposureAssessmentRequest.model_validate(values)
        return request.model_copy(update={"input_hash": model_input_hash(request)})
    return build


@pytest.fixture
def private_key(monkeypatch):
    key = "component-test-key-" + "x" * 32
    monkeypatch.setenv("FIRE_RISK_SERVICE_KEY", key)
    monkeypatch.setenv("FIRE_RISK_ENVIRONMENT", "local")
    return key
