"""The standalone numerical benchmark must measure the API's actual retained attribute envelope."""
import importlib.util
import json
import math
from pathlib import Path

import pytest
from fire_risk_modeler.models import AssessmentResult, PolygonGeometry

from backend import fire_risk_broker

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "landscape_capacity_benchmark", ROOT / "fire-risk-modeler/scripts/benchmark_landscape.py",
)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


@pytest.mark.parametrize("dense_geometry", [False, True])
def test_benchmark_attribute_bytes_match_retained_publication_serializer(dense_geometry):
    result = AssessmentResult.model_validate(json.loads(
        (ROOT / "backend/tests/data/fire-risk-protocol.json").read_text(),
    ))
    if dense_geometry:
        ring = [[.01 * math.cos(index * math.tau / 4000), .01 * math.sin(index * math.tau / 4000)]
                for index in range(4000)]
        result.geojson.features[0].geometry = PolygonGeometry(coordinates=[[*ring, ring[0]]])
    document = {"id": "fire_exposure:"+result.assessment_id, "kind": "fire_exposure",
                "subject": "demo-"+"f"*64, "revision": 2, "created_at": 9999999999.999999,
                "expires": 9999999999, "input_hash": result.input_hash, "mode": "landscape",
                "result": result.model_dump(mode="json")}
    assert benchmark.retained_envelope_bytes(result) == fire_risk_broker.retained_size(document)
    assert benchmark.RETAINED_GUARD_BYTES == fire_risk_broker.MAX_RETAINED_BYTES
    if dense_geometry:
        assert benchmark.retained_envelope_bytes(result) > len(result.model_dump_json().encode())
