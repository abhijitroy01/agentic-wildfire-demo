"""Regional numerical transport is bounded, aligned and never substituted."""

import io
import json

import httpx
import numpy as np
import pytest
from fire_risk_modeler import landscape_acquisition
from fire_risk_modeler.landscape import REGIONAL_CRS, LandscapeError, terrain_grid
from fire_risk_modeler.models import FireObservation
from PIL import Image, TiffImagePlugin
from pyproj import Transformer


@pytest.mark.parametrize(
    "latitude,longitude,region",
    [
        (47.470805726545464, -121.25588168570884, "CONUS"),
        (64.0, -150.0, "AK"),
        (52.0, 179.0, "AK"),
        (20.0, -156.0, "HI"),
    ],
)
def test_regional_numeric_crop_alignment_without_other_sources(latitude, longitude, region, monkeypatch):
    fire = FireObservation(latitude=latitude, longitude=longitude)
    calls = []
    crs = REGIONAL_CRS[region]
    x, y = Transformer.from_crs(4326, crs, always_xy=True).transform(longitude, latitude)

    def handle(request):
        calls.append(request)
        if request.url.params["f"] == "json":
            return httpx.Response(
                200,
                json={
                    "pixelSizeX": 30,
                    "pixelSizeY": 30,
                    "extent": {"xmin": x - 10_000, "ymin": y - 10_000},
                },
            )
        size = int(request.url.params["size"].split(",")[0])
        west, _south, _east, north = map(float, request.url.params["bbox"].split(","))
        assert request.url.params["renderingRule"] == '{"rasterFunction": "None"}'
        assert request.url.params["interpolation"] == "RSP_NearestNeighbor"
        assert request.url.params["imageSR"] == crs.split(":")[1]
        fuel = "FBFM40" in request.url.path
        values = np.full((size, size), 122 if fuel else 1000, dtype=np.int32)
        tags = TiffImagePlugin.ImageFileDirectory_v2()
        tags[33550] = (30.0, 30.0, 0.0)
        tags[33922] = (0.0, 0.0, 0.0, west, north, 0.0)
        buffer = io.BytesIO()
        Image.fromarray(values).save(buffer, format="TIFF", tiffinfo=tags)
        return httpx.Response(200, content=buffer.getvalue())

    original = httpx.Client
    monkeypatch.setattr(
        landscape_acquisition.httpx,
        "Client",
        lambda **options: original(
            transport=httpx.MockTransport(handle),
            **options,
        ),
    )
    result = landscape_acquisition.prepare_landscape(
        fire,
        300.0,
        {
            "timeout_seconds": 0, "idle_timeout_seconds": 30,
            "max_response_bytes": 2 * 1024 * 1024,
            "max_cells": 10_000,
        },
    )
    assert result.manifest.region == region
    assert result.manifest.crs == crs
    assert result.manifest.resolution_m == 30
    assert terrain_grid(result, fire).fuel[10, 10] == 122
    assert len(calls) == 3
    assert all(str(call.url).startswith("https://lfps.usgs.gov/arcgis/rest/services/") for call in calls)
    assert all("LF2024" in str(call.url) or "LF2020" in str(call.url) for call in calls)


def test_http_error_is_provider_failure_and_not_a_coverage_claim(monkeypatch):
    original = httpx.Client
    monkeypatch.setattr(
        landscape_acquisition.httpx,
        "Client",
        lambda **options: original(
            transport=httpx.MockTransport(lambda _request: httpx.Response(503)),
            **options,
        ),
    )
    with pytest.raises(LandscapeError) as exc:
        landscape_acquisition.prepare_landscape(
            FireObservation(latitude=47.6, longitude=-122.3),
            300.0,
            {
                "timeout_seconds": 0, "idle_timeout_seconds": 30,
                "max_response_bytes": 2 * 1024 * 1024,
                "max_cells": 10_000,
            },
        )
    assert exc.value.failure.code == "LANDSCAPE_REQUEST_FAILED"
    assert isinstance(exc.value.__cause__, httpx.HTTPStatusError)
    assert "http" not in exc.value.failure.message.lower()
    assert exc.value.retryable is True and exc.value.provider_status == 503


@pytest.mark.parametrize("code,message,retryable", [
    (400, "General function failure.", True),
    (400, "Invalid bounding box", False),
    (401, "Access denied", False),
    (403, "Access denied", False),
    (429, "Throttled", True),
    (503, "Service unavailable", True),
    ("400", "General function failure.", False),
])
def test_only_narrow_provider_failures_are_retried(code, message, retryable):
    with pytest.raises(LandscapeError) as raised:
        landscape_acquisition.reject_provider_error(json.dumps({"error": {"code": code,
                                                                         "message": message}}).encode(),
                                                    "fuel", "crop")
    assert raised.value.retryable is retryable
    assert message not in raised.value.failure.message


def test_socket_idle_timeout_stays_finite_without_a_whole_job_deadline(monkeypatch):
    seen = []

    def client(**options):
        seen.append(options["timeout"])
        raise httpx.ReadTimeout("untrusted provider URL")

    monkeypatch.setattr(landscape_acquisition.httpx, "Client", client)
    with pytest.raises(LandscapeError) as raised:
        landscape_acquisition.prepare_landscape(FireObservation(latitude=47.6, longitude=-122.3), 300,
                                                {"timeout_seconds": 0, "idle_timeout_seconds": 30,
                                                 "max_response_bytes": 2*1024*1024, "max_cells": 10_000})
    assert seen == [30] and raised.value.retryable
    assert "untrusted provider URL" not in str(raised.value)


def test_unsupported_geography_makes_no_request(monkeypatch):
    def forbidden(**_options):
        raise AssertionError("Unsupported geography must not choose a different provider")

    monkeypatch.setattr(landscape_acquisition.httpx, "Client", forbidden)
    with pytest.raises(LandscapeError) as exc:
        landscape_acquisition.prepare_landscape(
            FireObservation(latitude=0.0, longitude=0.0),
            300.0,
            {
                "timeout_seconds": 0, "idle_timeout_seconds": 30,
                "max_response_bytes": 2 * 1024 * 1024,
                "max_cells": 10_000,
            },
        )
    assert exc.value.failure.code == "LANDSCAPE_COVERAGE_MISSING"


@pytest.mark.parametrize("failed_stage", ["metadata", "fuel_crop", "elevation_crop"])
def test_http_200_provider_error_is_detected_before_metadata_or_tiff_decoding(failed_stage, monkeypatch):
    """Real ArcGIS failures may claim image/tiff while their bounded body contains an error envelope."""
    sensitive = "private provider diagnostic must never reach the public assessment"

    def handle(request):
        metadata = request.url.params["f"] == "json"
        fuel = "FBFM40" in request.url.path
        failed = (failed_stage == "metadata" and metadata
                  or failed_stage == "fuel_crop" and not metadata and fuel
                  or failed_stage == "elevation_crop" and not metadata and not fuel)
        if failed:
            return httpx.Response(
                200,
                json={"error": {"code": 400, "message": sensitive, "details": [sensitive]}},
                headers={"content-type": "image/tiff"},
            )
        if metadata:
            return httpx.Response(200, json={"pixelSizeX": 30, "pixelSizeY": 30,
                                            "extent": {"xmin": -2_400_000, "ymin": 0}})
        size = int(request.url.params["size"].split(",")[0])
        west, _south, _east, north = map(float, request.url.params["bbox"].split(","))
        values = np.full((size, size), 122 if fuel else 1000, dtype=np.int32)
        tags = TiffImagePlugin.ImageFileDirectory_v2()
        tags[33550] = (30.0, 30.0, 0.0)
        tags[33922] = (0.0, 0.0, 0.0, west, north, 0.0)
        buffer = io.BytesIO()
        Image.fromarray(values).save(buffer, format="TIFF", tiffinfo=tags)
        return httpx.Response(200, content=buffer.getvalue())

    original = httpx.Client
    monkeypatch.setattr(landscape_acquisition.httpx, "Client",
                        lambda **options: original(transport=httpx.MockTransport(handle), **options))
    with pytest.raises(LandscapeError) as raised:
        landscape_acquisition.prepare_landscape(
            FireObservation(latitude=38.57640501062021, longitude=-122.40124630847806),
            1500.0, {"timeout_seconds": 0, "idle_timeout_seconds": 30, "max_response_bytes": 8 * 1024 * 1024, "max_cells": 250_000},
        )
    failure = raised.value.failure
    assert failure.code == "LANDSCAPE_REQUEST_FAILED"
    role, stage = ("fuel", "metadata") if failed_stage == "metadata" else (failed_stage.split("_")[0], "crop")
    assert failure.message == f"Canonical LANDFIRE {role} {stage} returned a provider error (code 400)."
    assert sensitive not in str(raised.value) and raised.value.__cause__ is None


@pytest.mark.parametrize("code", [True, "400", 400.0, 999999])
def test_provider_error_never_echoes_an_unvalidated_code_or_diagnostic(code):
    with pytest.raises(LandscapeError) as raised:
        landscape_acquisition.reject_provider_error(
            json.dumps({"error": {"code": code, "message": "untrusted"}}).encode(),
            "fuel", "crop",
        )
    assert raised.value.failure.message == "Canonical LANDFIRE fuel crop returned a provider error."


def test_malformed_tiff_remains_invalid_input_without_substitution(monkeypatch):
    def handle(request):
        if request.url.params["f"] == "json":
            return httpx.Response(200, json={"pixelSizeX": 30, "pixelSizeY": 30,
                                            "extent": {"xmin": -2_400_000, "ymin": 0}})
        return httpx.Response(200, content=b"malformed numerical TIFF", headers={"content-type": "image/tiff"})

    original = httpx.Client
    monkeypatch.setattr(landscape_acquisition.httpx, "Client",
                        lambda **options: original(transport=httpx.MockTransport(handle), **options))
    with pytest.raises(LandscapeError) as raised:
        landscape_acquisition.prepare_landscape(
            FireObservation(latitude=38.57640501062021, longitude=-122.40124630847806),
            1500.0, {"timeout_seconds": 0, "idle_timeout_seconds": 30, "max_response_bytes": 8 * 1024 * 1024, "max_cells": 250_000},
        )
    assert raised.value.failure.code == "LANDSCAPE_INPUT_INVALID"
