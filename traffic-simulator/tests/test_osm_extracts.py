"""Canonical publisher selection, immutable identity and complete-way native extraction."""
import hashlib
import multiprocessing
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from shapely.geometry import Point, Polygon, box, mapping, shape
from shapely.ops import unary_union
from traffic_simulator import network, osm_corridors, osm_extracts, osm_source_cache, osm_transport
from traffic_simulator.errors import IntegrationError
from traffic_simulator.models import Area
from traffic_simulator.timings import Timings
from traffic_simulator.transport_work import run_transport


def region(identifier, geometry, code=None):
    return {"type": "Feature", "geometry": mapping(geometry), "properties": {
        "id": identifier, "name": identifier,
        "iso3166-2": [code] if code else [],
        "urls": {"pbf": f"https://download.geofabrik.de/{identifier}-latest.osm.pbf"},
    }}


def test_cancellation_interrupts_an_entered_idle_body_read_without_waiting_for_server(tmp_path):
    """Bounded rescue detects an idle-read hang; the parent must terminate and reap its actual spawned child."""
    headers, release, rescued = (threading.Event() for _ in range(3))
    marker = tmp_path / "transport.pid"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "1")
            self.end_headers()
            headers.set()
            if not release.wait(5):
                rescued.set()
                release.set()
            try:
                self.wfile.write(b"x")
            except OSError:
                pass

    entered_at = None
    def check():
        nonlocal entered_at
        if marker.exists() and headers.is_set():
            entered_at = entered_at if entered_at is not None else time.monotonic()
            if time.monotonic() - entered_at >= .2:
                raise InterruptedError("fixture cancelled")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    serving = threading.Thread(target=server.serve_forever, daemon=True)

    serving.start()
    try:
        with pytest.raises(InterruptedError, match="fixture cancelled"):
            run_transport("traffic_simulator.testing", "stalled_http_transport",
                          {"url": f"http://127.0.0.1:{server.server_port}/", "marker": str(marker)},
                          4096, check)
        assert not release.is_set() and not rescued.is_set(), "child must finish before server body rescue"
    finally:
        release.set()
        server.shutdown()
        serving.join(5)
        server.server_close()
    assert marker.exists() and not serving.is_alive()
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert not any(child.pid == pid for child in multiprocessing.active_children())


def region_index():
    alaska = Polygon([(170, 50), (180, 50), (180, 72), (170, 72)])
    return {"type": "FeatureCollection", "features": [
        region("us/washington", box(-125, 45, -117, 50), "US-WA"),
        region("us/idaho", box(-117, 42, -111, 50), "US-ID"),
        region("us/hawaii", box(-161, 18, -154, 24), "US-HI"),
        region("us/alaska", box(-180, 50, -130, 72).union(alaska), "US-AK"),
        region("us", box(-180, 18, -60, 72).union(alaska)),
        region("europe/france", box(-5, 40, 10, 52), "FR-NA"),
    ]}


@pytest.mark.parametrize("lat,lon,expected", [
    (47.6062, -122.3321, ["us/washington"]),
    (46.9, -120.4, ["us/washington"]),  # Rural location, without a fixed demo area.
    (47, -117, ["us/idaho", "us/washington"]),
    (61.2181, -149.9003, ["us/alaska"]),
    (21.3069, -157.8583, ["us/hawaii"]),
    (52, 179.995, ["us/alaska"]),
])
def test_actual_bounds_select_complete_us_region_coverage(lat, lon, expected):
    bounds = osm_corridors.study_bounds(lat, lon, 4)
    selected = osm_extracts.select_regions(region_index(), bounds)
    assert [value["id"] for value in selected] == expected


def test_outside_us_and_uncovered_extent_are_typed_not_replaced():
    for collection, bounds in (
        (region_index(), [[2, 47, 3, 48]]),
        ({"features": region_index()["features"][:1]}, [[-118, 41, -116, 44]]),
    ):
        with pytest.raises(IntegrationError) as raised:
            osm_extracts.select_regions(collection, bounds)
        assert raised.value.code == "OSM_COVERAGE_UNAVAILABLE"


def test_border_and_coastal_envelope_require_only_validated_us_coverage():
    us = box(-125, 45, -117, 49)
    catalog = {"features": [region("us/washington", us, "US-WA"), region("us", us)]}
    selected, coverage = osm_extracts.region_coverage(catalog, [[-126, 48, -124, 50]], require_country=True,
                                                      origin=Point(-124.5, 48.5))
    assert [value["id"] for value in selected] == ["us/washington"]
    assert coverage == {"required_us_bounds": [[-125.0, 48.0, -124.0, 49.0]],
                        "includes_unsupported_area": True}


def test_non_us_anchor_is_refused_before_clipping_and_source_downloads(monkeypatch):
    us = box(-125, 45, -117, 49)
    catalog = {"features": [region("us/washington", us, "US-WA"), region("us", us)]}
    monkeypatch.setattr(osm_extracts, "verify_tool", lambda *_: None)
    monkeypatch.setattr(osm_extracts, "index", lambda _: catalog)
    monkeypatch.setattr(osm_extracts, "transport",
                        lambda *_args, **_kwargs: pytest.fail("Unsupported anchor must not resolve/download files"))
    # Its envelope intersects the country, but the exact point is outside.
    with pytest.raises(IntegrationError) as raised:
        osm_extracts.plan(Area(lat=49.01, lon=-122, radius_km=15), network.command,
                          lambda *_: None, lambda: None, Timings())
    assert raised.value.code == "OUT_OF_COVERAGE"


@pytest.mark.parametrize("latitude,longitude,expected", [
    (49.999, -122, ["us/washington"]),
    (47, -124.999, ["us/washington"]),
    (61.2181, -149.9003, ["us/alaska"]),
    (21.3069, -157.8583, ["us/hawaii"]),
    (52, 179.995, ["us/alaska"]),
    (52, -179.995, ["us/alaska"]),
    (52, 180, ["us/alaska"]),
    (52, -180, ["us/alaska"]),
])
def test_us_anchor_country_gate_keeps_coast_border_and_noncontiguous_us(latitude, longitude, expected):
    catalog = region_index()
    # This fixture's country follows its state boundaries, allowing a genuine clipped coast/border envelope.
    country = unary_union([shape(feature["geometry"]) for feature in catalog["features"]
                           if feature["properties"]["iso3166-2"]
                           and feature["properties"]["iso3166-2"][0].startswith("US-")])
    catalog["features"] = [feature for feature in catalog["features"] if feature["properties"]["id"] != "us"]
    catalog["features"].append(region("us", country))
    selected, _ = osm_extracts.region_coverage(
        catalog, osm_corridors.study_bounds(latitude, longitude, 15),
        require_country=True, origin=Point(longitude, latitude))
    assert [value["id"] for value in selected] == expected


@pytest.mark.parametrize("country_geometry", [None, Point(0, 0), Polygon([(0, 0), (1, 1), (1, 0), (0, 1)])])
def test_production_region_selection_refuses_missing_or_malformed_country_geometry(country_geometry):
    catalog = {"features": [region("us/washington", box(-125, 45, -117, 50), "US-WA")]}
    if country_geometry is not None:
        catalog["features"].append(region("us", country_geometry))
    with pytest.raises(IntegrationError) as raised:
        osm_extracts.region_coverage(catalog, [[-123, 47, -122, 48]], require_country=True)
    assert raised.value.code == "OSM_SOURCE_INVALID"


def test_complete_minimal_catalog_covers_full_envelope_without_clipping():
    catalog = {"features": [region("us/washington", box(-125, 45, -117, 50), "US-WA")]}
    selected, coverage = osm_extracts.region_coverage(catalog, [[-123, 47, -122, 48]])
    assert [value["id"] for value in selected] == ["us/washington"]
    assert coverage["required_us_bounds"] == [[-123.0, 47.0, -122.0, 48.0]]
    assert not coverage["includes_unsupported_area"]


def test_maximum_envelope_selects_both_states_before_expansion():
    # Small area remains in Washington; the bounded maximum reaches Idaho.
    assert [value["id"] for value in osm_extracts.select_regions(
        region_index(), osm_corridors.study_bounds(47, -117.10, 4))] == ["us/washington"]
    assert [value["id"] for value in osm_extracts.select_regions(
        region_index(), osm_corridors.study_bounds(47, -117.10, 15))] == ["us/idaho", "us/washington"]


@pytest.mark.parametrize("url", [
    "http://download.geofabrik.de/us.osm.pbf",
    "https://download.geofabrik.de:444/us.osm.pbf",
    "https://name:password@download.geofabrik.de/us.osm.pbf",
    "https://example.invalid/us.osm.pbf",
    "https://download.geofabrik.de/us.osm.pbf?token=private",
    "https://download.geofabrik.de/../us.osm.pbf",
])
def test_publisher_boundary_refuses_unapproved_urls(url):
    with pytest.raises(IntegrationError, match="boundary"):
        osm_extracts.publisher_url(url)


def source():
    content = b"immutable-pbf-fixture"
    return {"region_id": "us/washington", "region_name": "Washington",
            "url": "https://download.geofabrik.de/us/washington-261025.osm.pbf",
            "bytes": len(content), "md5": hashlib.md5(content, usedforsecurity=False).hexdigest(),
            "etag": '"immutable-etag"', "last_modified": "Mon, 26 Oct 2026 05:05:22 GMT"}


def test_latest_resolves_one_same_publisher_dated_file_and_checksum():
    calls = []
    selected = source()

    def respond(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("latest.osm.pbf"):
            return httpx.Response(307, headers={"Location": selected["url"]})
        if request.url.path.endswith(".md5"):
            return httpx.Response(200, text=selected["md5"] + "  washington-261025.osm.pbf\n")
        return httpx.Response(200, headers={"Content-Length": str(selected["bytes"]),
                                          "ETag": selected["etag"]})

    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=False) as http:
        result = osm_extracts.resolve(region_index()["features"][0]["properties"], http, lambda: None)
    assert result["url"] == selected["url"] and result["md5"] == selected["md5"]
    assert calls == [("HEAD", "/us/washington-latest.osm.pbf"), ("HEAD", "/us/washington-261025.osm.pbf"),
                     ("GET", "/us/washington-261025.osm.pbf.md5")]


def test_provider_failure_never_selects_another_provider_or_stale_source(monkeypatch):
    calls = []
    monkeypatch.setattr(osm_extracts, "index", lambda _: region_index())
    monkeypatch.setattr(osm_extracts, "verify_tool", lambda *_: None)
    real_client = httpx.Client

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(503)

    monkeypatch.setattr(osm_extracts.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(osm_extracts, "transport", lambda operation, payload, *_args, **_kwargs:
                        getattr(osm_transport, operation)(payload))
    with pytest.raises(IntegrationError) as raised:
        osm_extracts.plan(Area(lat=47.6, lon=-122.3), network.command, lambda *_: None, lambda: None, Timings())
    assert raised.value.code == "OSM_SOURCE_UNAVAILABLE"
    assert calls == ["https://download.geofabrik.de/us/washington-latest.osm.pbf"]


@pytest.mark.parametrize("body", [b"short", b"immutable-pbf-fixture-extra"])
def test_incomplete_or_oversized_exact_file_is_refused(monkeypatch, tmp_path, body):
    real_client = httpx.Client
    monkeypatch.setattr(osm_extracts.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)), **kwargs))
    with pytest.raises(IntegrationError) as raised:
        osm_extracts.download_direct(source(), tmp_path/"part", lambda: None, lambda *_: None)
    assert raised.value.code == "OSM_SOURCE_INVALID"


def test_zero_configured_wait_preserves_background_work_and_checks_cancellation():
    with osm_extracts.client(lambda: None) as http:
        assert set(http.timeout.as_dict().values()) == {None}

    def stopped():
        raise InterruptedError("lease lost")
    with pytest.raises(InterruptedError, match="lease lost"), osm_extracts.client(stopped):
        pytest.fail("Cancelled job opened its request")


def test_network_identity_includes_exact_point_snapshot_hash_and_recipe():
    from test_network_providers import source_manifest

    area, manifest = Area(lat=47.6, lon=-122.3), source_manifest()
    original = network.network_key(area, manifest)
    assert network.network_key(Area(lat=area.lat+1e-7, lon=area.lon), manifest) != original
    for field, changed in (("snapshot_timestamp", "2026-10-26T21:21:21Z"), ("recipe", "next-recipe")):
        other = {**manifest, field: changed}
        assert network.network_key(area, other) != original
    other = {**manifest, "sources": [{**manifest["sources"][0], "sha256": "c"*64}]}
    assert network.network_key(area, other) != original


def test_derived_network_missing_or_different_source_manifest_is_a_miss():
    from test_network_providers import source_manifest

    selected = source_manifest()
    assert network.matches_source({"source_manifest": selected}, selected)
    assert not network.matches_source({}, selected)
    other = {**selected, "sources": [{**selected["sources"][0], "sha256": "f"*64}]}
    assert not network.matches_source({"source_manifest": other}, selected)


def test_native_size_guard_stops_oversized_output(tmp_path):
    import sys

    target = tmp_path/"too-large.xml"
    with pytest.raises(network.NetworkTooLarge):
        network.command([sys.executable, "-c", "import sys;open(sys.argv[2],'wb').write(b'x'*100)",
                         "--output", str(target)], lambda: None, max_output_bytes=50)


def test_regional_merge_deduplicates_only_identical_objects(tmp_path):
    first, second = tmp_path/"first.osm", tmp_path/"second.osm"
    first.write_bytes(b'<osm><node id="1" lat="47" lon="-122"/></osm>')
    second.write_bytes(b'<osm><node lon="-122" lat="47" id="1"/></osm>')
    assert osm_extracts.merge_xml([first, second], 1000).count(b"<node ") == 1
    second.write_bytes(b'<osm><node id="1" lat="47.1" lon="-122"/></osm>')
    with pytest.raises(IntegrationError, match="conflicting"):
        osm_extracts.merge_xml([first, second], 1000)


def test_region_seams_require_one_actual_pbf_snapshot_timestamp(monkeypatch):
    monkeypatch.setattr(osm_extracts, "verify_tool", lambda *_: None)
    monkeypatch.setattr(osm_extracts, "index", lambda _: region_index())
    monkeypatch.setattr(osm_extracts, "transport",
                        lambda _operation, payload, *_args, **_kwargs: {"region_id": payload["region"]["id"]})
    monkeypatch.setattr(osm_source_cache, "manifest", lambda source: {
        "snapshot_timestamp": "2026-10-25T21:21:21Z" if source["region_id"].endswith("idaho")
                              else "2026-10-26T21:21:21Z"})
    monkeypatch.setattr(osm_source_cache, "obtain", lambda *_: pytest.fail("Do not replace an inconsistent snapshot"))
    with pytest.raises(IntegrationError, match="one publisher snapshot"):
        osm_extracts.plan(Area(lat=47, lon=-117), network.command, lambda *_: None, lambda: None, Timings())


def test_native_recipe_refuses_unreviewed_tool_version():
    with pytest.raises(IntegrationError) as raised:
        osm_extracts.verify_tool(lambda *_: "osmium version 1.15.0\nlibosmium version 2.19.0\n", lambda: None)
    assert raised.value.code == "OSM_TOOL_UNAVAILABLE"


def test_cached_source_still_requires_extraction_scratch_space(monkeypatch, tmp_path):
    from types import SimpleNamespace

    target = tmp_path/"cached-source.pbf"
    target.write_bytes(b"already-verified-fixture")
    source = {"sha256": "a"*64, "region_id": "us/washington"}
    monkeypatch.setattr(osm_source_cache, "obtain", lambda *_: (target, source))
    monkeypatch.setattr(osm_extracts.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises(IntegrationError) as raised:
        osm_extracts.extract(Area(lat=47.6, lon=-122.3), {"sources": [source], "study_bounds": []},
                            tmp_path, lambda *_: pytest.fail("Disk guard must precede native extraction"),
                            lambda *_: None, lambda: None)
    assert raised.value.code == "OSM_SOURCE_CAPACITY"


@pytest.mark.integration
def test_native_extract_keeps_complete_crossing_ways_and_turn_restrictions(monkeypatch, tmp_path):
    """Actual pinned CLI; fixtures test topology, not invented observed streets."""
    xml = tmp_path/"source.osm"
    xml.write_text('''<osm version="0.6">
<node id="1" lat="47.60" lon="-122.35"/>
<node id="2" lat="47.60" lon="-122.33"/>
<node id="3" lat="47.60" lon="-122.31"/>
<node id="4" lat="47.62" lon="-122.33"/>
<way id="10"><nd ref="1"/><nd ref="2"/><tag k="highway" v="primary"/><tag k="name" v="Fixture Road"/></way>
<way id="11"><nd ref="2"/><nd ref="3"/><tag k="highway" v="primary"/><tag k="name" v="Fixture Road"/></way>
<way id="12"><nd ref="2"/><nd ref="4"/><tag k="highway" v="primary"/></way>
<relation id="20"><member type="way" ref="10" role="from"/><member type="way" ref="12" role="to"/>
<member type="node" ref="2" role="via"/><tag k="type" v="restriction"/><tag k="restriction" v="no_right_turn"/>
</relation></osm>''')
    pbf = tmp_path/"source.osm.pbf"
    subprocess.run([osm_extracts.tool(), "cat", str(xml), "--output", str(pbf)], check=True, capture_output=True)
    declared = {"sha256": hashlib.sha256(pbf.read_bytes()).hexdigest(), "region_id": "us/washington"}
    monkeypatch.setattr(osm_source_cache, "obtain", lambda *_: (pbf, declared))
    extent = [-122.331, 47.599, -122.329, 47.601]
    area = Area(lat=47.6, lon=-122.33, radius_km=2)
    raw, checksum = osm_extracts.extract(area, {"sources": [declared], "study_bounds": [extent]},
                                        tmp_path, network.command, lambda *_: None, lambda: None)
    document = osm_corridors.parse_xml(raw)
    assert checksum == hashlib.sha256(raw).hexdigest()
    assert {node.attrib["id"] for node in document.findall("node")} == {"1", "2", "3", "4"}
    assert document.find("relation").attrib["id"] == "20"
    assert {way.attrib["id"] for way in document.findall("way")} == {"10", "11", "12"}
    assert not list(tmp_path.glob("region-*.osm.pbf")), "per-job scratch files cleaned"
