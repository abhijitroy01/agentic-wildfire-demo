"""Bounded traffic snapshots and conservative, directional geometry matching.

Provider speeds become experimental lane speed limits, not measured demand.
HTTP response time is NOT an individual traffic observation timestamp.
Limits come from traffic_simulator.settings at call time: local_server.py and configure_tomtom.py
import this module before STORAGE_MODE is set.
"""
import asyncio
import hashlib
import math
import os
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from itertools import pairwise
from pathlib import Path

import httpx
from shapely.geometry import Point, shape
from shapely.ops import transform

from traffic_simulator.provider_calls import ProviderCalls

URL = "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/19/json"
MIN_CONFIDENCE = 0.8
MATCH_DISTANCE_M = 20
# Recorded on a request skipped because today's shared TomTom budget is used up.
BUDGET_REACHED = "DailyBudgetReached"
# Local development only: configure_tomtom.py writes this git-ignored file and local_server.py loads it.
# Containers receive TOMTOM_API_KEY as a secret instead. Fixed path, independent of DATA_DIR.
LOCAL_KEY_FILE = Path(__file__).resolve().parents[1] / ".runtime" / "tomtom-api-key"


def configured():
    return bool(os.getenv("TOMTOM_API_KEY", "").strip())


def tomtom_calls_per_day():
    """Return budgets.tomtom_calls_per_day (TOMTOM_CALLS_PER_DAY), read when a call is spent."""
    from traffic_simulator import settings
    return settings.tomtom_calls_per_day()


def tomtom_reuse_seconds():
    """Return cache.tomtom_cache_minutes in seconds (TOMTOM_CACHE_MINUTES), read when a reading is reused or kept."""
    from traffic_simulator import settings
    return settings.tomtom_cache_minutes() * 60


class Readings(ProviderCalls):
    """One process's TomTom use: today's shared call budget, and recent readings reused without a call.

    spend() takes one call from budgets.tomtom_calls_per_day, counted for every visitor in the jobs table, so
    the API's source lookups and the workers' runs share it. A reading kept here is reused for the same request
    while it is younger than cache.tomtom_cache_minutes, so repeat runs of an area cost no calls. Without a
    store (tests, local scripts) nothing is counted. A worker's public-feed fetch runs in a background thread,
    so the saved readings are guarded by a lock.
    """

    def __init__(self, store=None):
        super().__init__(store, "tomtom_calls", tomtom_calls_per_day, tomtom_reuse_seconds)


def applied_age_seconds():
    """Return how old a TomTom reading a run still applies: the response age limit, or the reuse window if longer."""
    from traffic_simulator import settings
    return max(settings.tomtom_response_max_age_seconds(), tomtom_reuse_seconds())


def projection(area):
    """Return an equirectangular lon/lat-to-meters projection centered on the area."""
    scale = 111320 * math.cos(math.radians(area.lat))
    return lambda x, y, z=None: (((x - area.lon + 180) % 360 - 180) * scale, (y - area.lat) * 111320)


def sample_points(area, network):
    """Pick up to TOMTOM_REQUESTS_PER_RUN highway midpoints in the area, nearest first and at least 1 km apart."""
    from traffic_simulator import settings
    most = settings.tomtom_requests_per_run()
    project = projection(area)
    candidates = []
    for feature in network["features"]:
        if not feature["properties"].get("highway"):
            continue
        line = shape(feature["geometry"])
        point = line.interpolate(0.5, normalized=True)
        xy = Point(*project(point.x, point.y))
        if xy.distance(Point(0, 0)) <= area.radius_km * 1000:
            candidates.append((xy.distance(Point(0, 0)), feature["properties"]["id"], point, xy))
    selected = []
    for _, _, point, xy in sorted(candidates, key=lambda c: (c[0], c[1])):
        if all(xy.distance(previous[1]) >= 1000 for previous in selected):
            selected.append((point, xy))
        if len(selected) == most:
            break
    return [[p.x, p.y] for p, _ in selected]


def parse_segment(raw, retrieved_at, response_date):
    """Validate one TomTom flow response, dated within TOMTOM_RESPONSE_MAX_AGE_SECONDS of retrieval, as a segment."""
    from traffic_simulator import settings
    try:
        published = parsedate_to_datetime(response_date).timestamp()
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Provider response date missing or invalid") from None
    if not -120 <= retrieved_at - published <= settings.tomtom_response_max_age_seconds():
        raise ValueError("Provider response is stale or future dated")
    flow = raw["flowSegmentData"]
    values = {name: float(flow[name]) for name in (
        "currentSpeed", "freeFlowSpeed", "currentTravelTime", "freeFlowTravelTime", "confidence"
    )}
    if not all(math.isfinite(value) for value in values.values()):
        raise ValueError("Non-finite traffic values")
    if not (0 <= values["currentSpeed"] <= 200 and 0 < values["freeFlowSpeed"] <= 200
            and 0 <= values["confidence"] <= 1
            and values["currentTravelTime"] >= 0 and values["freeFlowTravelTime"] > 0):
        raise ValueError("Traffic values outside supported bounds")
    closure = flow.get("roadClosure")
    if not isinstance(closure, bool):
        raise TypeError("Closure status missing")
    coordinates = [[float(p["longitude"]), float(p["latitude"])]
                   for p in flow["coordinates"]["coordinate"]]
    if not 2 <= len(coordinates) <= 10000 or any(
        not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90)
        for lon, lat in coordinates
    ):
        raise ValueError("Invalid traffic geometry")
    return {
        "id": hashlib.sha256(repr(coordinates).encode()).hexdigest()[:20],
        "retrieved_at": retrieved_at, "provider_response_at": published,
        "observed_at": None, "observation_time_note": "Individual observation timestamp not supplied",
        "speed_kmh": values["currentSpeed"], "free_flow_kmh": values["freeFlowSpeed"],
        "travel_time_s": values["currentTravelTime"], "free_flow_time_s": values["freeFlowTravelTime"],
        "confidence": values["confidence"], "road_closure": closure,
        "geometry": {"type": "LineString", "coordinates": coordinates},
    }


def match_segments(area, network, segments, now=None):
    """Match fresh, confident readings to unambiguous same-direction highway edges; list the excluded ones.

    A reading applies while it is younger than applied_age_seconds() (a reused reading included) and its
    provider date was within the response age limit of its retrieval.
    """
    from traffic_simulator import settings
    now = time.time() if now is None else now
    response_age, reading_age = settings.tomtom_response_max_age_seconds(), applied_age_seconds()
    project = projection(area)
    roads = [(f, transform(project, shape(f["geometry"])))
             for f in network["features"] if f["properties"].get("highway")]
    matches, excluded = {}, []
    for record in segments:
        reason = None
        if not (-120 <= now - record["retrieved_at"] <= reading_age
                and -120 <= record["retrieved_at"] - record["provider_response_at"] <= response_age):
            reason = "stale_response"
        elif record["confidence"] < MIN_CONFIDENCE:
            reason = "low_confidence"
        elif record["road_closure"] or record["speed_kmh"] < 1:
            reason = "closure_or_zero_speed_requires_separate_access_validation"
        if reason:
            excluded.append({"segment_id": record["id"], "reason": reason})
            continue
        source = transform(project, shape(record["geometry"]))
        if source.length < 20 or not source.is_simple:
            excluded.append({"segment_id": record["id"], "reason": "ambiguous_geometry"})
            continue
        candidates = []
        for feature, road in roads:
            if road.length < 20 or source.distance(road) > MATCH_DISTANCE_M:
                continue
            probes = [road.interpolate(f, normalized=True) for f in (0.05, 0.25, 0.5, 0.75, 0.95)]
            distances = [p.distance(source) for p in probes]
            along = [source.project(p) for p in probes]
            # Returned geometry order is a directional matching assumption.
            # Opposite-direction, partial, and very different-length matches fail.
            if (max(distances) > MATCH_DISTANCE_M
                    or any(b <= a for a, b in pairwise(along))
                    or not 0.7 <= (along[-1] - along[0]) / road.length <= 1.1):
                continue
            candidates.append((feature, sum(distances) / len(distances), along[0], along[-1]))
        accepted = 0
        for feature, distance, start, end in candidates:
            ambiguous = any(
                other["properties"]["id"] != feature["properties"]["id"]
                and min(end, stop) - max(start, begin) > 0.7 * min(end - start, stop - begin)
                and abs(distance - other_distance) < 8
                for other, other_distance, begin, stop in candidates
            )
            if ambiguous:
                continue
            edge_id = feature["properties"]["id"]
            speed = min(feature["properties"]["speed_m_s"], record["speed_kmh"] / 3.6)
            candidate = {
                "edge_id": edge_id, "segment_id": record["id"], "speed_limit_m_s": speed,
                "provider_speed_kmh": record["speed_kmh"], "free_flow_kmh": record["free_flow_kmh"],
                "confidence": record["confidence"], "match_distance_m": round(distance, 2),
                "retrieved_at": record["retrieved_at"], "geometry": feature["geometry"],
            }
            previous = matches.get(edge_id)
            if previous is None or candidate["confidence"] > previous["confidence"]:
                matches[edge_id] = candidate
            accepted += 1
        if not accepted:
            excluded.append({"segment_id": record["id"], "reason": "no_unambiguous_directional_match"})
    return sorted(matches.values(), key=lambda m: m["edge_id"]), excluded


async def capture_speeds(area, network, check_cancel=lambda: None, client=None, readings=None):
    """Capture TomTom flow readings at the area's highway sample points and match them to its edges.

    readings (Readings) reuses this process's recent readings and spends the shared daily budget before each
    call. Once the budget is used up the remaining points are skipped; when nothing matched, the snapshot is
    marked unavailable and the run continues with OSM speed limits (decision D20).
    Up to TOMTOM_REQUEST_CONCURRENCY (simulation.tomtom_request_concurrency) requests are in flight at once. Points
    still check for cancellation and take their reused reading or budget unit one at a time in sample-point order,
    and the snapshot lists requests and segments in that order, so it is the same as with one request at a time.
    """
    from traffic_simulator import settings
    key = os.getenv("TOMTOM_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Traffic-speed mode requires a configured TomTom key; no run fallback was used.")
    slots = asyncio.Semaphore(settings.tomtom_request_concurrency())
    readings = readings or Readings()
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=20)
    # A 401, 403 or 429 answer sets refused: no later point is requested (invalid or quota-exhausted key).
    segments, requests, seen, points, refused = [], [], set(), [], asyncio.Event()
    budget_reached = False

    def add(segment):
        if segment["id"] not in seen:
            seen.add(segment["id"])
            segments.append(segment)

    async def read(lon, lat, request):
        """Request one sample point's flow reading, recording the answer in its request; return the segment or None."""
        try:
            response = await client.get(URL, params={
                "key": key, "point": f"{lat:.6f},{lon:.6f}", "unit": "KMPH",
            })
            received = time.time()
            request.update(retrieved_at=received, status=response.status_code)
            if response.status_code in (401, 403, 429):
                # Do not repeat invalid or quota-exhausted requests.
                refused.set()
                raise RuntimeError(
                    f"TomTom returned HTTP {response.status_code}; traffic-speed run stopped without fallback."
                )
            if response.status_code != 200:
                request["error"] = "Provider request failed"
                return None
            segment = parse_segment(response.json(), received, response.headers.get("date"))
            readings.keep(("flow", lon, lat), segment, received)
            return segment
        except (httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError) as exc:
            # Never log request URLs, provider messages or keys.
            request["error"] = type(exc).__name__
            return None
        finally:
            slots.release()

    try:
        # points holds, per admitted sample point in order, its reused reading or the task requesting it.
        try:
            for lon, lat in sample_points(area, network):
                await slots.acquire()
                requested = False
                try:
                    if refused.is_set():
                        break
                    check_cancel()
                    received = time.time()
                    request = {"point": [lon, lat], "retrieved_at": received}
                    requests.append(request)
                    reused = readings.recent(("flow", lon, lat), received)
                    if reused is not None:
                        # Fetched by this process inside the reuse window: no call and no budget.
                        request.update(retrieved_at=reused["retrieved_at"], status=200, reused=True)
                        points.append(reused)
                        continue
                    if not await readings.spend():
                        request["error"] = BUDGET_REACHED
                        budget_reached = True
                        break
                    points.append(asyncio.create_task(read(lon, lat, request)))
                    requested = True
                finally:
                    if not requested:
                        slots.release()
            tasks = [point for point in points if isinstance(point, asyncio.Task)]
            answers = dict(zip(tasks, await asyncio.gather(*tasks, return_exceptions=True), strict=True))
        except BaseException:
            # A cancellation, a worker stop or a failed budget write stops every request still in flight.
            tasks = [point for point in points if isinstance(point, asyncio.Task)]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        # The first failure in sample-point order is the one a request-at-a-time capture would have raised.
        failure = next((answers[t] for t in tasks if isinstance(answers[t], BaseException)), None)
        if failure is not None:
            raise failure
        for point in points:
            segment = answers[point] if isinstance(point, asyncio.Task) else point
            if segment is not None:
                add(segment)
        matches, excluded = match_segments(area, network, segments)
        highway_edges = sum(bool(f["properties"].get("highway")) for f in network["features"])
        if not matches and budget_reached:
            # Missing data stays unavailable: no provider speed is applied or implied.
            return {
                "mode": "provider_speed_limits", "provider": "TomTom", "status": "unavailable",
                "reason": "daily_budget_reached",
                "message": "Today's shared TomTom request budget is used up, so no provider speeds were applied; "
                           "every road keeps its OSM speed limit.",
                "retrieved_at": datetime.now(UTC).isoformat(), "requests": requests,
                "segments": segments, "matches": [], "excluded": excluded,
                "matched_edges": 0, "highway_edges": highway_edges,
                "geojson": {"type": "FeatureCollection", "features": []},
            }
        if not matches:
            raise RuntimeError(
                "No recent, high-confidence traffic segments could be matched to this highway. "
                "Traffic-speed run stopped; choose assumed traffic or another area."
            )
        return {
            "mode": "provider_speed_limits", "provider": "TomTom", "status": "applied",
            "retrieved_at": datetime.now(UTC).isoformat(), "requests": requests,
            "segments": segments, "matches": matches, "excluded": excluded,
            "matched_edges": len(matches),
            "highway_edges": highway_edges,
            "method": "Frozen provider speeds used as lane speed limits, capped at the OSM limit, in every policy",
            "limitations": [
                "No measured vehicle counts or initial queues; demand remains synthetic and the network starts empty.",
                "Individual observation timestamps are unavailable; response age is checked instead.",
                "Geometry order is assumed to follow traffic direction; this is not an authoritative lane crosswalk.",
                "Speed limits are fixed for the run, not a live traffic replay or calibrated congestion forecast.",
                "SUMO driver speed-factor variation and internal junction limits remain in effect.",
                "Unmatched roads retain their OSM limits; provider closure flags do not automatically close roads.",
            ],
            "geojson": {"type": "FeatureCollection", "features": [
                {"type": "Feature", "geometry": m["geometry"],
                 "properties": {k: v for k, v in m.items() if k != "geometry"}} for m in matches
            ]},
        }
    finally:
        if owns_client:
            await client.aclose()
