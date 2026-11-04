"""Rank nearby Caltrans highway segments from a WGS84 point.

Caltrans State Highway discovery client: bounded, read-only, IDs-first queries to the official public
ArcGIS service, then proximity ranking by route. This does not determine fire exposure,
road passability, evacuation routes, or emergency-vehicle access.
"""
import json
import logging
import math
from itertools import pairwise
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

SERVICE = ("https://caltrans-gis.dot.ca.gov/arcgis/rest/services/"
           "CHhighway/SHN_Lines/FeatureServer/0/query")
logger = logging.getLogger(__name__)


class CaltransRedirects(HTTPRedirectHandler):
    """Follow only HTTPS redirects to the configured Caltrans highway host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if (target.scheme != "https" or target.hostname != urlsplit(SERVICE).hostname
                or target.port not in (None, 443) or target.username or target.password):
            raise RuntimeError("Caltrans highway redirect left the provider host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urlopen(request, timeout):
    """Open a highway request with redirect restrictions on every hop."""
    return build_opener(CaltransRedirects()).open(request, timeout=timeout)

def request(params):
    """Query the Caltrans highway service over HTTPS with a bounded read; provider error text stays in the server log.

    The read stops after CALTRANS_RESPONSE_MAX_MB (simulation.caltrans_response_max_mb), read at call time.
    """
    from traffic_simulator import settings
    largest = settings.caltrans_response_max_mb() * 1_000_000
    url = SERVICE + "?" + urlencode(params)
    if urlsplit(url).scheme != "https":
        raise RuntimeError("The Caltrans highway service must be an https:// URL")
    req = Request(url, headers={"User-Agent": "WildfireCorridorResearch/0.1"})
    # The fixed https:// service URL with encoded parameters, scheme-checked above: no caller chooses a scheme or host.
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected
    with urlopen(req, timeout=45) as response:  # nosec B310 - https:// checked above
        raw = response.read(largest + 1)
    if len(raw) > largest:
        raise RuntimeError("Caltrans highway response exceeded the size limit. Reduce the area radius.")
    value = json.loads(raw)
    if "error" in value:
        error = value["error"] if isinstance(value["error"], dict) else {}
        logger.warning("Caltrans highway service error %s: %.200s", error.get("code"), error.get("message"))
        raise RuntimeError("Caltrans highway service returned an error. Retry later.")
    return value

def query_geometry(latitude, longitude, radius_km):
    """Build the Caltrans ArcGIS envelope query parameters around a point and radius."""
    # Envelope is only a discovery filter, never a fire hazard boundary.
    dy = radius_km / 110.5
    dx = radius_km / (110.5 * math.cos(math.radians(latitude)))
    common = {
        "where": "1=1",
        "geometry": f"{longitude-dx},{latitude-dy},{longitude+dx},{latitude+dy}",
        "geometryType": "esriGeometryEnvelope", "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
    }
    # IDs-first avoids silently truncating ArcGIS results at maxRecordCount.
    found = request({**common, "f": "json", "returnIdsOnly": "true"})
    ids = found.get("objectIds") or []
    features = []
    for start in range(0, len(ids), 200):
        page = request({
            "f": "geojson", "objectIds": ",".join(map(str, ids[start:start+200])),
            "outFields": "OBJECTID,Route,RouteType,County,District,Direction,PMRouteID,bPM,ePM",
            "outSR": 4326, "returnGeometry": "true",
        })
        if page.get("exceededTransferLimit") or page.get("properties", {}).get("exceededTransferLimit"):
            raise RuntimeError("Incomplete highway geometry response")
        features.extend(page.get("features", []))
    if len(features) != len(ids):
        raise RuntimeError("Service returned incomplete geometry for the requested IDs")
    return {"type": "FeatureCollection", "features": features}

def segment_distance_km(latitude, longitude, a, b):
    """Approximate kilometers from a point to a line segment on a local tangent plane."""
    # Local tangent-plane distance: adequate for approximate discovery within
    # the supported 1–50 km radius. Routing must use a full directed network.
    scale = math.cos(math.radians(latitude))
    ax, ay = ((a[0]-longitude+180) % 360-180)*111.32*scale, (a[1]-latitude)*111.32
    bx, by = ((b[0]-longitude+180) % 360-180)*111.32*scale, (b[1]-latitude)*111.32
    dx, dy = bx-ax, by-ay
    t = max(0, min(1, -(ax*dx+ay*dy)/(dx*dx+dy*dy))) if dx or dy else 0
    return math.hypot(ax+t*dx, ay+t*dy)

def rank_highways(collection, latitude, longitude, radius_km):
    """Group Caltrans highway features by route and sort them by distance from the point."""
    grouped = {}
    for feature in collection.get("features", []):
        geometry = feature.get("geometry") or {}
        if geometry.get("type") == "LineString":
            lines = [geometry["coordinates"]]
        elif geometry.get("type") == "MultiLineString":
            lines = geometry["coordinates"]
        else:
            continue
        distances = [segment_distance_km(latitude, longitude, a, b)
                     for line in lines for a, b in pairwise(line)]
        if not distances or min(distances) > radius_km:
            continue
        p = feature["properties"]
        key = (p.get("RouteType"), p.get("Route"))
        record = grouped.setdefault(key, {
            "route_type": key[0], "route_number": key[1],
            "approx_distance_km": min(distances), "districts": set(),
            "counties": set(), "segment_ids": [],
        })
        record["approx_distance_km"] = min(record["approx_distance_km"], min(distances))
        record["districts"].add(p.get("District"))
        record["counties"].add(p.get("County"))
        record["segment_ids"].append(p.get("OBJECTID"))
    results = sorted(grouped.values(), key=lambda x: x["approx_distance_km"])
    for item in results:
        item["approx_distance_km"] = round(item["approx_distance_km"], 2)
        item["districts"] = sorted(x for x in item["districts"] if x is not None)
        item["counties"] = sorted(x for x in item["counties"] if x is not None)
    return results
