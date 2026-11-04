"""Road-coverage decisions for one pinned source set and one unchanged incident point."""
import math

from traffic_simulator import osm_corridors
from traffic_simulator.errors import IntegrationError


class RoadCoverageUnavailable(IntegrationError):
    """A successfully examined extent lacks usable roads; operational errors never use this carrier."""

    def __init__(self, code, message, *, details=None, source_manifest=None):
        if code not in ("NO_DRIVABLE_ROADS", "NO_ROUTABLE_CORRIDOR"):
            raise ValueError("Unsupported road-coverage reason")
        super().__init__(code, message, 422)
        self.details = details or {}
        self.source_manifest = source_manifest


def source_snapshot(manifest):
    """Only public source identity enters coverage evidence, never transport headers or local paths."""
    if manifest is None:
        return None
    return {
        "publisher": manifest["publisher"], "snapshot_timestamp": manifest["snapshot_timestamp"],
        "recipe": manifest["recipe"], "osmium_version": manifest["osmium_version"],
        "sources": [{key: source[key] for key in ("region_id", "bytes", "sha256")}
                    for source in manifest["sources"]],
        **({"required_us_bounds": manifest["required_us_bounds"],
            "includes_unsupported_area": manifest["includes_unsupported_area"]}
           if "required_us_bounds" in manifest else {}),
    }


def pin_source(selected, proposed):
    """Source-byte identity cannot change between geographic candidates or at publication."""
    if proposed is None:
        return selected
    if selected is not None:
        # Local import avoids coupling the extract module's coverage exception to source-resolution imports.
        from traffic_simulator.osm_extracts import identity

        if identity(selected) != identity(proposed):
            raise IntegrationError("OSM_SOURCE_INVALID", "The road source changed during extent planning.", 502)
        return selected
    return proposed


def coverage(scenario, mode, attempts, manifest, code, message):
    searched = max((attempt["radius_km"] for attempt in attempts), default=scenario.radius_km)
    return {
        "reason_code": code, "message": message, "extent_mode": mode,
        "requested_radius_km": scenario.radius_km if mode == "fixed" else None,
        "searched_radius_km": searched,
        "study_bounds": osm_corridors.study_bounds(scenario.lat, scenario.lon, searched),
        "attempts": attempts, "source_snapshot": source_snapshot(manifest),
    }


def network_bounds(network, longitude):
    """Disclose complete retained-way extent separately from the query box, including the date line."""
    coordinates = [coordinate for feature in network.get("features", [])
                   for coordinate in feature["geometry"]["coordinates"]]
    if not coordinates:
        return []
    longitudes = [longitude + (coordinate[0] - longitude + 180) % 360 - 180 for coordinate in coordinates]
    west, east = min(longitudes), max(longitudes)
    south, north = min(value[1] for value in coordinates), max(value[1] for value in coordinates)
    if west < -180:
        return [[west + 360, south, 180, north], [-180, south, east, north]]
    if east > 180:
        return [[west, south, 180, north], [-180, south, east - 360, north]]
    return [[west, south, east, north]]


def next_radius(radius, maximum, details):
    """Expand the search envelope or supply the interior margin required by actual mapped roads.

    Doubling searches a geometrically larger area when no usable road distance is known. When the native graph
    reports a corridor just outside its interior admission boundary, derive the next area from that unrounded
    distance. This is geographic planning, not recovery from a provider or native-tool error.
    """
    distance = details.get("nearest_corridor_distance_km")
    if distance is not None and distance > radius * 0.8:
        candidate = math.nextafter(distance / 0.8, math.inf)
    else:
        candidate = radius * 2
    return min(maximum, candidate)
