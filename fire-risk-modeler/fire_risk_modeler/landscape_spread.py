"""Directed cell-arrival surface-fire demo. Native local rates, real landscape, no substitute mode."""

import heapq
import math
import time
from datetime import UTC, datetime
from functools import cache
from importlib.metadata import version
from itertools import pairwise

import numpy as np
from pyproj import Transformer
from shapely import segmentize
from shapely.geometry import box
from shapely.ops import transform, unary_union

from fire_risk_modeler.exposure import suitable_wind
from fire_risk_modeler.geometry import projection, to_geojson
from fire_risk_modeler.landscape import LandscapeError, TerrainGrid, terrain_grid
from fire_risk_modeler.models import (
    LANDSCAPE_MODEL_VERSION,
    AssessmentResult,
    ExposureFeature,
    FeatureCollection,
    FeatureProperties,
    LandscapeAssessmentRequest,
    LandscapeSummary,
)


def native_wavelet(fuel_code: int, slope: float, aspect: float, weather, moisture):
    """Native head/backing m/s and compass bearing; never rebuild for every edge bearing."""
    import pyrothermel

    if version("pyrothermel") != "0.1.4":
        raise LandscapeError("CALCULATION_FAILED", "Native behavior calculator version is unsupported.")
    fuel = pyrothermel.FuelModel.from_existing(fuel_code, units_preset="metric")
    run = pyrothermel.PyrothermelRun(
        fuel,
        moisture,
        weather.wind_speed_mps * 3.6,
        units_preset="metric",
        wind_input_mode="ten_meter",
        # A neutral angle for OBSERVED calm has no wind-vector effect; it does not invent a wind observation.
        wind_direction=weather.wind_from_degrees if weather.wind_speed_mps else 0.0,
        slope=slope,
        aspect=aspect,
        canopy_cover=0.0,
        canopy_height=0.0,
        canopy_ratio=0.0,
    )
    head = run.run_surface_fire_in_direction_of_max_spread()
    backing = run.run_surface_fire_in_direction_of_interest((head["direction"] + 180) % 360)
    speed, back, direction = head["spread_rate"] / 3.6, backing["spread_rate"] / 3.6, head["direction"]
    if not all(math.isfinite(value) for value in (speed, back, direction)) or not 0 <= back <= speed:
        raise LandscapeError(
            "CALCULATION_FAILED", "Native behavior calculator returned invalid directional rates."
        )
    eccentricity = (speed - back) / (speed + back) if speed + back else 0.0
    return speed, eccentricity, direction


def radial_rate(head: float, eccentricity: float, direction: float, bearing: float) -> float:
    """Behave's FromIgnitionPoint equation, not its different normal-front equation."""
    if head == 0:
        return 0.0
    denominator = 1 - eccentricity * math.cos(math.radians(direction - bearing))
    return head * (1 - eccentricity) / denominator if denominator > 0 else head


@cache
def stencil(radius: int):
    """Primitive integer vectors plus every crossed cell's fractional segment length.

    Zero-length corner touches are included as barriers. This prevents diagonal/long-edge lake shortcuts.
    """
    edges = []
    for dr in range(-radius, radius + 1):
        for dc in range(-radius, radius + 1):
            if not (dr or dc) or math.gcd(abs(dr), abs(dc)) != 1:
                continue
            breaks = {0.0, 1.0}
            for delta in (dr, dc):
                if delta:
                    breaks.update((index + 0.5) / abs(delta) for index in range(abs(delta)))
            ordered = sorted(breaks)
            segments = []
            barriers = set()
            for first, last in pairwise(ordered):
                middle = (first + last) / 2
                cell = (math.floor(dr * middle + 0.5), math.floor(dc * middle + 0.5))
                segments.append((*cell, last - first))
                barriers.add(cell)
            for point in ordered[1:-1]:
                row, column = round(dr * point, 12), round(dc * point, 12)
                rows = {math.floor(row + 0.5), math.ceil(row - 0.5)}
                columns = {math.floor(column + 0.5), math.ceil(column - 0.5)}
                barriers.update((r, c) for r in rows for c in columns)

            def entry(cell, dr=dr, dc=dc):
                lower = [0.0]
                for offset, delta in zip(cell, (dr, dc)):
                    if delta:
                        lower.append(min((offset - 0.5) / delta, (offset + 0.5) / delta))
                return max(lower)

            edges.append((dr, dc, tuple(segments), tuple((*cell, entry(cell)) for cell in sorted(barriers))))
    return tuple(edges)


def arrival_times(grid: TerrainGrid, weather, scenario, *, maximum_seconds: float | None):
    """Integrate positive travel time over each traversed cell; process only the requested horizon."""
    import pyrothermel

    deadline = time.monotonic() + maximum_seconds if maximum_seconds else None
    moisture = pyrothermel.MoistureScenario.from_existing(
        dead_fuel_moisture_class=scenario.moisture_profile,
        live_fuel_moisture_class=scenario.moisture_profile,
    )
    horizon = max(scenario.horizons_minutes) * 60
    states = {}
    waves = {}
    arrivals = np.full(grid.fuel.shape, np.inf)
    row, column = grid.ignition
    arrivals[row, column] = 0
    queue = [(0.0, row, column)]
    height, width = grid.fuel.shape
    edges = stencil(scenario.directional_radius)
    examined = 0

    def wavelet(r, c):
        if (r, c) not in waves:
            key = (int(grid.fuel[r, c]), float(grid.slope[r, c]), float(grid.aspect[r, c]))
            if key not in states:
                states[key] = native_wavelet(*key, weather, moisture)
            waves[r, c] = states[key]
        return waves[r, c]

    def segment_time(r, c, dr, dc, fraction):
        dx, dy = dc * grid.manifest.resolution_m, -dr * grid.manifest.resolution_m
        east = grid.east_x[r, c] * dx + grid.east_y[r, c] * dy
        north = grid.north_x[r, c] * dx + grid.north_y[r, c] * dy
        bearing = math.degrees(math.atan2(east, north)) % 360
        rate = radial_rate(*wavelet(r, c), bearing)
        if rate <= 0:
            return math.inf
        # Ground distance along the local terrain plane; projected horizontal distances are not native ROS.
        rise = grid.gradient_east[r, c] * east + grid.gradient_north[r, c] * north
        return math.sqrt(east * east + north * north + rise * rise) * fraction / rate

    while queue:
        elapsed, row, column = heapq.heappop(queue)
        if elapsed != arrivals[row, column] or elapsed > horizon:
            continue
        examined += 1
        if deadline is not None and examined % 32 == 1 and time.monotonic() > deadline:
            raise LandscapeError(
                "CALCULATION_LIMIT", "Landscape calculation exceeded its declared execution limit."
            )
        if row in (0, height - 1) or column in (0, width - 1):
            raise LandscapeError(
                "DOMAIN_TRUNCATED", "Spread reached the landscape boundary; a larger crop is required."
            )
        for dr, dc, segments, barriers in edges:
            target_r, target_c = row + dr, column + dc
            if not 0 <= target_r < height or not 0 <= target_c < width:
                continue
            if not grid.burnable[target_r, target_c] and not grid.unknown[target_r, target_c]:
                continue
            crossing = [(row + r, column + c, entry) for r, c, entry in barriers]
            if any(not 0 <= r < height or not 0 <= c < width for r, c, _entry in crossing):
                continue
            if any(not grid.burnable[r, c] and not grid.unknown[r, c] for r, c, _entry in crossing):
                continue
            # A reachable unknown region is a failed scientific input, not a pretend firebreak.
            missing = [entry for r, c, entry in crossing if grid.unknown[r, c]]
            if missing:
                entry, prefix, spent = min(missing), 0.0, 0.0
                for r, c, fraction in segments:
                    portion = max(0.0, min(fraction, entry - spent))
                    if portion:
                        prefix += segment_time(row + r, column + c, dr, dc, portion)
                    spent += fraction
                if elapsed + prefix <= horizon:
                    raise LandscapeError(
                        "LANDSCAPE_NODATA",
                        "Spread encountered missing landscape data; it was not filled or blocked as safe.",
                    )
                continue
            travel = sum(segment_time(row + r, column + c, dr, dc, fraction) for r, c, fraction in segments)
            candidate = elapsed + travel
            if candidate <= horizon and candidate < arrivals[target_r, target_c]:
                arrivals[target_r, target_c] = candidate
                heapq.heappush(queue, (candidate, target_r, target_c))
    return arrivals, len(states)


def cell_polygons(mask, manifest):
    """Merge row runs, preserving mapped holes and islands without a raster library dependency."""
    runs = []
    for row, values in enumerate(mask):
        padded = np.concatenate(([False], values, [False])).astype(np.int8)
        starts, ends = np.where(np.diff(padded) == 1)[0], np.where(np.diff(padded) == -1)[0]
        top = manifest.north - row * manifest.resolution_m
        for start, end in zip(starts, ends):
            runs.append(
                box(
                    manifest.west + start * manifest.resolution_m,
                    top - manifest.resolution_m,
                    manifest.west + end * manifest.resolution_m,
                    top,
                )
            )
    return unary_union(runs)


def assess_landscape(
    request: LandscapeAssessmentRequest,
    *,
    maximum_seconds: float | None = None,
) -> AssessmentResult:
    """One declared mode. Failures never return exposure or homogeneous geometry."""
    assumptions = [
        "Terrain/fuel-informed surface-fire demo; not a calibrated probability or evacuation forecast.",
        "Reported point treated as demo ignition, not a verified current active fire front.",
        f"Assumed {request.scenario.moisture_profile} dead/live fuel moisture; humidity is not fuel moisture.",
        request.scenario.wind_height_assumption,
        "Static DTN wind; local terrain and mapped LANDFIRE fuel vary across the grid.",
        "Directed finite-grid arrival approximation; no spotting, crown fire, suppression or urban structure fire.",
        "Native surface rates integrated over terrain-plane distances; no-data cells are not established as safe.",
    ]
    base = {
        "schema_version": "2",
        "assessment_id": request.assessment_id,
        "fire_ref": request.fire_ref,
        "input_hash": request.input_hash,
        "model_version": LANDSCAPE_MODEL_VERSION,
        "mode": "landscape",
        "calculated_at": datetime.now(UTC).isoformat(),
        "fire": request.fire,
        "weather": request.weather,
        "scenario": request.scenario,
        "assumptions": assumptions,
    }
    try:
        if not suitable_wind(request):
            raise LandscapeError(
                "DTN_WIND_UNUSABLE", "Landscape mode requires usable current DTN wind at the fire point."
            )
        grid = terrain_grid(request.landscape, request.fire)
        arrivals, state_count = arrival_times(
            grid, request.weather, request.scenario, maximum_seconds=maximum_seconds
        )
        forward, _ = projection(request.fire)
        converter = Transformer.from_crs(grid.manifest.crs, forward.target_crs, always_xy=True)
        features = []
        for minutes in request.scenario.horizons_minutes:
            footprint = cell_polygons(arrivals <= minutes * 60, grid.manifest)
            # Every outline uses the same cell-edge vertices before nonlinear projection. Otherwise a later
            # longer straight chord can exclude an earlier edge despite cumulative raster arrivals.
            footprint = segmentize(footprint, grid.manifest.resolution_m)
            geometry = to_geojson(transform(converter.transform, footprint), request.fire)
            features.append(
                ExposureFeature(
                    geometry=geometry,
                    properties=FeatureProperties(
                        kind="landscape",
                        label=f"{minutes:g} minutes — landscape surface-fire demo",
                        elapsed_minutes=minutes,
                        wind_from_degrees=request.weather.wind_from_degrees,
                    ),
                )
            )
        row, column = grid.ignition
        summary = LandscapeSummary(
            manifest=grid.manifest,
            directional_radius=request.scenario.directional_radius,
            ignition_row=row,
            ignition_column=column,
            ignition_fuel_model=int(grid.fuel[row, column]),
            ignition_slope_degrees=float(grid.slope[row, column]),
            ignition_aspect_degrees=float(grid.aspect[row, column]),
            reached_cells=int(np.isfinite(arrivals).sum()),
            spread_cells=max(0, int(np.isfinite(arrivals).sum()) - 1),
            modeled_cells=int(grid.burnable.sum()),
            nonburnable_cells=int((~grid.burnable & ~grid.unknown).sum()),
            nodata_cells=int(grid.unknown.sum()),
            native_states=state_count,
            maximum_arrival_minutes=max(request.scenario.horizons_minutes),
        )
        return AssessmentResult(
            **base,
            availability="available",
            geojson=FeatureCollection(features=features),
            landscape=summary,
        )
    except LandscapeError as exc:
        return AssessmentResult(
            **base,
            availability="unavailable",
            geojson=FeatureCollection(),
            failure=exc.failure,
            warnings=[exc.failure.message],
        )
