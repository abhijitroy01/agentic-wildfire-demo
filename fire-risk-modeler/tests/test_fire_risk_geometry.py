"""Fire exposure physical distance, topology, longitude seams and reported-perimeter handling."""
import math
from itertools import pairwise

import pytest
from fire_risk_modeler.exposure import assess_exposure
from fire_risk_modeler.geometry import projection
from pyproj import Geod
from shapely.geometry import shape


@pytest.mark.parametrize("latitude,longitude", [
    (0.0, 0.0), (47.6, -122.3), (-33.8, 151.2), (0.0, 179.99), (0.0, -179.99),
    (89.9, 179.99), (-89.9, -179.99), (90.0, 0.0), (-90.0, 0.0),
])
def test_global_coordinate_geometry(latitude, longitude, make_request):
    result = assess_exposure(make_request(latitude=latitude, longitude=longitude, weather=False))
    assert result.fire.latitude == latitude
    assert result.fire.longitude == longitude
    assert result.geojson.features
    for feature in result.geojson.features:
        geometry = feature.geometry
        assert shape(geometry.model_dump(mode="json")).is_valid
        polygons = [geometry.coordinates] if geometry.type == "Polygon" else geometry.coordinates
        for polygon in polygons:
            for ring in polygon:
                assert ring[0] == ring[-1]
                for first, second in pairwise(ring):
                    assert math.isfinite(first[0]) and math.isfinite(first[1])
                    if abs(first[0] - second[0]) > 180:
                        assert first[1] == second[1] and abs(first[1]) == 90


def test_distances_are_metres_and_bands_have_holes(make_request):
    result = assess_exposure(make_request(latitude=0.0, longitude=0.0, weather=False))
    first, second = result.geojson.features[:2]
    assert first.geometry.type == "Polygon"
    geod = Geod(ellps="WGS84")
    for lon, lat in first.geometry.coordinates[0]:
        _, _, distance = geod.inv(0, 0, lon, lat)
        assert distance == pytest.approx(first.properties.outer_km * 1000, abs=0.02)
    assert len(second.geometry.coordinates) == 2
    assert not shape(second.geometry.model_dump(mode="json")).contains(
        shape(first.geometry.model_dump(mode="json")).centroid
    )


def test_dateline_is_split_into_local_parts(make_request):
    result = assess_exposure(make_request(latitude=0.0, longitude=179.99, weather=False))
    for feature in result.geojson.features:
        assert feature.geometry.type == "MultiPolygon"
        for polygon in shape(feature.geometry.model_dump(mode="json")).geoms:
            assert polygon.bounds[2] - polygon.bounds[0] < 1


def test_matched_perimeter_changes_proximity_context(make_request):
    perimeter = {"type": "Polygon", "coordinates": [[
        [-0.01, -0.01], [0.01, -0.01], [0.01, 0.01], [-0.01, 0.01], [-0.01, -0.01],
    ]]}
    request = make_request(latitude=0.0, longitude=0.0, weather=False,
                           fire={"latitude": 0.0, "longitude": 0.0, "perimeter": perimeter})
    result = assess_exposure(request)
    forward, _ = projection(request.fire)
    last = result.geojson.features[0].geometry.coordinates[0]
    assert max(math.hypot(*forward.transform(lon, lat)) for lon, lat in last) > 6000
    assert any("edge is not assumed active" in assumption for assumption in result.assumptions)


def test_invalid_perimeter_preserves_point_context(make_request):
    perimeter = {"type": "Polygon", "coordinates": [[
        [-0.01, -0.01], [0.01, 0.01], [0.01, -0.01], [-0.01, 0.01], [-0.01, -0.01],
    ]]}
    result = assess_exposure(make_request(
        latitude=0.0, longitude=0.0, weather=False,
        fire={"latitude": 0.0, "longitude": 0.0, "perimeter": perimeter},
    ))
    assert result.availability == "available"
    assert any("invalid" in warning for warning in result.warnings)
