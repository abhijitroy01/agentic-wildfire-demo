"""Sealed real-data contracts, native conventions and honest landscape outcomes."""

import base64
import hashlib
import json
import zlib
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from uuid import uuid4

import numpy as np
import pyrothermel
import pytest
from fire_risk_modeler import landscape_spread
from fire_risk_modeler.landscape import (
    BURNABLE_FUELS,
    REGIONAL_CRS,
    LandscapeError,
    regional_sources,
    seal_landscape,
    source_region,
    terrain_grid,
    unpack_landscape,
)
from fire_risk_modeler.models import (
    SURFACE_WIND_ASSUMPTION,
    FireObservation,
    LandscapeAssessmentRequest,
    LandscapeScenario,
    model_input_hash,
)
from pydantic import ValidationError
from pyproj import Transformer
from shapely.geometry import shape

FIRE = FireObservation(latitude=47.470805726545464, longitude=-121.25588168570884)


def make_landscape(size=51, *, fuel=None, elevation=None, resolution=30.0, fire=FIRE):
    region = source_region(fire)
    crs = REGIONAL_CRS[region]
    x, y = Transformer.from_crs(4326, crs, always_xy=True).transform(fire.longitude, fire.latitude)
    fuel = np.full((size, size), 122) if fuel is None else fuel
    elevation = np.full((size, size), 1000.0) if elevation is None else elevation
    sources = regional_sources(region)
    values = seal_landscape(
        fuel,
        elevation,
        region=region,
        fuel_source=sources[0],
        elevation_source=sources[1],
        crs=crs,
        west=float(x - size * resolution / 2),
        north=float(y + size * resolution / 2),
        resolution_m=30.0,
        captured_at="2026-10-27T16:40:00Z",
        fuel_sha256="f" * 64,
        elevation_sha256="e" * 64,
    )
    if resolution != 30:
        # Numerical refinement tests use synthetic grids; public acquisition remains canonical 30 metres.
        values = values.model_copy(
            update={"manifest": values.manifest.model_copy(update={"resolution_m": resolution})}
        )
    return values


def request_for(values=None, *, wind=5.0, direction=270.0, radius=5, horizons=None, fire=FIRE):
    request = LandscapeAssessmentRequest(
        assessment_id=str(uuid4()),
        fire_ref="source-backed-test-fire",
        input_hash="0" * 64,
        fire=fire,
        weather={
            "query_latitude": fire.latitude,
            "query_longitude": fire.longitude,
            "availability": "available",
            "observed_at": "2026-10-27T16:00:00Z",
            "wind_speed_mps": wind,
            "wind_from_degrees": direction,
        },
        scenario=LandscapeScenario(
            moisture_profile="moderate",
            wind_height_assumption=SURFACE_WIND_ASSUMPTION,
            initialization="reported point treated as demo ignition",
            horizons_minutes=horizons or [15.0, 30.0, 60.0],
            directional_radius=radius,
        ),
        landscape=values or make_landscape(fire=fire),
    )
    return request.model_copy(update={"input_hash": model_input_hash(request)})


def test_fuel_and_elevation_seal_roundtrip_and_bounded_decompression():
    value = make_landscape()
    fuel, elevation = unpack_landscape(value)
    assert fuel.dtype == np.dtype("<i2") and elevation.dtype == np.dtype("<f4")
    assert (fuel == 122).all() and (elevation == 1000).all()
    corrupt = value.model_copy(
        update={"raster_data": base64.b64encode(zlib.compress(b"x" * 100_000)).decode()}
    )
    with pytest.raises(LandscapeError, match="seal"):
        unpack_landscape(corrupt)
    # Extra data, concatenated streams and valid-size wrong hashes are also rejected.
    for payload in [b"x" * (51 * 51 * 6), zlib.decompress(base64.b64decode(value.raster_data)) + b"x"]:
        corrupt = value.model_copy(update={"raster_data": base64.b64encode(zlib.compress(payload)).decode()})
        with pytest.raises(LandscapeError):
            unpack_landscape(corrupt)


@pytest.mark.parametrize("fuel_code", sorted(BURNABLE_FUELS))
def test_standard_fbfm40_native_radial_equation(fuel_code):
    weather = request_for().weather
    moisture = pyrothermel.MoistureScenario.from_existing("moderate", "moderate")
    head, eccentricity, direction = landscape_spread.native_wavelet(fuel_code, 23.4, 135.0, weather, moisture)
    native = pyrothermel.PyrothermelRun(
        pyrothermel.FuelModel.from_existing(fuel_code),
        moisture,
        18.0,
        wind_input_mode="ten_meter",
        wind_direction=270.0,
        slope=23.4,
        aspect=135.0,
        canopy_cover=0.0,
        canopy_height=0.0,
        canopy_ratio=0.0,
    )
    for bearing in range(0, 360, 15):
        actual = native.run_surface_fire_in_direction_of_interest(float(bearing))["spread_rate"] / 3.6
        assert landscape_spread.radial_rate(head, eccentricity, direction, bearing) == pytest.approx(
            actual, rel=1e-10
        )


def test_terrain_slope_aspect_true_north_and_no_flat_fill():
    # A numerical plane rises along projected +x. Its aspect must point geographically downslope.
    elevation = np.tile(np.arange(51) * 30.0, (51, 1))
    grid = terrain_grid(make_landscape(elevation=elevation), FIRE)
    row, col = grid.ignition
    # The physical gradient is perpendicular to the projected constant-x direction.
    expected = (np.degrees(np.arctan2(-grid.north_y[row, col], grid.east_y[row, col])) + 360) % 360
    assert grid.aspect[row, col] == pytest.approx(expected, abs=0.001)
    assert 44 < grid.slope[row, col] < 47  # Physical slope includes Albers scale distortion.
    elevation[row, col] = -9999
    with pytest.raises(LandscapeError) as exc:
        terrain_grid(make_landscape(elevation=elevation), FIRE)
    assert exc.value.failure.code == "LANDSCAPE_NODATA"


def test_mapped_water_is_not_missing_data_and_ignition_is_never_moved():
    fuel = np.full((51, 51), 122)
    fuel[25, 25] = 98
    result = landscape_spread.assess_landscape(request_for(make_landscape(fuel=fuel)))
    assert result.failure.code == "IGNITION_DATA_CONFLICT"
    assert result.mode == "landscape" and not result.geojson.features
    assert result.fire == FIRE


def test_coastal_land_uses_measured_one_sided_gradient_without_filling_ocean():
    fuel = np.full((51, 51), 122)
    elevation = np.tile(np.arange(51) * 10.0, (51, 1))
    fuel[:, 26:] = 98
    elevation[:, 26:] = -9999
    grid = terrain_grid(make_landscape(fuel=fuel, elevation=elevation), FIRE)
    assert not grid.unknown[25, 25]
    assert 17 < grid.slope[25, 25] < 21
    assert not grid.burnable[:, 26:].any()
    assert (grid.elevation[:, 26:] == -9999).all()


@pytest.mark.parametrize(
    "fire,region",
    [
        (FireObservation(latitude=47.6, longitude=-122.3), "CONUS"),
        (FireObservation(latitude=64.0, longitude=-150.0), "AK"),
        (FireObservation(latitude=52.0, longitude=179.0), "AK"),
        (FireObservation(latitude=20.0, longitude=-156.0), "HI"),
    ],
)
def test_one_product_version_for_each_declared_region(fire, region):
    assert source_region(fire) == region
    fuel, elevation = regional_sources(region)
    assert f"LF2024_FBFM40_{region}" in fuel and f"LF2020_Elev_{region}" in elevation


def test_missing_wind_fails_but_observed_calm_is_a_real_input():
    missing = request_for(wind=None, direction=None)
    result = landscape_spread.assess_landscape(missing)
    assert result.failure.code == "DTN_WIND_UNUSABLE" and not result.geojson.features
    result = landscape_spread.assess_landscape(request_for(wind=0.0, direction=None))
    assert result.availability == "available" and result.weather.wind_speed_mps == 0
    assert result.scenario.engine == "Pyrothermel/Behave directional arrival grid"


def test_outlines_are_cumulative_valid_and_not_substituted():
    result = landscape_spread.assess_landscape(request_for())
    assert result.availability == "available" and result.mode == "landscape"
    assert result.landscape.ignition_fuel_model == 122 and result.landscape.spread_cells > 0
    geometries = [shape(feature.geometry.model_dump()) for feature in result.geojson.features]
    assert all(value.is_valid for value in geometries)
    assert all(second.covers(first) for first, second in pairwise(geometries))
    assert all(feature.properties.kind == "landscape" for feature in result.geojson.features)
    assert len(json.dumps(result.model_dump(mode="json")).encode()) < 128 * 1024


def test_corrupted_manifest_and_unsupported_codes_never_use_uniform_fuel():
    value = make_landscape(fuel=np.full((51, 51), 110))
    result = landscape_spread.assess_landscape(request_for(value))
    assert result.failure.code == "FUEL_CODE_UNSUPPORTED" and not result.geojson.features
    value = make_landscape()
    value = value.model_copy(
        update={"manifest": value.manifest.model_copy(update={"raster_sha256": "0" * 64})}
    )
    result = landscape_spread.assess_landscape(request_for(value))
    assert result.failure.code == "LANDSCAPE_INPUT_INVALID"
    assert hashlib.sha256(base64.b64decode(value.raster_data)).hexdigest() != "0" * 64


def ideal_grid(size=61, resolution=30.0):
    """Orthogonal physical plane for solver convergence proofs, independent of reprojection error."""
    grid = terrain_grid(make_landscape(size=size, resolution=resolution), FIRE)
    ones, zeros = np.ones((size, size)), np.zeros((size, size))
    return replace(
        grid,
        east_x=ones,
        north_x=zeros,
        east_y=zeros,
        north_y=ones,
        gradient_east=zeros,
        gradient_north=zeros,
    )


def test_water_wall_and_diagonal_corner_are_not_crossed_by_long_edges(monkeypatch):
    monkeypatch.setattr(landscape_spread, "native_wavelet", lambda *args: (1.0, 0.0, 0.0))
    grid = ideal_grid()
    burnable = grid.burnable.copy()
    burnable[:, 33] = False
    grid = replace(grid, burnable=burnable)
    request = request_for(horizons=[2.0])
    arrival, _ = landscape_spread.arrival_times(grid, request.weather, request.scenario, maximum_seconds=10)
    assert np.isfinite(arrival[:, :33]).any() and not np.isfinite(arrival[:, 34:]).any()
    grid = ideal_grid()
    burnable = np.zeros_like(grid.burnable)
    burnable[30, 30] = burnable[31, 31] = True
    grid = replace(grid, burnable=burnable)
    arrival, _ = landscape_spread.arrival_times(grid, request.weather, request.scenario, maximum_seconds=10)
    assert not np.isfinite(arrival[31, 31])


def test_domain_and_reachable_unknown_are_typed_failures(monkeypatch):
    monkeypatch.setattr(landscape_spread, "native_wavelet", lambda *args: (1.0, 0.0, 0.0))
    tiny = request_for(make_landscape(size=11), horizons=[10.0])
    result = landscape_spread.assess_landscape(tiny)
    assert result.failure.code == "DOMAIN_TRUNCATED" and not result.geojson.features
    grid = ideal_grid()
    unknown = grid.unknown.copy()
    unknown[:, 33] = True
    grid = replace(grid, unknown=unknown, burnable=grid.burnable & ~unknown)
    request = request_for(horizons=[2.0])
    with pytest.raises(LandscapeError) as exc:
        landscape_spread.arrival_times(grid, request.weather, request.scenario, maximum_seconds=10)
    assert exc.value.failure.code == "LANDSCAPE_NODATA"


def test_zero_native_spread_is_not_a_fake_failure(monkeypatch):
    monkeypatch.setattr(landscape_spread, "native_wavelet", lambda *args: (0.0, 0.0, 0.0))
    result = landscape_spread.assess_landscape(request_for())
    assert result.availability == "available" and result.landscape.spread_cells == 0
    assert result.landscape.reached_cells == 1


def test_contract_rejects_success_without_landscape_proof():
    result = landscape_spread.assess_landscape(request_for())
    data = result.model_dump(mode="json")
    from fire_risk_modeler.models import AssessmentResult

    for change in [
        {"landscape": None},
        {"geojson": {"type": "FeatureCollection", "features": []}},
        {"schema_version": "1"},
        {"mode": "proximity"},
    ]:
        with pytest.raises(ValidationError):
            AssessmentResult.model_validate({**data, **change})


def test_directional_and_spatial_refinement_against_native_wavelet(monkeypatch):
    head, eccentricity, direction = 1.0, 0.6, 37.0
    monkeypatch.setattr(landscape_spread, "native_wavelet", lambda *args: (head, eccentricity, direction))
    errors = []
    for radius, resolution in [(1, 30.0), (5, 30.0), (5, 15.0)]:
        grid = ideal_grid(size=81, resolution=resolution)
        request = request_for(radius=radius, horizons=[6.0])
        arrivals, _ = landscape_spread.arrival_times(
            grid, request.weather, request.scenario, maximum_seconds=20
        )
        row, column = np.indices(arrivals.shape)
        east, north = (column - 40) * resolution, -(row - 40) * resolution
        bearing = np.degrees(np.arctan2(east, north))
        speed = head * (1 - eccentricity) / (1 - eccentricity * np.cos(np.radians(direction - bearing)))
        expected = np.hypot(east, north) / speed
        sample = (
            np.isfinite(arrivals) & (expected > 90) & (expected < 300) & (east % 30 == 0) & (north % 30 == 0)
        )
        errors.append(float(np.mean(np.abs(arrivals[sample] / expected[sample] - 1))))
    assert errors[1] < errors[0] / 4
    # Compare identical physical targets. Half-size cells admit additional paths without claiming that
    # spatial refinement alone removes the finite directional stencil's residual angular error.
    assert errors[2] <= errors[1] + 1e-10
    assert errors[2] < 0.01


def test_official_king_crop_has_real_fuel_terrain_and_native_grid_result():
    fixture = json.loads((Path(__file__).parent / "data" / "king-landscape.json").read_text())
    from fire_risk_modeler.models import LandscapeInput

    values = LandscapeInput.model_validate(fixture["landscape"])
    grid = terrain_grid(values, FIRE)
    assert len(np.unique(grid.fuel)) > 5
    assert (grid.fuel == 98).any() and np.ptp(grid.elevation) > 500
    result = landscape_spread.assess_landscape(request_for(values))
    assert result.availability == "available" and result.failure is None
    assert 23 < result.landscape.ignition_slope_degrees < 24
    assert result.landscape.ignition_fuel_model == 122
    assert result.landscape.native_states > 1
    assert [feature.properties.elapsed_minutes for feature in result.geojson.features] == [15, 30, 60]


@pytest.mark.parametrize(
    "latitude,longitude,region",
    [
        (64.0, -150.0, "AK"),
        (52.0, 179.9999, "AK"),
        (20.0, -156.0, "HI"),
    ],
)
def test_regional_geometry_uses_actual_seed_and_handles_alaska_dateline(latitude, longitude, region):
    fire = FireObservation(latitude=latitude, longitude=longitude)
    result = landscape_spread.assess_landscape(request_for(fire=fire, wind=0.0, direction=None))
    assert result.availability == "available"
    assert result.fire == fire and result.landscape.manifest.region == region
    assert all(shape(feature.geometry.model_dump()).is_valid for feature in result.geojson.features)
    if longitude > 179.99:
        assert any(feature.geometry.type == "MultiPolygon" for feature in result.geojson.features)
