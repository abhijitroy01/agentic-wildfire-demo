"""Metric construction and antimeridian-safe WGS84 Polygon/MultiPolygon output."""
import json
import math

from pyproj import CRS, Transformer
from shapely import affinity, get_parts, orient_polygons, set_precision
from shapely.geometry import MultiPolygon, Point, Polygon, box, mapping, shape
from shapely.ops import transform, unary_union

from fire_risk_modeler.models import FireObservation, MultiPolygonGeometry, PolygonGeometry


def projection(fire: FireObservation) -> tuple[Transformer, Transformer]:
    """An azimuthal equidistant frame preserves distances from the reported fire coordinate."""
    local = CRS.from_proj4(
        f"+proj=aeqd +lat_0={fire.latitude} +lon_0={fire.longitude} +datum=WGS84 +units=m +no_defs"
    )
    return (
        Transformer.from_crs("EPSG:4326", local, always_xy=True),
        Transformer.from_crs(local, "EPSG:4326", always_xy=True),
    )


def source_geometry(fire: FireObservation):
    """Use a matched perimeter as proximity context, otherwise use the reported point."""
    if fire.perimeter is None:
        return Point(0, 0)
    forward, _ = projection(fire)
    projected = transform(forward.transform, shape(fire.perimeter.model_dump(mode="json")))
    if projected.is_empty or not projected.is_valid:
        raise ValueError("Reported perimeter is invalid in the fire's metric coordinate frame")
    if any(not math.isfinite(value) for value in projected.bounds):
        raise ValueError("Reported perimeter cannot be projected")
    return projected


def distance_band(source, inner_km: float, outer_km: float, ring_points: int):
    outer = source.buffer(outer_km * 1000, quad_segs=ring_points // 4)
    return outer.difference(source.buffer(inner_km * 1000, quad_segs=ring_points // 4)) if inner_km else outer


def ellipse(head_m: float, back_m: float, flank_m: float, wind_to_degrees: float, ring_points: int):
    """A local metric ellipse with the reported point as its documented scenario origin."""
    if not all(math.isfinite(value) and value > 0 for value in (head_m, back_m, flank_m)):
        raise ValueError("Ellipse distances must be finite and positive")
    semimajor = (head_m + back_m) / 2
    center = (head_m - back_m) / 2
    outline = Polygon([
        (center + semimajor * math.cos(index * 2 * math.pi / ring_points),
         flank_m * math.sin(index * 2 * math.pi / ring_points))
        for index in range(ring_points)
    ])
    return affinity.rotate(outline, 90 - wind_to_degrees, origin=(0, 0))


def _unwrap_ring(ring, inverse: Transformer, latitude: float, longitude: float) -> Polygon:
    positions = []
    for x, y in ring.coords:
        lon, lat = inverse.transform(x, y)
        if not math.isfinite(lon) or not math.isfinite(lat):
            raise ValueError("Model geometry cannot be transformed to WGS84")
        reference = positions[-1][0] if positions else longitude
        while lon - reference > 180:
            lon -= 360
        while lon - reference < -180:
            lon += 360
        positions.append((lon, max(-90.0, min(90.0, lat))))
    # A ring containing a pole winds once around longitude. Close it through that pole rather than drawing
    # a diagonal across the world. The resulting cap is split into bounded longitude windows below.
    if abs(positions[-1][0] - positions[0][0]) > 180:
        pole = 90.0 if latitude >= 0 else -90.0
        positions.extend([(positions[-1][0], pole), (positions[0][0], pole), positions[0]])
    else:
        positions[-1] = positions[0]
    result = Polygon(positions)
    if result.is_empty or not result.is_valid:
        raise ValueError("Model ring is invalid after longitude unwrapping")
    return result


def _polygons(value):
    return [part for part in get_parts(value) if isinstance(part, Polygon) and part.area > 0]


def _wrap_polygon(value: Polygon):
    lower = math.floor((value.bounds[0] + 180) / 360)
    upper = math.floor((value.bounds[2] + 180) / 360)
    parts = []
    for window in range(lower, upper + 1):
        cut = value.intersection(box(-180 + 360 * window, -90, 180 + 360 * window, 90))
        parts.extend(affinity.translate(part, xoff=-360 * window) for part in _polygons(cut))
    return unary_union(parts)


def to_geojson(value, fire: FireObservation) -> PolygonGeometry | MultiPolygonGeometry:
    """Split longitude crossings, preserve holes and correctly close polar caps."""
    _, inverse = projection(fire)
    output = []
    for polygon in _polygons(value):
        shell = _wrap_polygon(_unwrap_ring(polygon.exterior, inverse, fire.latitude, fire.longitude))
        holes = [
            _wrap_polygon(_unwrap_ring(ring, inverse, fire.latitude, fire.longitude))
            for ring in polygon.interiors
        ]
        if holes:
            shell = shell.difference(unary_union(holes))
        output.extend(_polygons(shell))
    if not output:
        raise ValueError("Calculation produced no polygon geometry")
    # Snap seam coordinates to a centimetre-scale angular grid to avoid tiny topology slivers at +/-180.
    joined = set_precision(unary_union(output), 1e-10)
    parts = _polygons(joined)
    final = orient_polygons(parts[0] if len(parts) == 1 else MultiPolygon(parts))
    if final.is_empty or not final.is_valid:
        raise ValueError("Calculation produced invalid polygon geometry")
    document = json.loads(json.dumps(mapping(final)))
    return (PolygonGeometry if document["type"] == "Polygon" else MultiPolygonGeometry).model_validate(document)


def vertex_count(geometry: PolygonGeometry | MultiPolygonGeometry) -> int:
    polygons = [geometry.coordinates] if geometry.type == "Polygon" else geometry.coordinates
    return sum(len(ring) for polygon in polygons for ring in polygon)
