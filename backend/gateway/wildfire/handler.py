"""Bounded adapter for the existing public ArcGIS wildfire feed; no provider key."""
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

URL = "https://services9.arcgis.com/RHVPKKiFTONKtxq3/arcgis/rest/services/USA_Wildfires_v1/FeatureServer/0/query"
ALLOWED = {"geometry", "geometryType", "inSR", "distance", "units", "outFields",
           "where", "returnGeometry", "outSR", "f"}
# Result cap of the tool contract: at most this many fires per query. When the feed holds more,
# ArcGIS sets exceededTransferLimit and the result says "truncated": true.
RECORD_LIMIT = 100
# Widest search radius the tool accepts, in the query's units (statute miles unless kilometers are asked for).
MAX_DISTANCE = 500
# Largest feed answer accepted, in bytes; a longer one is refused, never cut short.
BODY_LIMIT = 2_000_000
# Seconds for reading the whole body, checked after each read: the 20 s socket timeout bounds each read,
# not a body trickled slowly. The status line and headers come before the body, outside this deadline.
READ_SECONDS = 30


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: a 3xx answer raises HTTPError, so only the fixed feed host is requested."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# The feed's opener: the default handlers without redirects, so the scheme check covers every request.
OPENER = urllib.request.build_opener(NoRedirects)


def handler(event, context):
    """Query the public ArcGIS wildfire feed around one point and return its features, flagging truncation."""
    try:
        if not isinstance(event, dict) or set(event) - ALLOWED:
            raise ValueError()
        lon, lat = [float(v) for v in event["geometry"].split(",")]
        radius = event["distance"]
        if (not all(math.isfinite(v) for v in (lon, lat)) or not -180 <= lon <= 180
                or not -90 <= lat <= 90 or type(radius) not in (int, float)
                or not math.isfinite(radius) or not 0 < radius <= MAX_DISTANCE):
            raise ValueError()
        fixed = {"geometryType": "esriGeometryPoint", "inSR": 4326, "outSR": 4326,
                 "f": "json", "where": "1=1", "outFields": "*", "returnGeometry": True}
        if any(k in event and event[k] != v for k, v in fixed.items()):
            raise ValueError()
        units = event.get("units", "esriSRUnit_StatuteMile")
        if units not in ("esriSRUnit_StatuteMile", "esriSRUnit_Kilometer"):
            raise ValueError()
        params = {**fixed, "geometry": f"{lon},{lat}", "distance": radius,
                  "units": units, "returnGeometry": "true", "resultRecordCount": RECORD_LIMIT}
    except (ValueError, KeyError, TypeError, AttributeError):
        return {"error": {"code": "INVALID_FIRE_QUERY", "message": "Use bounded point/radius wildfire queries."}}
    try:
        url = URL + "?" + urllib.parse.urlencode(params)
        # Only https:// reaches the opener, so no file: or custom scheme can (Bandit B310).
        if urllib.parse.urlsplit(url).scheme != "https":
            raise RuntimeError()
        request = urllib.request.Request(url, headers={"User-Agent": "FireGUard/1"})
        deadline = time.monotonic() + READ_SECONDS
        # Fixed https:// ArcGIS URL and parameters, scheme-checked above; the model cannot choose a
        # scheme, host or SQL filter.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected
        with OPENER.open(request, timeout=20) as response:  # nosec B310 - https:// checked above
            body = bytearray()
            # read1 makes at most one socket read, so the deadline also ends a body trickled byte by byte.
            while chunk := response.read1(BODY_LIMIT + 1 - len(body)):
                body += chunk
                if time.monotonic() > deadline:
                    raise TimeoutError()
        if len(body) > BODY_LIMIT:
            raise RuntimeError()
        result = json.loads(body)
        if not isinstance(result, dict) or result.get("error") or not isinstance(result.get("features"), list):
            raise RuntimeError()
        if result.get("exceededTransferLimit"):
            # Never let a capped list pass for every fire in the area.
            result["truncated"] = True
        return result
    except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError, OSError):
        return {"error": {"code": "WILDFIRE_UNAVAILABLE", "message": "The wildfire source is unavailable."}}
