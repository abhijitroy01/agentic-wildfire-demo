"""Sealed landscape raster inputs and terrain preparation. No network access."""

import base64
import hashlib
import math
import zlib
from dataclasses import dataclass

import numpy as np
from pyproj import Geod, Transformer

from fire_risk_modeler.models import (
    AssessmentFailure,
    FireObservation,
    LandscapeInput,
    LandscapeManifest,
)

BURNABLE_FUELS = frozenset(
    list(range(101, 110))
    + list(range(121, 125))
    + list(range(141, 150))
    + list(range(161, 166))
    + list(range(181, 190))
    + list(range(201, 205))
)
NONBURNABLE_FUELS = frozenset({91, 92, 93, 98, 99})
REGIONAL_CRS = {"CONUS": "EPSG:5070", "AK": "EPSG:3338", "HI": "ESRI:102007"}
SOURCE_BASE = "https://lfps.usgs.gov/arcgis/rest/services/"


class LandscapeError(Exception):
    """A safe typed failure. Messages contain no upstream bodies, URLs or credentials."""

    def __init__(self, code: str, message: str, *, retryable=False, retry_after=None, provider_status=None,
                 stage=None, role=None):
        self.failure = AssessmentFailure(code=code, message=message)
        self.retryable = retryable
        self.retry_after = retry_after
        self.provider_status = provider_status
        self.stage = stage
        self.role = role
        super().__init__(message)


def regional_sources(region: str) -> tuple[str, str]:
    if region not in REGIONAL_CRS:
        raise LandscapeError("LANDSCAPE_COVERAGE_MISSING", "Fire lies outside supported landscape coverage.")
    return (
        f"{SOURCE_BASE}Landfire_LF2024/LF2024_FBFM40_{region}/ImageServer",
        f"{SOURCE_BASE}Landfire_Topo/LF2020_Elev_{region}/ImageServer",
    )


def source_region(fire: FireObservation) -> str:
    lat, lon = fire.latitude, fire.longitude
    if 18 <= lat <= 23 and -161 <= lon <= -154:
        return "HI"
    if 50 <= lat <= 72 and (lon <= -129 or lon >= 170):
        return "AK"
    if 24 <= lat <= 50 and -125 <= lon <= -66:
        return "CONUS"
    raise LandscapeError(
        "LANDSCAPE_COVERAGE_MISSING", "Fire lies outside CONUS, Alaska and Hawaii landscape coverage."
    )


def seal_landscape(fuel, elevation, **manifest_values) -> LandscapeInput:
    """Pack explicitly acquired arrays; no inference, filling, resampling or fuel substitution."""
    fuel, elevation = np.asarray(fuel), np.asarray(elevation)
    if fuel.ndim != 2 or fuel.shape != elevation.shape or fuel.size > 250_000:
        raise LandscapeError("LANDSCAPE_INPUT_INVALID", "Landscape arrays have invalid dimensions.")
    if not np.isfinite(fuel).all() or not np.equal(fuel, np.floor(fuel)).all():
        raise LandscapeError(
            "LANDSCAPE_INPUT_INVALID", "Fuel raster must contain finite integer class codes."
        )
    if not np.isfinite(elevation).all() or np.any((fuel < -9999) | (fuel > 32767)):
        raise LandscapeError("LANDSCAPE_INPUT_INVALID", "Landscape raster contains invalid numerical values.")
    raw = fuel.astype("<i2").tobytes() + elevation.astype("<f4").tobytes()
    manifest = LandscapeManifest(
        width=int(fuel.shape[1]),
        height=int(fuel.shape[0]),
        raster_sha256=hashlib.sha256(raw).hexdigest(),
        **manifest_values,
    )
    return LandscapeInput(manifest=manifest, raster_data=base64.b64encode(zlib.compress(raw)).decode("ascii"))


def unpack_landscape(value: LandscapeInput) -> tuple[np.ndarray, np.ndarray]:
    """Bound decompression before allocation, verify the seal and reject unknown fuel codes."""
    cells = value.manifest.width * value.manifest.height
    try:
        compressed = base64.b64decode(value.raster_data, validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, cells * 6 + 1)
        if (
            len(raw) != cells * 6
            or not decoder.eof
            or decoder.unused_data
            or decoder.unconsumed_tail
            or hashlib.sha256(raw).hexdigest() != value.manifest.raster_sha256
        ):
            raise ValueError("Invalid raster seal")
        fuel = np.frombuffer(raw, "<i2", count=cells).reshape(value.manifest.height, value.manifest.width)
        elevation = np.frombuffer(raw, "<f4", offset=cells * 2).reshape(fuel.shape)
    except (ValueError, zlib.error) as exc:
        raise LandscapeError(
            "LANDSCAPE_INPUT_INVALID", "Landscape raster seal or encoding is invalid."
        ) from exc
    if not np.isfinite(elevation).all() or np.any((elevation != -9999) & (np.abs(elevation) > 20_000)):
        raise LandscapeError(
            "LANDSCAPE_INPUT_INVALID", "Elevation raster contains invalid numerical heights."
        )
    known = np.isin(fuel, list(BURNABLE_FUELS | NONBURNABLE_FUELS | {-9999}))
    if not known.all():
        raise LandscapeError(
            "FUEL_CODE_UNSUPPORTED", "Fuel raster contains an unsupported standard model code."
        )
    return fuel, elevation


@dataclass(frozen=True)
class TerrainGrid:
    fuel: np.ndarray
    elevation: np.ndarray
    slope: np.ndarray
    aspect: np.ndarray
    east_x: np.ndarray
    north_x: np.ndarray
    east_y: np.ndarray
    north_y: np.ndarray
    gradient_east: np.ndarray
    gradient_north: np.ndarray
    burnable: np.ndarray
    unknown: np.ndarray
    ignition: tuple[int, int]
    manifest: LandscapeManifest


def terrain_grid(value: LandscapeInput, fire: FireObservation) -> TerrainGrid:
    fuel, elevation = unpack_landscape(value)
    manifest = value.manifest
    if source_region(fire) != manifest.region:
        raise LandscapeError(
            "LANDSCAPE_INPUT_INVALID", "Landscape region does not match the reported fire point."
        )
    transform = Transformer.from_crs(4326, manifest.crs, always_xy=True)
    x, y = transform.transform(fire.longitude, fire.latitude)
    row = math.floor((manifest.north - y) / manifest.resolution_m)
    column = math.floor((x - manifest.west) / manifest.resolution_m)
    if not 0 < row < manifest.height - 1 or not 0 < column < manifest.width - 1:
        raise LandscapeError(
            "LANDSCAPE_INPUT_INVALID", "Landscape must contain the unchanged ignition point and a halo."
        )
    nonburnable = np.isin(fuel, list(NONBURNABLE_FUELS))
    unknown = (fuel == -9999) | ((elevation == -9999) & ~nonburnable)
    if nonburnable[row, column]:
        raise LandscapeError(
            "IGNITION_DATA_CONFLICT", "Reported fire point maps to a nonburnable fuel cell; it was not moved."
        )
    valid_elevation = elevation != -9999

    def derivative(axis):
        # Standard finite differences use acquired neighbors, including one-sided data-boundary stencils.
        # No elevation is extrapolated or filled. A component with no measured neighbor remains unknown.
        previous, following = np.roll(elevation, 1, axis), np.roll(elevation, -1, axis)
        previous_valid, following_valid = (
            np.roll(valid_elevation, 1, axis),
            np.roll(valid_elevation, -1, axis),
        )
        first, last = [slice(None)] * 2, [slice(None)] * 2
        first[axis], last[axis] = 0, -1
        previous_valid[tuple(first)] = False
        following_valid[tuple(last)] = False
        result = np.full(elevation.shape, np.nan)
        both = valid_elevation & previous_valid & following_valid
        forward = valid_elevation & ~previous_valid & following_valid
        backward = valid_elevation & previous_valid & ~following_valid
        result[both] = (following[both] - previous[both]) / (2 * manifest.resolution_m)
        result[forward] = (following[forward] - elevation[forward]) / manifest.resolution_m
        result[backward] = (elevation[backward] - previous[backward]) / manifest.resolution_m
        return result

    gradient_south, gradient_x = derivative(0), derivative(1)
    unknown |= (~np.isfinite(gradient_south) | ~np.isfinite(gradient_x)) & ~nonburnable
    if unknown[row, column]:
        raise LandscapeError(
            "LANDSCAPE_NODATA", "Required fuel or elevation data is missing at the reported fire point."
        )
    gradient_y = -gradient_south
    xs = manifest.west + (np.arange(manifest.width) + 0.5) * manifest.resolution_m
    ys = manifest.north - (np.arange(manifest.height) + 0.5) * manifest.resolution_m
    inverse = Transformer.from_crs(manifest.crs, 4326, always_xy=True)
    xx, yy = np.meshgrid(xs, ys)
    longitude, latitude = inverse.transform(xx, yy)
    geod = Geod(ellps="WGS84")

    # Local geodesic Jacobian handles true north AND regional Albers grid scale distortion.
    def basis(dx, dy):
        lon2, lat2 = inverse.transform(xx + dx, yy + dy)
        azimuth, _, distance = geod.inv(longitude, latitude, lon2, lat2)
        angle = np.radians(azimuth)
        return np.sin(angle) * distance, np.cos(angle) * distance

    east_x, north_x = basis(1, 0)
    east_y, north_y = basis(0, 1)
    determinant = east_x * north_y - east_y * north_x
    gradient_east = (gradient_x * north_y - gradient_y * north_x) / determinant
    gradient_north = (east_x * gradient_y - east_y * gradient_x) / determinant
    slope = np.degrees(np.arctan(np.hypot(gradient_east, gradient_north)))
    aspect = np.degrees(np.arctan2(-gradient_east, -gradient_north)) % 360
    aspect[slope == 0] = 0  # Aspect has no physical effect for DEM-derived flat ground.
    burnable = np.isin(fuel, list(BURNABLE_FUELS)) & ~unknown
    return TerrainGrid(
        fuel,
        elevation,
        slope,
        aspect,
        east_x,
        north_x,
        east_y,
        north_y,
        gradient_east,
        gradient_north,
        burnable,
        unknown,
        (row, column),
        manifest,
    )
