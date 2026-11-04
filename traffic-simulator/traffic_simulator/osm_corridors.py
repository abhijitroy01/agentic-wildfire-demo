"""OSM road-corridor geometry and identities for bounded study areas.

Names, references and geometry are source observations. A corridor is a candidate for a
modeled exit, not a verified evacuation route, road closure or safe destination.
"""
import hashlib
import math
import re
from collections import Counter
from itertools import pairwise
from xml.etree import ElementTree

from traffic_simulator.corridor import segment_distance_km

ROAD_CLASSES = (
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
    "residential", "living_street", "service",
)
ATTRIBUTION = "© OpenStreetMap contributors, ODbL 1.0"


def study_bounds(latitude, longitude, radius_km):
    """Return conservative WGS84 boxes [west, south, east, north], split at the antimeridian.

    The smallest WGS84 curvature radius bounds the angular extent conservatively.
    This is a provider query extent, never a fire-hazard or safety boundary.
    """
    angular = radius_km * 1000 / 6_335_439
    latitude_radians = math.radians(latitude)
    south, north = max(-90, latitude-math.degrees(angular)), min(90, latitude+math.degrees(angular))
    if south == -90 or north == 90:
        return [[-180.0, south, 180.0, north]]
    width = math.degrees(math.asin(min(1.0, math.sin(angular) / math.cos(latitude_radians))))
    west, east = longitude-width, longitude+width
    if west < -180:
        return [[west+360, south, 180.0, north], [-180.0, south, east, north]]
    if east > 180:
        return [[west, south, 180.0, north], [-180.0, south, east-360, north]]
    return [[west, south, east, north]]


def projection(latitude, longitude):
    """Local meter projection used by netconvert, including study areas near the date line."""
    return f"+proj=aeqd +lat_0={latitude:.8f} +lon_0={longitude:.8f} +datum=WGS84 +units=m +no_defs"


def _identity(tags, identifier):
    reference = " ".join(tags.get("ref", "").split())
    name = " ".join(tags.get("name", "").split())
    if reference or name:
        kind, value = ("ref", reference) if reference else ("name", name)
        key = hashlib.sha256(value.casefold().encode()).hexdigest()[:20]
        corridor_id = f"osm:{kind}:{key}"
    else:
        corridor_id = f"osm:way:{identifier}"
    label = " · ".join(value for value in (reference, name) if value) or f"OSM road {identifier}"
    return corridor_id, label, reference or None, name or None


def parse_xml(raw):
    """Validate the bounded road XML before accepting a source snapshot."""
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("OSM road XML contains unsupported declarations")
    try:
        root = ElementTree.fromstring(raw)  # nosec B314 - declarations rejected; bounded public OSM bytes
    except ElementTree.ParseError:
        raise ValueError("Incomplete OSM road response") from None
    if root.tag != "osm" or root.find("remark") is not None:
        raise ValueError("Incomplete OSM road response")
    return root


def geometry_from_xml(raw):
    """Parse the bounded road snapshot, refusing malformed or incomplete geometry.

    The provider downloader limits raw bytes. Entity/DTD declarations are forbidden;
    a missing referenced node is an incomplete response, never an invented segment.
    """
    root = parse_xml(raw)
    nodes = {}
    for node in root.findall("node"):
        try:
            longitude, latitude = float(node.attrib["lon"]), float(node.attrib["lat"])
        except (KeyError, ValueError):
            raise ValueError("OSM road response contains invalid coordinates") from None
        if not math.isfinite(longitude + latitude) or not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise ValueError("OSM road response contains invalid coordinates")
        nodes[node.attrib["id"]] = [longitude, latitude]
    features = []
    try:
        ways = sorted(root.findall("way"), key=lambda item: int(item.attrib["id"]))
    except (KeyError, ValueError):
        raise ValueError("OSM road response contains an invalid way ID") from None
    for way in ways:
        tags = {tag.attrib["k"]: tag.attrib["v"] for tag in way.findall("tag")}
        road_class = tags.get("highway", "")
        base_class = road_class.removesuffix("_link")
        access = next((tags[key] for key in ("motorcar", "motor_vehicle", "vehicle", "access")
                       if key in tags), "yes")
        if base_class not in ROAD_CLASSES or access in ("no", "private"):
            continue
        identifier = int(way.attrib["id"])
        if identifier <= 0:
            raise ValueError("OSM road response contains an invalid way ID")
        try:
            coordinates = [nodes[point.attrib["ref"]] for point in way.findall("nd")]
        except KeyError:
            raise ValueError("OSM road response is missing referenced geometry") from None
        if len(coordinates) < 2:
            raise ValueError("OSM road response contains an incomplete road")
        corridor_id, label, reference, name = _identity(tags, identifier)
        features.append({
            "type": "Feature", "id": identifier,
            "geometry": {"type": "LineString", "coordinates": coordinates},
            "properties": {"corridor_id": corridor_id, "label": label, "source_way_id": identifier,
                           "source": "OpenStreetMap", "route_ref": reference, "name": name,
                           "road_class": road_class},
        })
    return {"type": "FeatureCollection", "features": features}


def coverage_from_xml(raw, geometry):
    """Retain road-class counts even when a valid extract contains only trails or restricted vehicle ways."""
    root = parse_xml(raw)
    classes = Counter()
    for way in root.findall("way"):
        tags = {tag.attrib["k"]: tag.attrib["v"] for tag in way.findall("tag")}
        if tags.get("highway"):
            classes[tags["highway"]] += 1
    return {"highway_classes": dict(sorted(classes.items())),
            "mapped_highway_ways": sum(classes.values()), "passenger_ways": len(geometry["features"])}


def rank_corridors(collection, latitude, longitude, radius_km):
    """Group named/referenced road ways and rank their approximate proximity deterministically."""
    grouped = {}
    for feature in collection["features"]:
        coordinates = feature["geometry"]["coordinates"]
        distance = min(segment_distance_km(latitude, longitude, a, b)
                       for a, b in pairwise(coordinates))
        if distance > radius_km:
            continue
        properties = feature["properties"]
        identifier = properties["corridor_id"]
        record = grouped.setdefault(identifier, {
            **{key: properties[key] for key in ("corridor_id", "label", "source", "route_ref",
                                                "name", "road_class")},
            "source_way_ids": [], "approx_distance_km": distance, "districts": [],
        })
        record["approx_distance_km"] = min(record["approx_distance_km"], distance)
        record["source_way_ids"].append(properties["source_way_id"])
    ordered = sorted(grouped.values(), key=lambda value: (value["approx_distance_km"], value["corridor_id"]))
    for record in ordered:
        record["distance_km"] = record["approx_distance_km"]
        record["approx_distance_km"] = round(record["approx_distance_km"], 2)
        record["source_way_ids"] = sorted(set(record["source_way_ids"]))
    return ordered


def legacy_highway(reference):
    """Recognize old California example selections without requiring Caltrans discovery."""
    for pattern, kind in ((r"(?:CA|SR)[ -]?([0-9]{1,3})", "State"),
                          (r"US[ -]?([0-9]{1,3})", "US"),
                          (r"I[ -]?([0-9]{1,3})", "Interstate")):
        match = re.fullmatch(pattern, reference or "", flags=re.IGNORECASE)
        if match:
            return f"{kind} {int(match.group(1))}"
    return None
