"""Synthetic provider fixtures test validation, not live traffic coverage."""
import asyncio
import json
import time
from datetime import UTC, datetime
from email.utils import formatdate
from types import SimpleNamespace

import httpx
import pytest
from traffic_simulator import traffic
from traffic_simulator.models import Scenario
from traffic_simulator.store import Store
from traffic_simulator.traffic import (
    BUDGET_REACHED,
    Readings,
    capture_speeds,
    match_segments,
    parse_segment,
    sample_points,
)


def road(name="forward", coordinates=None):
    return {"type": "Feature", "properties": {
        "id": name, "highway": True, "speed_m_s": 20,
    }, "geometry": {"type": "LineString", "coordinates": coordinates or [
        [-118.7798, 34.0259], [-118.7778, 34.0259],
    ]}}


def response():
    return {"flowSegmentData": {
        "currentSpeed": 18, "freeFlowSpeed": 72, "currentTravelTime": 40,
        "freeFlowTravelTime": 10, "confidence": 0.99, "roadClosure": False,
        "coordinates": {"coordinate": [
            {"longitude": -118.7798, "latitude": 34.0259},
            {"longitude": -118.7778, "latitude": 34.0259},
        ]},
    }}


def area():
    return Scenario(lat=34.0259, lon=-118.7798, highway="State 1")


def network(*features):
    return {"type": "FeatureCollection", "features": list(features or [road()])}


def test_directional_match_excludes_opposite_and_ambiguous_parallel_roads():
    now = time.time()
    record = parse_segment(response(), now, formatdate(now, usegmt=True))
    backward = road("backward", list(reversed(road()["geometry"]["coordinates"])))
    matches, _ = match_segments(area(), network(road(), backward), [record], now)
    assert [m["edge_id"] for m in matches] == ["forward"]
    assert matches[0]["speed_limit_m_s"] == 5
    parallel = road("parallel", [[-118.7798, 34.02592], [-118.7778, 34.02592]])
    matches, excluded = match_segments(area(), network(road(), parallel), [record], now)
    assert matches == []
    assert excluded[0]["reason"] == "no_unambiguous_directional_match"


@pytest.mark.parametrize("change,reason", [
    ({"confidence": 0.5}, "low_confidence"),
    ({"road_closure": True}, "closure_or_zero_speed_requires_separate_access_validation"),
    ({"speed_kmh": 0}, "closure_or_zero_speed_requires_separate_access_validation"),
    ({"provider_response_at": 1}, "stale_response"),
])
def test_unusable_readings_never_become_speed_limits(change, reason):
    now = time.time()
    record = parse_segment(response(), now, formatdate(now, usegmt=True))
    record.update(change)
    matches, excluded = match_segments(area(), network(), [record], now)
    assert matches == []
    assert excluded[0]["reason"] == reason


def test_response_age_is_distinct_from_unknown_measurement_age():
    now = time.time()
    for date in (None, "invalid", formatdate(now - 600, usegmt=True), formatdate(now + 600, usegmt=True)):
        with pytest.raises(ValueError):
            parse_segment(response(), now, date)
    record = parse_segment(response(), now, formatdate(now, usegmt=True))
    assert record["observed_at"] is None
    raw = response()
    raw["flowSegmentData"]["currentSpeed"] = float("nan")
    with pytest.raises(ValueError):
        parse_segment(raw, now, formatdate(now, usegmt=True))


def test_requests_are_bounded_deduplicated_and_credentials_do_not_enter_artifacts(monkeypatch):
    monkeypatch.setenv("TOMTOM_API_KEY", "synthetic-test-key-not-a-credential")
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(200, json=response(), headers={"Date": formatdate(usegmt=True)})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            return await capture_speeds(area(), network(), client=client)

    result = asyncio.run(run())
    assert len(calls) == len(result["requests"]) == 1
    assert result["matched_edges"] == 1
    assert "synthetic-test-key" not in json.dumps(result)
    assert result["segments"][0]["observed_at"] is None
    assert len(sample_points(area(), network(*[
        road(str(i), [[-118.9 + i * .015, 34.0259], [-118.899 + i * .015, 34.0259]])
        for i in range(20)
    ]))) <= 12


def two_point_capture(readings, calls):
    """Capture over two highway roads 1.4 km apart (two sample points), counting provider calls."""
    def transport(request):
        calls.append(request)
        return httpx.Response(200, json=response(), headers={"Date": formatdate(usegmt=True)})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            second = road("second", [[-118.7648, 34.0259], [-118.7628, 34.0259]])
            return await capture_speeds(area(), network(road(), second), client=client, readings=readings)
    return asyncio.run(run())


def test_repeat_runs_of_an_area_reuse_recent_readings_without_calls(monkeypatch):
    monkeypatch.setenv("TOMTOM_API_KEY", "synthetic-test-key-not-a-credential")
    readings, calls = Readings(), []
    first = two_point_capture(readings, calls)
    assert len(calls) == 2 and first["status"] == "applied" and first["matched_edges"] == 1
    again = two_point_capture(readings, calls)
    assert len(calls) == 2, "Readings inside cache.tomtom_cache_minutes are not fetched again"
    assert [request.get("reused") for request in again["requests"]] == [True, True]
    assert again["segments"] == first["segments"] and again["matches"] == first["matches"]
    # 0 turns reuse off.
    monkeypatch.setenv("TOMTOM_CACHE_MINUTES", "0")
    two_point_capture(readings, calls)
    assert len(calls) == 4


def test_readings_are_reused_only_inside_the_window_and_then_forgotten():
    readings = Readings()  # cache.tomtom_cache_minutes: 10 minutes
    readings.keep(("flow", 1.0, 2.0), {"id": "x", "retrieved_at": 1000.0}, now=1000.0)
    assert readings.recent(("flow", 1.0, 2.0), now=1599.0)["id"] == "x"
    assert readings.recent(("flow", 1.0, 2.0), now=1601.0) is None
    readings.keep(("flow", 3.0, 4.0), {"id": "y", "retrieved_at": 1700.0}, now=1700.0)
    assert list(readings.saved) == [("flow", 3.0, 4.0)]


def test_a_reused_reading_applies_inside_the_reuse_window_only(monkeypatch):
    now = time.time()
    # Retrieved 8 minutes ago; the provider dated it 100 s before retrieval (inside the response age).
    record = parse_segment(response(), now - 480, formatdate(now - 580, usegmt=True))
    assert [m["edge_id"] for m in match_segments(area(), network(), [record], now)[0]] == ["forward"]
    monkeypatch.setenv("TOMTOM_CACHE_MINUTES", "0")
    matches, excluded = match_segments(area(), network(), [record], now)
    assert matches == [] and excluded[0]["reason"] == "stale_response"


def test_daily_budget_skips_calls_and_marks_traffic_unavailable_instead_of_failing(tmp_path, monkeypatch):
    monkeypatch.setenv("TOMTOM_API_KEY", "synthetic-test-key-not-a-credential")
    monkeypatch.setenv("TOMTOM_CALLS_PER_DAY", "1")
    store, calls = Store(tmp_path), []
    partial = two_point_capture(Readings(store), calls)
    # One call was left today: the first point is read and applied, the second is skipped without a call.
    assert len(calls) == 1 and partial["status"] == "applied" and partial["matched_edges"] == 1
    assert partial["requests"][-1]["error"] == BUDGET_REACHED
    spent = two_point_capture(Readings(store), calls)
    assert len(calls) == 1
    assert (spent["status"], spent["reason"], spent["matches"], spent["matched_edges"]) == (
        "unavailable", "daily_budget_reached", [], 0)
    assert spent["requests"] == [{**spent["requests"][0], "error": BUDGET_REACHED}]


def test_sample_points_follow_the_requests_per_run_setting(monkeypatch):
    # 15 highway roads 1.1 km apart across the study area (simulation.tomtom_requests_per_run: 12).
    roads = network(*[road(str(i), [[-118.86 + i * .012, 34.0259], [-118.859 + i * .012, 34.0259]])
                      for i in range(15)])
    assert len(sample_points(area(), roads)) == 12
    monkeypatch.setenv("TOMTOM_REQUESTS_PER_RUN", "3")
    assert len(sample_points(area(), roads)) == 3


@pytest.mark.parametrize("status", [403, 429])
def test_auth_or_quota_failure_stops_without_fallback_or_key_leak(monkeypatch, status):
    key = "synthetic-test-key-not-a-credential"
    monkeypatch.setenv("TOMTOM_API_KEY", key)
    calls = []

    def transport(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "secret must not escape: " + key})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            await capture_speeds(area(), network(), client=client)

    with pytest.raises(RuntimeError) as error:
        asyncio.run(run())
    assert key not in str(error.value)
    assert f"HTTP {status}" in str(error.value)
    assert len(calls) == 1


# simulation.tomtom_request_concurrency: highway roads 1.1 km apart going east from the study area's centre,
# one sample point each, so sample-point order is road order.
SPREAD = [[-118.7798 + offset * .012, -118.7788 + offset * .012] for offset in range(6)]


def spread_network():
    """Return the SPREAD highway roads, 1.1 km apart from the study area's centre, one TomTom sample point on each."""
    return network(*[road(f"road-{index}", [[west, 34.0259], [east, 34.0259]])
                     for index, (west, east) in enumerate(SPREAD)])


def point_index(request):
    """Return which road of spread_network a TomTom request samples, from its point parameter."""
    lon = float(request.url.params["point"].split(",")[1])
    return min(range(len(SPREAD)), key=lambda index: abs(sum(SPREAD[index]) / 2 - lon))


def road_response(index, now):
    """A TomTom answer whose geometry is road `index` of spread_network, dated `now`."""
    west, east = SPREAD[index]
    body = response()
    body["flowSegmentData"]["coordinates"]["coordinate"] = [
        {"longitude": west, "latitude": 34.0259}, {"longitude": east, "latitude": 34.0259}]
    return httpx.Response(200, json=body, headers={"Date": formatdate(now, usegmt=True)})


def frozen_clock(monkeypatch, now=1_790_000_000.0):
    """Fix the clock the capture reads, so two captures of the same answers give equal snapshots."""
    monkeypatch.setattr(traffic, "time", SimpleNamespace(time=lambda: now))
    monkeypatch.setattr(traffic, "datetime", SimpleNamespace(now=lambda tz=None: datetime.fromtimestamp(now, UTC)))
    return now


def concurrent_capture(monkeypatch, concurrency, answer, readings=None, check_cancel=lambda: None):
    """Capture spread_network with `concurrency` requests at once; answer(index) is an async provider stand-in.

    Returns the snapshot, the requested point indexes in the order they were sent and the most requests in flight.
    """
    monkeypatch.setenv("TOMTOM_API_KEY", "synthetic-test-key-not-a-credential")
    monkeypatch.setenv("TOMTOM_REQUEST_CONCURRENCY", str(concurrency))
    sent, flight = [], {"now": 0, "most": 0}

    async def respond(request):
        """Record the requested point and how many requests are in flight, then let `answer` reply."""
        index = point_index(request)
        sent.append(index)
        flight["now"] += 1
        flight["most"] = max(flight["most"], flight["now"])
        try:
            return await answer(index)
        finally:
            flight["now"] -= 1

    async def run():
        """Capture through a mock TomTom transport."""
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await capture_speeds(area(), spread_network(), check_cancel, client=client, readings=readings)

    return asyncio.run(run()), sent, flight["most"]


def test_concurrent_tomtom_requests_give_the_same_snapshot_as_one_at_a_time(monkeypatch):
    """Up to the configured requests run at once, answers arrive out of order, and the snapshot is unchanged."""
    now = frozen_clock(monkeypatch)
    finished = []

    async def answer(index):
        """Answer the nearest points last, so a concurrent capture receives them out of order."""
        await asyncio.sleep(0.01 * (len(SPREAD) - index))
        finished.append(index)
        return road_response(index, now)

    one_at_a_time, sent_alone, most_alone = concurrent_capture(monkeypatch, 1, answer)
    finished.clear()
    together, sent_together, most_together = concurrent_capture(monkeypatch, 4, answer)
    assert (most_alone, most_together) == (1, 4)
    assert finished != sorted(finished), "answers arrived out of sample-point order"
    assert together == one_at_a_time
    assert together["matched_edges"] == len(SPREAD) and sent_alone == sorted(sent_together) == list(range(len(SPREAD)))
    assert [request["point"] for request in together["requests"]] == sample_points(area(), spread_network())


def test_concurrent_capture_spends_the_daily_budget_in_sample_point_order(tmp_path, monkeypatch):
    """With budget for two calls, the first two points are read and the third is marked, at any concurrency."""
    now = frozen_clock(monkeypatch)
    monkeypatch.setenv("TOMTOM_CALLS_PER_DAY", "2")

    async def answer(index):
        """Answer the first point last."""
        await asyncio.sleep(0.02 if index == 0 else 0)
        return road_response(index, now)

    snapshots = []
    for concurrency in (1, 4):
        snapshot, sent, _ = concurrent_capture(monkeypatch, concurrency, answer,
                                               readings=Readings(Store(tmp_path / str(concurrency))))
        assert sorted(sent) == [0, 1]
        snapshots.append(snapshot)
    assert snapshots[0] == snapshots[1]
    assert [request.get("error") for request in snapshots[1]["requests"]] == [None, None, BUDGET_REACHED]
    assert snapshots[1]["status"] == "applied" and snapshots[1]["matched_edges"] == 2


def test_a_refused_key_stops_new_requests_and_raises_the_first_refusal_in_point_order(monkeypatch):
    """A 403 or 429 stops further points; the error is the earliest point's, as with one request at a time."""
    now = frozen_clock(monkeypatch)

    async def answer(index):
        """The first point is refused slowly (429), the second quickly (403); the rest answer normally."""
        if index == 0:
            await asyncio.sleep(0.02)
            return httpx.Response(429)
        if index == 1:
            return httpx.Response(403)
        await asyncio.sleep(0.01)
        return road_response(index, now)

    for concurrency in (1, 4):
        with pytest.raises(RuntimeError, match="TomTom returned HTTP 429") as refused:
            concurrent_capture(monkeypatch, concurrency, answer)
        assert "synthetic-test-key" not in str(refused.value)
    sent = []

    async def counted(index):
        """Record the requested points and answer as above."""
        sent.append(index)
        return await answer(index)

    with pytest.raises(RuntimeError):
        concurrent_capture(monkeypatch, 4, counted)
    assert sorted(sent) == [0, 1, 2, 3], "no point after the refusal was requested"


def test_cancelling_a_capture_stops_the_requests_in_flight(monkeypatch):
    """A cancellation seen at the next point cancels the requests still in flight and reaches the caller."""
    now = frozen_clock(monkeypatch)
    cancelled, checks = [], []

    class Cancelled(Exception):
        pass

    def check_cancel():
        """Ask to stop at the third point."""
        checks.append(True)
        if len(checks) == 3:
            raise Cancelled()

    async def answer(index):
        """Answer the first point at once and hold the second until it is cancelled."""
        if index == 1:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(index)
                raise
        return road_response(index, now)

    with pytest.raises(Cancelled):
        concurrent_capture(monkeypatch, 2, answer, check_cancel=check_cancel)
    assert cancelled == [1] and len(checks) == 3
