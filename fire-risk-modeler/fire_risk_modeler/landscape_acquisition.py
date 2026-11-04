"""Bounded acquisition of one canonical LANDFIRE product pair, outside the numerical pool."""

import hashlib
import io
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx
import numpy as np
from PIL import Image
from pyproj import Transformer

from fire_risk_modeler.landscape import (
    REGIONAL_CRS,
    LandscapeError,
    regional_sources,
    seal_landscape,
    source_region,
)
from fire_risk_modeler.models import FireObservation, LandscapeInput


def reject_provider_error(body: bytes, role: str, stage: str) -> None:
    """ArcGIS can return a JSON error with HTTP 200 and image/tiff; never echo its untrusted content."""
    try:
        document = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(document, dict) or document.get("error") is None:
        return
    error = document["error"]
    code = error.get("code") if isinstance(error, dict) else None
    suffix = f" (code {code})" if type(code) is int and 100 <= code <= 599 else ""
    # Only the observed opaque ArcGIS function failure is rechecked at 400. Ordinary invalid requests,
    # access refusals, malformed crops and coverage errors are permanent.
    retryable = type(code) is int and (code in (429, 500, 502, 503, 504) or (
        code == 400 and isinstance(error, dict) and str(error.get("message", "")).strip().lower()
        in ("general function failure", "general function failure.")))
    raise LandscapeError(
        "LANDSCAPE_REQUEST_FAILED",
        f"Canonical LANDFIRE {role} {stage} returned a provider error{suffix}.",
        retryable=retryable, provider_status=code if type(code) is int else None, role=role, stage=stage,
    )


def prepare_landscape(fire: FireObservation, radius_m: float, limits: dict) -> LandscapeInput:
    """Return an aligned sealed crop; caller persists it and owns job status and retries.

    limits requires timeout_seconds, idle_timeout_seconds, max_response_bytes, max_cells.
    """
    if not math.isfinite(radius_m) or radius_m < 150:
        raise LandscapeError(
            "LANDSCAPE_INPUT_INVALID", "Landscape acquisition radius must be at least 150 metres."
        )
    timeout, maximum, cells = (limits[key] for key in ("timeout_seconds", "max_response_bytes", "max_cells"))
    idle_timeout = limits["idle_timeout_seconds"]
    if (
        not math.isfinite(timeout)
        or timeout < 0
        or not 1024 <= maximum <= 32 * 1024 * 1024
        or not 25 <= cells <= 250_000
        or not math.isfinite(idle_timeout) or not 0 < idle_timeout <= 120
    ):
        raise LandscapeError("LANDSCAPE_INPUT_INVALID", "Landscape acquisition limits are invalid.")
    region = source_region(fire)
    fuel_source, elevation_source = regional_sources(region)
    crs = REGIONAL_CRS[region]
    deadline = time.monotonic() + timeout if timeout else None

    def fetch(url, params, role, stage):
        remaining = deadline - time.monotonic() if deadline else None
        if remaining is not None and remaining <= 0:
            raise LandscapeError("CALCULATION_LIMIT", "Landscape acquisition exceeded its declared deadline.")
        try:
            with (
                httpx.Client(timeout=min(idle_timeout, remaining) if remaining is not None else idle_timeout,
                             follow_redirects=False, trust_env=False) as client,
                client.stream("GET", url, params=params) as response,
            ):
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    if (deadline is not None and time.monotonic() >= deadline) or len(body) + len(
                        chunk
                    ) > maximum:
                        raise LandscapeError(
                            "CALCULATION_LIMIT", "Landscape response exceeded its declared limits."
                        )
                    body.extend(chunk)
            content = bytes(body)
            reject_provider_error(content, role, stage)
            return content
        except httpx.HTTPError as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            retryable = status in (429, 500, 502, 503, 504) if status is not None else isinstance(
                exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError))
            delay = exc.response.headers.get("Retry-After", "") if isinstance(exc, httpx.HTTPStatusError) else ""
            raise LandscapeError(
                "LANDSCAPE_REQUEST_FAILED", f"Canonical LANDFIRE {role} {stage} request failed.",
                retryable=retryable, retry_after=int(delay) if delay.isdecimal() and len(delay) <= 10 else None,
                provider_status=status, role=role, stage=stage,
            ) from exc

    try:
        metadata = json.loads(fetch(fuel_source, {"f": "json"}, "fuel", "metadata"))
        extent = metadata["extent"]
        resolution = 30.0
        if metadata["pixelSizeX"] != resolution or metadata["pixelSizeY"] != resolution:
            raise ValueError("Unexpected product resolution")
        x, y = Transformer.from_crs(4326, crs, always_xy=True).transform(fire.longitude, fire.latitude)
        west = extent["xmin"] + math.floor((x - radius_m - extent["xmin"]) / resolution) * resolution
        north = extent["ymin"] + math.ceil((y + radius_m - extent["ymin"]) / resolution) * resolution
        size = math.ceil((2 * radius_m) / resolution) + 2
        if size * size > cells:
            raise LandscapeError(
                "CALCULATION_LIMIT", "Required landscape window exceeds its declared cell limit."
            )
        bounds = [west, north - size * resolution, west + size * resolution, north]
        authority = crs.split(":")[1]
        params = {
            "f": "image",
            "bbox": ",".join(map(str, bounds)),
            "bboxSR": authority,
            "imageSR": authority,
            "size": f"{size},{size}",
            "format": "tiff",
            "pixelType": "S16",
            "renderingRule": json.dumps({"rasterFunction": "None"}),
            "interpolation": "RSP_NearestNeighbor",
        }

        def crop(url, role):
            content = fetch(url + "/exportImage", params, role, "crop")
            with Image.open(io.BytesIO(content)) as image:
                if image.size != (size, size) or image.mode != "I":
                    raise ValueError("Unexpected numerical TIFF")
                scale, tiepoint = image.tag_v2.get(33550), image.tag_v2.get(33922)
                if (
                    not scale
                    or not tiepoint
                    or list(scale[:2]) != [resolution, resolution]
                    or abs(tiepoint[3] - west) > 0.01
                    or abs(tiepoint[4] - north) > 0.01
                ):
                    raise ValueError("Unexpected georeferencing")
                values = np.array(image, copy=True)
            return values, hashlib.sha256(content).hexdigest()

        with ThreadPoolExecutor(max_workers=2) as executor:
            fuel_future = executor.submit(crop, fuel_source, "fuel")
            elevation_future = executor.submit(crop, elevation_source, "elevation")
            fuel, fuel_hash = fuel_future.result()
            elevation, elevation_hash = elevation_future.result()
        return seal_landscape(
            fuel,
            elevation,
            region=region,
            fuel_source=fuel_source,
            elevation_source=elevation_source,
            crs=crs,
            west=float(west),
            north=float(north),
            resolution_m=resolution,
            captured_at=datetime.now(UTC).isoformat(),
            fuel_sha256=fuel_hash,
            elevation_sha256=elevation_hash,
        )
    except LandscapeError:
        raise
    except (ValueError, KeyError, OSError, TypeError) as exc:
        raise LandscapeError(
            "LANDSCAPE_INPUT_INVALID", "Canonical landscape response is not a valid aligned numerical crop."
        ) from exc
