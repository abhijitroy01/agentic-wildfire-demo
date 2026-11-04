import asyncio
import logging

import httpx
from traffic_simulator.feeds import FEED_HOSTS, epoch, fetch_sources, fresh, lcs_active, true
from traffic_simulator.models import Area
from traffic_simulator.store import Store
from traffic_simulator.traffic import Readings

NWS_POINT = "https://api.weather.gov/points/39.7596,-121.6219"


def feed_client(monkeypatch, respond):
    """Route fetch_sources' HTTP client to `respond`; return the client's keyword arguments and the requests sent."""
    built, sent = {}, []

    def transport(request):
        """Record a request that reached the network and answer it."""
        sent.append(request.url)
        return respond(request)

    real_client = httpx.AsyncClient

    def client(**kwargs):
        """Build the real client on the mock transport, keeping its keyword arguments."""
        built.update(kwargs)
        return real_client(transport=httpx.MockTransport(transport), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.delenv("TOMTOM_API_KEY", raising=False)
    monkeypatch.delenv("FIRMS_MAP_KEY", raising=False)
    return built, sent


def by_name(result):
    """Index a fetch_sources result's source records by name."""
    return {source["name"]: source for source in result["sources"]}


def test_feed_redirects_stay_on_the_provider_hosts(monkeypatch, caplog):
    """A same-host NWS redirect is followed; a redirect to another host or to plain HTTP is refused, unsent."""
    hourly = "https://api.weather.gov/gridpoints/STO/53,77/forecast/hourly"

    def respond(request):
        """NWS redirects to canonical coordinates, then its hourly link redirects off-host; WFIGS downgrades."""
        host, path = request.url.host, request.url.path
        if (host, path) == ("api.weather.gov", httpx.URL(NWS_POINT).path):
            return httpx.Response(301, headers={"Location": "https://api.weather.gov/points/39.76,-121.62"})
        if (host, path) == ("api.weather.gov", "/points/39.76,-121.62"):
            return httpx.Response(200, json={"properties": {"forecastHourly": hourly}})
        if (host, path) == ("api.weather.gov", httpx.URL(hourly).path):
            return httpx.Response(302, headers={"Location": "https://169.254.169.254/latest/meta-data/"})
        if host == "services3.arcgis.com":
            return httpx.Response(302, headers={"Location": "http://services3.arcgis.com/insecure"})
        return httpx.Response(404)

    built, sent = feed_client(monkeypatch, respond)
    with caplog.at_level(logging.WARNING, logger="traffic_simulator.feeds"):
        sources = by_name(asyncio.run(fetch_sources(Area(), [])))
    assert built["trust_env"] is False and built["event_hooks"]["request"]
    weather = sources["NWS hourly weather"]
    assert (weather["status"], weather["error"], weather["url"]) == ("unavailable", "FeedHostRefused", hourly)
    perimeters = sources["WFIGS fire perimeters"]
    assert (perimeters["status"], perimeters["error"]) == ("unavailable", "FeedHostRefused")
    # The refused hops never reach the network; only the provider hosts were contacted.
    assert {url.host for url in sent} <= FEED_HOSTS and all(url.scheme == "https" for url in sent)
    assert "Refused a feed request to host 169.254.169.254" in caplog.text


def test_provider_data_cannot_replace_provenance_fields(monkeypatch):
    """A provider body with name, status, url or retrieved_at keys keeps the server's own provenance values."""
    spoofed = {"name": "spoofed", "status": "spoofed", "url": "https://attacker.example/", "retrieved_at": 1,
               "properties": {"forecastHourly": "https://api.weather.gov/gridpoints/STO/53,77/forecast/hourly"}}

    def respond(request):
        """Answer every NWS request with the spoofed body; other feeds fail."""
        return httpx.Response(200, json=spoofed) if request.url.host == "api.weather.gov" else httpx.Response(503)

    feed_client(monkeypatch, respond)
    weather = by_name(asyncio.run(fetch_sources(Area(), [])))["NWS hourly weather"]
    assert weather["status"] == "available" and weather["properties"] == spoofed["properties"]
    assert weather["url"] == spoofed["properties"]["forecastHourly"] and weather["retrieved_at"] > 1
    # Provenance keeps its usual first position, so stored source records keep their layout.
    assert list(weather)[:4] == ["name", "status", "retrieved_at", "url"]


def test_tomtom_point_sample_spends_the_daily_budget_and_is_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("TOMTOM_API_KEY", "synthetic-test-key-not-a-credential")
    monkeypatch.delenv("FIRMS_MAP_KEY", raising=False)
    monkeypatch.setenv("TOMTOM_CALLS_PER_DAY", "1")
    tomtom = []

    def respond(request):
        if request.url.host == "api.tomtom.com":
            tomtom.append(request)
            return httpx.Response(200, json={"flowSegmentData": {"currentSpeed": 40}})
        return httpx.Response(200, json={})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    store = Store(tmp_path)

    def sample(readings):
        result = asyncio.run(fetch_sources(Area(), [], readings))
        return next(s for s in result["sources"] if s["name"] == "TomTom traffic flow")
    readings = Readings(store)
    first = sample(readings)
    assert first["status"] == "available" and len(tomtom) == 1
    # The same process reuses its recent sample for the same point: no call and no budget.
    assert sample(readings) == {**first, "reused": True} and len(tomtom) == 1
    # Another process has no saved sample, and today's budget (1 call) is used up.
    spent = sample(Readings(store))
    assert (spent["status"], spent["error"], len(tomtom)) == ("unavailable", "DailyBudgetReached", 1)


def test_string_booleans_and_timestamp_quality():
    assert not true("false")
    assert true("true")
    assert not fresh(None, now=1000)
    assert not fresh("not reported", now=1000)
    assert not fresh(2000, now=1000)
    assert fresh(950, now=1000)
    assert epoch(1700000000000) == 1700000000


def test_planned_or_cancelled_closures_do_not_become_current_road_closures():
    record = {
        "index": "a-string-not-a-dictionary", "recordTimestamp": {"recordEpoch": "1000"},
        "closure": {
            "closureTimestamp": {"closureStartEpoch": "900", "closureEndEpoch": "1100",
                                 "isClosureEndIndefinite": "false"},
            "code1097": {"isCode1097": "false"}, "code1098": {"isCode1098": "false"},
            "code1022": {"isCode1022": "false"},
        },
    }
    assert not lcs_active(record, now=1000)
    record["closure"]["code1097"]["isCode1097"] = "true"
    assert lcs_active(record, now=1000)
    record["closure"]["code1022"]["isCode1022"] = "true"
    assert not lcs_active(record, now=1000)
    record["closure"]["code1022"]["isCode1022"] = "false"
    assert not lcs_active(record, now=1200)
    record["closure"]["closureTimestamp"]["closureStartEpoch"] = "1200"
    assert not lcs_active(record, now=1000)


def test_audited_2019_travel_times_rejected_as_current():
    # Every record of the audited Caltrans D3 travel-time feed carried this one 2019 observation epoch.
    assert not fresh("1576521162", now=1789578481)


def test_seattle_has_explicit_caltrans_unavailability_without_a_caltrans_request(monkeypatch):
    _, sent = feed_client(monkeypatch, lambda _: httpx.Response(200, json={"features": []}))
    sources = by_name(asyncio.run(fetch_sources(Area(lat=47.6062, lon=-122.3321), [])))
    assert sources["Caltrans highway context"]["status"] == "out_of_coverage"
    assert sources["Caltrans highway context"]["records"] == []
    assert all(url.host != "cwwp2.dot.ca.gov" for url in sent)


def test_california_without_selected_district_does_not_invent_road_status(monkeypatch):
    _, sent = feed_client(monkeypatch, lambda _: httpx.Response(200, json={"features": []}))
    sources = by_name(asyncio.run(fetch_sources(Area(), [])))
    assert sources["Caltrans highway context"]["status"] == "not_selected"
    assert "closure or open-road status" in sources["Caltrans highway context"]["use"]
    assert all(url.host != "cwwp2.dot.ca.gov" for url in sent)


def test_aleutian_perimeter_queries_cover_both_halves_and_partial_coverage_remains_unavailable(monkeypatch):
    envelopes = []

    def respond(request):
        if request.url.host == "services3.arcgis.com":
            envelope = [float(value) for value in request.url.params["geometry"].split(",")]
            envelopes.append(envelope)
            if envelope[0] < 0:
                return httpx.Response(503)
            return httpx.Response(200, json={"features": [{"properties": {}}]})
        return httpx.Response(404)

    feed_client(monkeypatch, respond)
    sources = by_name(asyncio.run(fetch_sources(Area(lat=52.0, lon=179.995, radius_km=2), [])))
    perimeters = sources["WFIGS fire perimeters"]
    assert len(envelopes) == 2 and all(-180 <= west < east <= 180 for west, _, east, _ in envelopes)
    assert perimeters["status"] == "unavailable" and perimeters["error"] == "IncompleteStudyAreaCoverage"
    assert perimeters["geojson"] is None
