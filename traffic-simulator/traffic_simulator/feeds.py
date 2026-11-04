"""Public observations with per-record freshness; a fetch is not an observation."""
import asyncio
import csv
import io
import logging
import math
import os
import time
from datetime import UTC, datetime

import httpx

from traffic_simulator.osm_corridors import study_bounds
from traffic_simulator.traffic import BUDGET_REACHED

WFIGS = ("https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
         "WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query")
HEADERS = {"User-Agent": "WildfireCorridorLab/0.1 (research simulation; contact configured by operator)"}
# The hosts of the fixed feed URLs below (WFIGS, Caltrans CWWP2, NWS, TomTom, NASA FIRMS). Every feed request,
# including each redirect hop, must be HTTPS on the default port to one of them: NWS answers /points with a
# same-host redirect to its canonical coordinates, but a redirect to any other host is refused.
FEED_HOSTS = frozenset({httpx.URL(WFIGS).host, "cwwp2.dot.ca.gov", "api.weather.gov", "api.tomtom.com",
                        "firms.modaps.eosdis.nasa.gov"})
logger = logging.getLogger(__name__)


class FeedHostRefused(ValueError):
    """A feed request or redirect left the fixed provider hosts; that source is recorded as unavailable."""


async def require_feed_host(request: httpx.Request) -> None:
    """Refuse a feed request, including each redirect hop, that is not HTTPS to one of the FEED_HOSTS."""
    url = request.url
    if url.scheme != "https" or url.host not in FEED_HOSTS or url.port is not None or url.userinfo:
        # Only the host is logged: feed URLs can carry provider keys in their path or query.
        logger.warning("Refused a feed request to host %s; only the fixed provider hosts are allowed", url.host)
        raise FeedHostRefused("Feed request outside the provider hosts")


def epoch(value):
    """Parse a seconds or milliseconds timestamp to positive finite epoch seconds, else None."""
    try:
        number = float(value)
        if number > 1e11:
            number /= 1000
        return number if math.isfinite(number) and number > 0 else None
    except (ValueError, TypeError):
        return None


def fresh(value, now=None, max_age=900):
    """Report whether a timestamp is within max_age seconds (allowing 2 minutes of clock skew)."""
    stamp = epoch(value)
    age = (now or time.time()) - stamp if stamp else None
    return age is not None and -120 <= age <= max_age


def true(value):
    return value is True or str(value).lower() == "true"


def distance(lat, lon, point):
    """Approximate meters from a lat/lon to a [lon, lat] point, or None for bad coordinates."""
    try:
        return math.hypot(((float(point[0]) - lon + 180) % 360 - 180) * 111320 * math.cos(math.radians(lat)),
                          (float(point[1]) - lat) * 111320)
    except (TypeError, ValueError):
        return float("inf")


def lcs_active(record, now=None):
    """Report whether a Caltrans lane-closure (LCS) record is in effect now."""
    now = now or time.time()
    closure = record.get("closure", {})
    stamp = closure.get("closureTimestamp", {})
    start = epoch(stamp.get("closureStartEpoch"))
    end = epoch(stamp.get("closureEndEpoch"))
    return (fresh(record.get("recordTimestamp", {}).get("recordEpoch"), now)
            and true(closure.get("code1097", {}).get("isCode1097"))
            and not true(closure.get("code1098", {}).get("isCode1098"))
            and not true(closure.get("code1022", {}).get("isCode1022"))
            and start is not None and start <= now
            and ((end is not None and now < end) or true(stamp.get("isClosureEndIndefinite"))))


async def fetch_sources(area, districts, readings=None):
    """Collect public fire, road, weather, traffic and satellite observations around an area, per record fresh.

    readings (traffic.Readings) spends the shared daily TomTom budget for the TomTom point sample and reuses a
    sample this process took for the same point inside the reuse window; without it nothing is counted.
    Redirects are followed only within FEED_HOSTS (require_feed_host), and proxy or credential settings from the
    environment are ignored.
    """
    async with httpx.AsyncClient(timeout=25, headers=HEADERS, follow_redirects=True, trust_env=False,
                                 event_hooks={"request": [require_feed_host]}) as client:
        async def source(name, url, params=None, parser=None):
            started = time.time()
            try:
                response = await client.get(url, params=params)
                response.raise_for_status()
                raw = response.json()
                if isinstance(raw, dict) and raw.get("error"):
                    raise ValueError("Provider returned an error")
                data = parser(raw) if parser else raw
                provenance = {"name": name, "status": "available", "retrieved_at": started, "url": url}
                # Provenance keeps its place first and is written again last, so provider data never replaces it.
                return {**provenance, **data, **provenance}
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                # Do not stringify request exceptions: their URLs can contain API keys.
                return {"name": name, "status": "unavailable", "retrieved_at": started,
                        "url": url, "error": type(exc).__name__, "records": []}

        def parse_caltrans(kind):
            def parse(raw):
                records = []
                for wrapper in raw.get("data", []):
                    record = wrapper.get(kind, {})
                    location = record.get("location", {})
                    begin = location.get("begin", location)
                    point = [begin.get("beginLongitude", begin.get("longitude")),
                             begin.get("beginLatitude", begin.get("latitude"))]
                    if distance(area.lat, area.lon, point) > area.radius_km * 1000:
                        continue
                    stamp = epoch(record.get("recordTimestamp", {}).get("recordEpoch"))
                    item = {"point": point, "location": location, "record_epoch": stamp}
                    if kind == "tt":
                        tt = record.get("traveltime", {})
                        observed = epoch(tt.get("traveltimeTimestamp", {}).get("traveltimeEpoch"))
                        item.update(observed_at=observed, fresh=fresh(observed), values=tt)
                    elif kind == "lcs":
                        item.update(active=lcs_active(record), closure=record.get("closure", {}),
                                    fresh=fresh(stamp))
                    elif kind == "cctv":
                        item.update(in_service=true(record.get("inService")),
                                    image_freshness="unknown; inventory timestamp is not image capture time")
                    else:
                        item.update(in_service=true(record.get("inService")),
                                    interpretation="Weather sensor values require station-specific quality checks")
                    records.append(item)
                return {"records": records, "total_records": len(raw.get("data", [])),
                        "use": "context only; no automatic lane closure or demand inference"}
            return parse

        def perimeters(raw):
            for feature in raw.get("features", []):
                props = feature.get("properties", {})
                props["capture_fresh_6h"] = fresh(props.get("poly_PolygonDateTime"), max_age=21600)
            return {"geojson": raw, "records": len(raw.get("features", [])),
                    "use": "observed perimeter context; not a fire spread forecast",
                    "incomplete": bool(raw.get("exceededTransferLimit"))}

        boxes = study_bounds(area.lat, area.lon, area.radius_km)

        async def fire_perimeters():
            pieces = await asyncio.gather(*(source("WFIGS fire perimeters", WFIGS, {
                "f": "geojson", "where": "1=1", "geometry": ",".join(map(str, box)),
                "geometryType": "esriGeometryEnvelope", "inSR": "4326", "outSR": "4326",
                "spatialRel": "esriSpatialRelIntersects", "outFields": "*",
                "returnGeometry": "true", "resultRecordCount": "2000",
            }, perimeters) for box in boxes))
            if len(pieces) == 1:
                return pieces[0]
            combined = {**pieces[0], "study_bounds": boxes}
            if any(piece["status"] != "available" for piece in pieces):
                return {**combined, "status": "unavailable", "records": [], "geojson": None,
                        "error": "IncompleteStudyAreaCoverage"}
            features = [feature for piece in pieces for feature in piece["geojson"].get("features", [])]
            return {**combined, "geojson": {"type": "FeatureCollection", "features": features},
                    "records": len(features), "incomplete": any(piece["incomplete"] for piece in pieces)}

        tasks = [fire_perimeters()]
        for district in sorted({int(d) for d in districts if 1 <= int(d) <= 12}):
            for kind in ("lcs", "tt", "cctv", "rwis"):
                if kind == "tt" and district not in (3, 8, 11, 12):
                    continue
                if kind == "rwis" and district not in (2, 3, 6, 8, 9, 10):
                    continue
                url = f"https://cwwp2.dot.ca.gov/data/d{district}/{kind}/{kind}StatusD{district:02}.json"
                tasks.append(source(f"Caltrans D{district} {kind.upper()}", url, parser=parse_caltrans(kind)))

        async def weather():
            point = await source("NWS point", f"https://api.weather.gov/points/{area.lat:.4f},{area.lon:.4f}")
            hourly = point.get("properties", {}).get("forecastHourly")
            if hourly and hourly.startswith("https://api.weather.gov/"):
                result = await source("NWS hourly weather", hourly)
                result["use"] = "weather context only; not coupled to hazard growth"
                return result
            point["name"] = "NWS weather"
            return point
        tasks.append(weather())
        tomtom = os.getenv("TOMTOM_API_KEY", "")

        async def tomtom_sample():
            key = ("sample", area.lat, area.lon)
            if readings is not None:
                reused = readings.recent(key)
                if reused is not None:
                    return {**reused, "reused": True}
                if not await readings.spend():
                    return {"name": "TomTom traffic flow", "status": "unavailable", "retrieved_at": time.time(),
                            "error": BUDGET_REACHED, "records": [],
                            "use": "Today's shared TomTom request budget is used up; no point sample was requested."}
            result = await source("TomTom traffic flow",
                                  "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json",
                                  {"point": f"{area.lat},{area.lon}", "key": tomtom, "unit": "KMPH"},
                                  lambda raw: {"values": raw.get("flowSegmentData", {}),
                                               "use": "point sample context; not calibrated network demand"})
            if readings is not None and result["status"] == "available":
                readings.keep(key, result)
            return result
        if tomtom:
            tasks.append(tomtom_sample())
        results = await asyncio.gather(*tasks)
        if not districts:
            california_extent = 32 <= area.lat <= 42.1 and -124.6 <= area.lon <= -114.1
            results.append({
                "name": "Caltrans highway context",
                "status": "not_selected" if california_extent else "out_of_coverage",
                "records": [],
                "use": "No Caltrans district selected; no closure or open-road status is inferred.",
            })
        if not tomtom:
            results.append({"name": "TomTom traffic flow", "status": "not_configured",
                            "use": "Optional API key; no live speed calibration is implied"})
        key = os.getenv("FIRMS_MAP_KEY", "")
        if key and len(boxes) > 1:
            results.append({"name": "NASA FIRMS", "status": "unavailable",
                            "error": "StudyAreaCrossesDateLine",
                            "use": "This optional connector cannot query both halves of the study area."})
        elif key:
            try:
                envelope = ",".join(map(str, boxes[0]))
                # Key is never returned in provenance or exception text.
                response = await client.get(
                    f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/VIIRS_SNPP_NRT/{envelope}/1")
                response.raise_for_status()
                records = list(csv.DictReader(io.StringIO(response.text)))
                if records and "latitude" not in records[0]:
                    raise ValueError("Invalid FIRMS response")
                results.append({"name": "NASA FIRMS", "status": "available", "records": records,
                                "retrieved_at": time.time(),
                                "use": "Satellite detections, not exact fire boundaries or forecasts"})
            except (httpx.HTTPError, ValueError) as exc:
                results.append({"name": "NASA FIRMS", "status": "unavailable", "error": type(exc).__name__})
        else:
            results.append({"name": "NASA FIRMS", "status": "not_configured",
                            "use": "Optional free NASA MAP_KEY required"})
        results.append({"name": "Caltrans PeMS", "status": "not_configured",
                        "use": "Account-based access; no authenticated PeMS connector is implemented"})
        return {"retrieved_at": datetime.now(UTC).isoformat(), "sources": results,
                "point": {"lat": area.lat, "lon": area.lon}, "radius_km": area.radius_km}
