"""Camera broker (owner brief 2026-10-14, camera through the broker): the Delphire camera reaches visitors only
through the workspace API.

No network: a stand-in Delphire answers every request through httpx.MockTransport, and key-shaped strings are
built at run time. Fake clocks replace camera.wall_time and camera.monotonic.
"""
import asyncio
import json
import logging
import re
import secrets
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient
from traffic_simulator import settings as simulator_settings
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import budget_day
from traffic_simulator.store import Store

from backend import api_settings as settings
from backend import camera
from backend.tests.builders import HEADERS, ORIGIN

FIXTURE_ORIGIN = "https://camera.fixture.example"
DEVICE = "fixture-device-12"
# A Delphire-shaped key, built at run time (20 characters, so it also fits a detection class and an image ID).
KEY = "-".join(("cam", secrets.token_hex(8)))
WEBP = b"RIFF" + (24).to_bytes(4, "little") + b"WEBPVP8 " + bytes(16)
PNG = b"\x89PNG\r\n\x1a\n" + bytes(24)
DETECTION = {"type": "detection", "detection": {"classes": ["smoke"], "confidences": [0.82],
                                                "boundingBoxes": [[320, 180, 64, 48]], "model": "fixture"}}
CAMERA_VARIABLES = ("CAMERA_API_BASE_URL", "CAMERA_DEVICE_ID", "CAMERA_ALIAS", "DELPHIRE_API_KEY",
                    "PROVIDER_CREDENTIALS")
SECURITY_LOGGER = "backend.security_events"


def latest_body(image_id="image-a0", claims=(DETECTION,)):
    """Delphire's latest records for the fixture device: the camera, its sensors and groups the modal ignores."""
    return [
        {"alias": "camera_a", "records": [
            {"id": image_id, "type": "image", "timeRecorded": "2026-10-14T20:15:03Z", "sensorNo": 1,
             "deviceId": DEVICE, "image": {"filename": "a.webp", "isInfrared": False}, "claims": list(claims)},
            {"id": "event-1", "type": "event", "timeRecorded": "2026-10-14T20:15:00Z"}]},
        {"alias": "navigation", "records": [{"type": "gps", "gps": {"latitude": 34.12, "longitude": -118.56}}]},
        {"alias": "power", "records": [{"type": "power", "power": {"voltage": 12.1}}]},
    ]


class Vendor:
    """A stand-in Delphire: records every request and the client settings, and answers from its fixtures."""

    def __init__(self):
        self.requests, self.clients = [], []
        self.latest = lambda request: httpx.Response(200, json=latest_body())
        self.image = lambda request: httpx.Response(200, content=WEBP, headers={"Content-Type": "image/webp"})

    async def handle(self, request):
        self.requests.append(request)
        if request.url.host == "camera.fixture.example" and request.url.path == f"/devices/{DEVICE}/latest":
            answer = self.latest(request)
        elif request.url.host == "camera.fixture.example" and request.url.path.startswith("/images/"):
            answer = self.image(request)
        else:
            answer = httpx.Response(404, text="not a Delphire route")
        return await answer if asyncio.iscoroutine(answer) else answer

    def paths(self):
        return [request.url.path for request in self.requests]


class Clock:
    """One fake clock for the reuse window and the back-off."""

    def __init__(self):
        self.now = 1_000_000.0

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def fresh_broker(monkeypatch):
    """Forget every process-level camera state and binding between tests."""
    def reset():
        camera.camera_calls.cache_clear()
        for state in (camera._inflight, camera._failures, camera._miss_refresh_at, settings.camera_warnings):
            state.clear()
    reset()
    for name in CAMERA_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    yield
    reset()


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(camera, "wall_time", lambda: fake.now)
    monkeypatch.setattr(camera, "monotonic", lambda: fake.now)
    return fake


@pytest.fixture
def vendor(monkeypatch):
    """Route the broker's HTTP client to the stand-in Delphire; the real client keeps every other setting."""
    stand_in = Vendor()
    real_client = httpx.AsyncClient

    def client(**kwargs):
        stand_in.clients.append(kwargs)
        return real_client(transport=httpx.MockTransport(stand_in.handle), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", client)
    return stand_in


@pytest.fixture
def configured(monkeypatch):
    """Bind the fixture camera the way a local run does (STORAGE_MODE=local reads DELPHIRE_API_KEY)."""
    for name, value in (("CAMERA_API_BASE_URL", FIXTURE_ORIGIN), ("CAMERA_DEVICE_ID", DEVICE),
                        ("CAMERA_ALIAS", "camera_a"), ("DELPHIRE_API_KEY", KEY)):
        monkeypatch.setenv(name, value)
    return settings.CameraBinding(FIXTURE_ORIGIN, DEVICE, "camera_a", KEY)


@contextmanager
def visitor(app):
    """A same-origin browser with its demo session, as the CloudFront workspace route serves it."""
    with TestClient(app, base_url=ORIGIN, headers=HEADERS) as browser:
        assert browser.post("/api/v1/demo/session").status_code == 200
        yield browser


def error(response):
    return response.status_code, response.json()["error"]["code"]


def spent(store):
    """Camera calls counted in today's shared budget."""
    day, _ = budget_day()
    return (store.get(f"budget:camera_calls:{day}") or {"count": 0})["count"]


def refusals(caplog):
    """The denied security events logged so far."""
    events = [json.loads(record.getMessage()) for record in caplog.records if record.name == SECURITY_LOGGER]
    return [event for event in events if event["security_event"] == "request_refused"]


BOUND = {"CAMERA_API_BASE_URL": FIXTURE_ORIGIN, "CAMERA_DEVICE_ID": DEVICE, "CAMERA_ALIAS": "camera_a",
         "DELPHIRE_API_KEY": KEY}


@pytest.mark.parametrize("changes", [
    dict.fromkeys(BOUND),  # nothing configured (None leaves the variable unset)
    {"DELPHIRE_API_KEY": None},  # a device ID without the key
    {"CAMERA_DEVICE_ID": None},  # the key without a device ID
    {"CAMERA_DEVICE_ID": ""},
    {"CAMERA_API_BASE_URL": "http://camera.fixture.example"},
    {"CAMERA_API_BASE_URL": "https://camera.fixture.example/v1"},
    {"CAMERA_API_BASE_URL": "https://camera.fixture.example?x=1"},
    {"CAMERA_API_BASE_URL": "https://user@camera.fixture.example"},
    {"CAMERA_API_BASE_URL": "https://camera.fixture.example:8443"},
    {"CAMERA_API_BASE_URL": "https://203.0.113.10"},
    {"CAMERA_DEVICE_ID": "../other"},
    {"CAMERA_ALIAS": "camera_z"},
    {"DELPHIRE_API_KEY": KEY[:15]},
    {"DELPHIRE_API_KEY": KEY[:10] + " " + KEY[10:]},
])
def test_unconfigured_camera_is_not_configured_and_calls_nothing(demo, vendor, monkeypatch, changes):
    app, store = demo
    for name, value in {**BOUND, **changes}.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    with visitor(app) as browser:
        assert browser.get("/api/v1/workspace-config").json()["camera_configured"] is False
        for path in ("/api/v1/camera/latest", "/api/v1/camera/images/image-a0"):
            response = browser.get(path)
            assert error(response) == (503, "CAMERA_NOT_CONFIGURED")
            assert response.json()["error"] == {"code": "CAMERA_NOT_CONFIGURED", "retryable": False,
                                                "message": "The camera is not configured for this demonstration.",
                                                "request_id": response.json()["error"]["request_id"], "details": {}}
            assert re.fullmatch(r"[0-9a-f-]{36}", response.json()["error"]["request_id"])
    assert vendor.clients == [] and vendor.requests == []
    assert spent(store) == 0


def test_configured_camera_is_reported_by_workspace_config(demo, configured, vendor):
    with visitor(demo[0]) as browser:
        assert browser.get("/api/v1/workspace-config").json()["camera_configured"] is True
    assert vendor.requests == [], "Reporting the binding calls nothing"


RICH = [
    {"alias": "camera_a", "extra": "group field", "records": [
        # A time without a zone drops its record; the event record is not an image.
        {"id": "image-naive", "type": "image", "timeRecorded": "2026-10-14T13:15:03", "claims": []},
        {"id": "event-1", "type": "event", "timeRecorded": "2026-10-14T20:15:00Z"},
        {"id": "image-a0", "type": "image", "timeRecorded": "2026-10-14T13:15:03.250-07:00", "sensorNo": 1,
         "deviceId": DEVICE, "image": {"filename": "a.webp", "isInfrared": False, "orientationRecord": {}},
         "evidences": [{"id": "evidence"}], "extra": "record field",
         "claims": [
             {"type": "detection", "extra": 1, "detection": {
                 "classes": ["smoke", "fire"], "confidences": [0.82, 0.4], "model": "fixture-model",
                 "boundingBoxes": [[320, 180, 64, 48], [10.5, 20, 30, 40]]}},
             {"type": "detection", "detection": {"classes": ["smoke"], "confidences": [0.5, 0.6]}},
             {"type": "detection", "detection": {"classes": ["fire"], "confidences": [0.7],
                                                 "boundingBoxes": [[1, 2, 3]]}},
             {"type": "detection", "detection": {"classes": ["smoke"], "confidences": ["0.9"]}},
             {"type": "detection", "detection": {"classes": ["smoke\n"], "confidences": [0.9]}},
             {"type": "smoke", "smoke": {"smokeProb": 0.3}},
             {"type": "fire", "fire": {"fireProb": 1.5}},
             {"type": "fire", "fire": {"fireProb": True}},
             {"type": "unknown", "unknown": {}},
             "not a claim",
         ]},
    ]},
    {"alias": "camera_b", "records": [{"id": "image-b0", "type": "image", "timeRecorded": "2026-10-14T20:15:03Z"}]},
    {"alias": "navigation", "records": [{"type": "gps", "timeRecorded": "2026-10-14T20:15:00Z", "gps": {
        "latitude": 34.123456, "longitude": -118.567891, "altitude": 12.0, "quality": 2, "HDOP": 0.8}}]},
    {"alias": "wind", "records": [{"type": "anemometer", "anemometer": {"speed": 3.4, "angle": 400}}]},
    {"alias": "gas", "records": [{"type": "gas", "gas": {"gas": 50000, "humidity": 41.2, "pressure": True,
                                                         "altitude": 100}}]},
    {"alias": "air_quality", "records": [{"type": "voc", "voc": {"TVOC": 12, "eCO2": 400, "CO2": 410}}]},
    {"alias": "power", "records": [{"type": "power", "power": {"voltage": 12.1}}]},
    {"alias": "camera_a", "records": [{"id": "image-later", "type": "image", "timeRecorded": "2026-10-14T20:15:03Z"}]},
]
RICH_VIEW = {"device_id": DEVICE, "camera_alias": "camera_a", "groups": [
    {"alias": "camera_a", "records": [{"id": "image-a0", "type": "image", "timeRecorded": "2026-10-14T20:15:03Z",
                                       "claims": [
        {"type": "detection", "detection": {"classes": ["smoke", "fire"], "confidences": [0.82, 0.4],
                                            "boundingBoxes": [[320.0, 180.0, 64.0, 48.0], [10.5, 20.0, 30.0, 40.0]]}},
        {"type": "detection", "detection": {"classes": ["fire"], "confidences": [0.7], "boundingBoxes": []}},
        {"type": "smoke", "smoke": {"smokeProb": 0.3}}]}]},
    {"alias": "navigation", "records": [{"type": "gps", "gps": {"latitude": 34.1235, "longitude": -118.5679}}]},
    {"alias": "wind", "records": [{"type": "anemometer", "anemometer": {"speed": 3.4, "angle": None}}]},
    {"alias": "gas", "records": [{"type": "gas", "gas": {"humidity": 41.2, "pressure": None}}]},
    {"alias": "air_quality", "records": [{"type": "voc", "voc": {"TVOC": 12.0, "CO2": 410.0}}]},
]}


def test_latest_returns_only_the_fields_the_modal_shows(demo, configured, vendor, caplog):
    vendor.latest = lambda request: httpx.Response(200, json=RICH)
    with caplog.at_level(logging.WARNING, logger="backend.camera"), visitor(demo[0]) as browser:
        response = browser.get("/api/v1/camera/latest")
    assert response.status_code == 200
    assert response.json() == RICH_VIEW
    # Dropped claims and records are counted in the log, with numbers only.
    assert "Camera latest projection dropped 7 of 10 claims and 1 records" in caplog.text


def test_projection_examines_bounded_counts_and_keeps_the_first_valid_image(configured):
    image = {"id": "image-a0", "type": "image", "timeRecorded": "2026-10-14T20:15:03Z"}
    smoke = {"type": "smoke", "smoke": {"smokeProb": 0.5}}
    navigation = {"alias": "navigation", "records": [{"type": "gps", "gps": {"latitude": 1, "longitude": 2}}]}
    fillers = [{"alias": f"filler-{n}", "records": []} for n in range(31)]
    view = camera.project([*fillers, navigation, {"alias": "wind", "records": [
        {"type": "anemometer", "anemometer": {"speed": 1}}]}], configured)
    assert [group["alias"] for group in view["groups"]] == ["camera_a", "navigation"], "A 33rd group is ignored"
    events = [{"id": f"event-{n}", "type": "event"} for n in range(20)]
    view = camera.project([{"alias": "camera_a", "records": [*events, image]}], configured)
    assert view["groups"][0]["records"] == [], "A 21st record is ignored"
    invalid = [{"type": "fire", "fire": {"fireProb": 2}}] * 20
    view = camera.project([{"alias": "camera_a", "records": [{**image, "claims": [*invalid, smoke]}]}], configured)
    assert view["groups"][0]["records"][0]["claims"] == [], "A 21st claim is ignored"
    for entries, kept in ((50, True), (51, False)):
        claim = {"type": "detection", "detection": {"classes": ["smoke"] * entries, "confidences": [0.5] * entries}}
        view = camera.project([{"alias": "camera_a", "records": [{**image, "claims": [claim]}]}], configured)
        assert bool(view["groups"][0]["records"][0]["claims"]) is kept, entries
    view = camera.project([{"alias": "camera_a", "records": [{**image, "id": "bad.id"}, {**image, "id": "image-a1"}]}],
                          configured)
    assert [record["id"] for record in view["groups"][0]["records"]] == ["image-a1"]


def test_the_key_reaches_only_the_delphire_authorization_header(demo, configured, vendor, monkeypatch, caplog):
    seen = []
    with caplog.at_level(logging.DEBUG), visitor(demo[0]) as browser:
        seen.append(browser.get("/api/v1/workspace-config"))
        seen.append(browser.get("/api/v1/camera/latest"))
        seen.append(browser.get("/api/v1/camera/images/image-a0"))
        seen.append(browser.get("/api/v1/camera/images/image-x9"))
        vendor.image = lambda request: httpx.Response(500, text="Delphire error text")
        camera.camera_calls(demo[1]).saved.clear()
        seen.append(browser.get("/api/v1/camera/images/image-a0"))
    assert [response.status_code for response in seen] == [200, 200, 200, 404, 503]
    for response in seen:
        assert KEY not in response.text and KEY.encode() not in response.content
        assert all(KEY not in value for value in response.headers.values())
    assert KEY not in caplog.text
    assert vendor.requests and all(request.headers["authorization"] == f"Bearer {KEY}" for request in vendor.requests)
    assert all(KEY not in str(request.url) for request in vendor.requests)
    for kwargs in vendor.clients:
        assert (kwargs["base_url"], kwargs["follow_redirects"], kwargs["trust_env"]) == (FIXTURE_ORIGIN, False, False)
        assert "verify" not in kwargs, "TLS verification stays the httpx default"
    assert "Delphire error text" not in seen[-1].text


def escaped(text):
    """Write the key's first character as a JSON \\u escape, so the raw answer no longer contains the key."""
    return text.replace(KEY, f"\\u{ord(KEY[0]):04x}{KEY[1:]}")


@pytest.mark.parametrize("where", ["raw class", "escaped class", "escaped image id", "image bytes"])
def test_an_echoed_key_is_refused_in_every_encoding(demo, configured, vendor, caplog, where):
    app, store = demo
    image_id = KEY if where == "escaped image id" else "image-a0"
    claim = {"type": "detection", "detection": {"classes": [KEY], "confidences": [0.5]}}
    body = json.dumps(latest_body(image_id, (claim,) if where.endswith("class") else (DETECTION,)))
    if where.startswith("escaped"):
        body = escaped(body)
        assert KEY not in body
    vendor.latest = lambda request: httpx.Response(200, text=body, headers={"Content-Type": "application/json"})
    vendor.image = lambda request: httpx.Response(200, content=PNG + KEY.encode(), headers={"Content-Type": "image/png"})
    with caplog.at_level(logging.DEBUG), visitor(app) as browser:
        latest = browser.get("/api/v1/camera/latest")
        # The visitor asks for the image the answer would have named; its ID is never served or requested.
        image = browser.get("/api/v1/camera/images/image-a0")
    if where == "image bytes":
        assert latest.status_code == 200 and error(image) == (502, "CAMERA_INVALID")
        assert ("image", DEVICE, image_id) not in camera.camera_calls(store).saved
    else:
        assert error(latest) == (502, "CAMERA_INVALID") and error(image) == (502, "CAMERA_INVALID")
        assert not any(path.startswith("/images/") for path in vendor.paths()), "A refused view names no image"
        assert camera.camera_calls(store).saved == {}, "A refused view is never kept"
    for response in (latest, image):
        assert KEY not in response.text and all(KEY not in value for value in response.headers.values())
    assert KEY not in caplog.text and "key echoed" in caplog.text


def test_an_image_outside_the_latest_records_is_refused(demo, configured, vendor, caplog):
    caplog.set_level(logging.INFO, logger=SECURITY_LOGGER)
    with visitor(demo[0]) as browser:
        assert error(browser.get("/api/v1/camera/images/image-x9")) == (404, "CAMERA_IMAGE_NOT_FOUND")
        assert vendor.paths() == [f"/devices/{DEVICE}/latest"], "The view this request fetched is the newest"
        assert [(event["code"], event["reason"]) for event in refusals(caplog)] == [
            ("CAMERA_IMAGE_NOT_FOUND", "not_in_latest")]
        for malformed in ("bad.id", "a" * 65, "with space", "a%2E%2E"):
            assert error(browser.get(f"/api/v1/camera/images/{malformed}")) == (422, "VALIDATION_ERROR"), malformed
        # An encoded slash forms another path, which no camera route matches.
        assert browser.get("/api/v1/camera/images/x%2Fy").status_code == 404
        assert browser.get("/api/v1/camera/images/image-a0?type=original").status_code == 200
    images = [request for request in vendor.requests if request.url.path.startswith("/images/")]
    assert [(request.url.path, dict(request.url.params)) for request in images] == [
        ("/images/image-a0", {"type": "display"})]


def test_an_image_from_another_processs_newer_view_is_served_after_one_refresh(demo, configured, vendor, caplog,
                                                                                 clock):
    app, store = demo
    caplog.set_level(logging.INFO, logger=SECURITY_LOGGER)
    with visitor(app) as browser:
        assert browser.get("/api/v1/camera/latest").json()["groups"][0]["records"][0]["id"] == "image-a0"
        vendor.latest = lambda request: httpx.Response(200, json=latest_body("image-a1"))
        clock.advance(5)
        assert browser.get("/api/v1/camera/images/image-a1").status_code == 200
        assert vendor.paths() == [f"/devices/{DEVICE}/latest", f"/devices/{DEVICE}/latest", "/images/image-a1"]
        assert refusals(caplog) == []
        clock.advance(5)
        assert error(browser.get("/api/v1/camera/images/image-b9")) == (404, "CAMERA_IMAGE_NOT_FOUND")
        assert vendor.paths().count(f"/devices/{DEVICE}/latest") == 2, "One miss refresh per process per window"
        assert [event["reason"] for event in refusals(caplog)] == ["not_in_latest"]

    # Two concurrent requests for a newer image, from a process that holds the older view, share one refresh.
    camera.camera_calls.cache_clear()
    camera._miss_refresh_at.clear()
    vendor.requests.clear()
    vendor.latest = lambda request: httpx.Response(200, json=latest_body("image-a0"))

    async def concurrently():
        await camera.latest_view(store, configured)
        vendor.latest = lambda request: httpx.Response(200, json=latest_body("image-a2"))
        return await asyncio.gather(*(camera.image_bytes(store, configured, "image-a2") for _ in range(2)))

    assert [value["media_type"] for value in asyncio.run(concurrently())] == ["image/webp", "image/webp"]
    assert vendor.paths() == [f"/devices/{DEVICE}/latest", f"/devices/{DEVICE}/latest", "/images/image-a2"]


def big_stream(size):
    async def chunks():
        sent = 0
        while sent < size:
            chunk = min(16384, size - sent)
            yield (PNG + bytes(chunk))[:chunk] if sent == 0 else bytes(chunk)
            sent += chunk
    return chunks()


CAP = 64 * 1024


@pytest.mark.parametrize("answer,expected", [
    (lambda: httpx.Response(200, text="<html></html>", headers={"Content-Type": "text/html"}),
     (502, "CAMERA_IMAGE_UNSUPPORTED")),
    (lambda: httpx.Response(200, text="<svg/>", headers={"Content-Type": "image/svg+xml"}),
     (502, "CAMERA_IMAGE_UNSUPPORTED")),
    (lambda: httpx.Response(200, content=b"\xff\xd8\xff", headers={"Content-Type": "image/jpeg"}),
     (502, "CAMERA_IMAGE_UNSUPPORTED")),
    (lambda: httpx.Response(200, content=WEBP, headers={"Content-Type": "image/png"}),
     (502, "CAMERA_IMAGE_UNSUPPORTED")),
    (lambda: httpx.Response(200, content=b"", headers={"Content-Type": "image/png"}),
     (502, "CAMERA_IMAGE_UNSUPPORTED")),
    (lambda: httpx.Response(200, content=PNG, headers={"Content-Type": "image/png", "Content-Length": "abc"}),
     (502, "CAMERA_IMAGE_UNSUPPORTED")),
    (lambda: httpx.Response(200, content=PNG, headers={"Content-Type": "image/png",
                                                       "Content-Length": str(CAP + 1)}),
     (502, "CAMERA_IMAGE_TOO_LARGE")),
    (lambda: httpx.Response(200, content=big_stream(CAP + 1), headers={"Content-Type": "image/png"}),
     (502, "CAMERA_IMAGE_TOO_LARGE")),
    (lambda: httpx.Response(200, content=PNG + bytes(CAP), headers={"Content-Type": "image/png",
                                                                    "Content-Length": "10"}),
     (502, "CAMERA_IMAGE_TOO_LARGE")),
    (lambda: httpx.Response(200, content=PNG + bytes(CAP - len(PNG)), headers={"Content-Type": "image/png"}),
     (200, "image/png")),
    (lambda: httpx.Response(200, content=WEBP, headers={"Content-Type": "image/webp; charset=binary"}),
     (200, "image/webp")),
    (lambda: httpx.Response(200, content=PNG, headers={"Content-Type": "IMAGE/PNG"}), (200, "image/png")),
])
def test_only_webp_or_png_within_the_cap_is_served(demo, configured, vendor, monkeypatch, answer, expected):
    monkeypatch.setenv("CAMERA_IMAGE_MAX_KIB", "64")
    vendor.image = lambda request: answer()
    with visitor(demo[0]) as browser:
        response = browser.get("/api/v1/camera/images/image-a0")
    if expected[0] == 200:
        assert (response.status_code, response.headers["content-type"]) == expected
        assert response.content and response.content[:4] in (PNG[:4], WEBP[:4])
        # Set on the response itself: the demo app has none of main.py's header middleware.
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
    else:
        assert error(response) == expected


def test_answers_are_reused_for_the_window_and_every_call_spends_the_budget(demo, configured, vendor, monkeypatch,
                                                                             clock):
    app, store = demo
    monkeypatch.setenv("CAMERA_CALLS_PER_DAY", "3")
    with visitor(app) as browser:
        for _ in range(3):
            assert browser.get("/api/v1/camera/latest").status_code == 200
        for _ in range(2):
            assert browser.get("/api/v1/camera/images/image-a0").status_code == 200
        assert (len(vendor.requests), spent(store)) == (2, 2), "One call per key per window, each counted"
        clock.advance(59)
        assert browser.get("/api/v1/camera/latest").status_code == 200
        assert len(vendor.requests) == 2, "Still inside the 60-second window"
        clock.advance(2)
        assert browser.get("/api/v1/camera/latest").status_code == 200
        assert (len(vendor.requests), spent(store)) == (3, 3), "A new window calls again"
        limited = browser.get("/api/v1/camera/images/image-a0")
        assert error(limited) == (429, "DAILY_LIMIT_REACHED")
        assert "Daily demo limit reached for camera requests" in limited.json()["error"]["message"]
        assert 0 < int(limited.headers["retry-after"]) <= 86400
        assert browser.get("/api/v1/camera/latest").status_code == 200, "An answer inside its window is still served"
    assert (len(vendor.requests), spent(store)) == (3, 3)


def test_concurrent_requests_share_one_call(tmp_path, configured, vendor):
    store = Store(tmp_path)

    async def together():
        return await asyncio.gather(*(camera.latest_view(store, configured) for _ in range(5)))

    answers = asyncio.run(together())
    assert len(vendor.requests) == 1 and spent(store) == 1
    assert all(view == answers[0][0] and fetched for view, fetched in answers)
    assert len({id(view) for view, _ in answers}) == 5, "Each caller gets its own copy"


async def slow(request):
    await asyncio.sleep(1)
    return httpx.Response(200, json=[])


def raises(exception):
    def answer(request):
        raise exception(f"Delphire text for {request.url}", request=request)
    return answer


@pytest.mark.parametrize("answer,expected", [
    (raises(httpx.ReadTimeout), (503, "CAMERA_UNAVAILABLE")),
    (raises(httpx.ConnectError), (503, "CAMERA_UNAVAILABLE")),
    (slow, (503, "CAMERA_UNAVAILABLE")),
    (lambda request: httpx.Response(401, text="Delphire: bad key"), (503, "CAMERA_UNAVAILABLE")),
    (lambda request: httpx.Response(404, text="Delphire: no device"), (503, "CAMERA_UNAVAILABLE")),
    (lambda request: httpx.Response(500, text="Delphire: stack trace"), (503, "CAMERA_UNAVAILABLE")),
    (lambda request: httpx.Response(429, text="Delphire: slow down"), (503, "CAMERA_UNAVAILABLE")),
    (lambda request: httpx.Response(302, headers={"Location": "https://elsewhere.example/collect"}),
     (503, "CAMERA_UNAVAILABLE")),
    (lambda request: httpx.Response(200, text="Delphire text", headers={"Content-Type": "application/json"}),
     (502, "CAMERA_INVALID")),
    (lambda request: httpx.Response(200, json={"groups": "Delphire text"}), (502, "CAMERA_INVALID")),
    (lambda request: httpx.Response(200, text="[]", headers={"Content-Type": "text/html"}), (502, "CAMERA_INVALID")),
    (lambda request: httpx.Response(200, content=b"[" + b" " * camera.LATEST_MAX_BYTES + b"]",
                                    headers={"Content-Type": "application/json"}), (502, "CAMERA_INVALID")),
    (lambda request: httpx.Response(200, content=b'["\xff"]', headers={"Content-Type": "application/json"}),
     (502, "CAMERA_INVALID")),
    # Python 3.14's json.loads bounds nesting by the C stack of the thread that parses (the TestClient's portal
    # thread here). Measured with the .venv's 3.14.6: about 14,000 levels on a 2 MiB stack, 58,000 on Linux's usual
    # 8 MiB and 116,000 on macOS's 16 MiB. So 500,000 nested arrays (1,000,000 bytes, under the 1 MiB cap) raise
    # RecursionError on every stack, an unusable answer and not a server error, and 1,000 levels parse on every one.
    (lambda request: httpx.Response(200, content=b"[" * 500_000 + b"]" * 500_000,
                                    headers={"Content-Type": "application/json"}), (502, "CAMERA_INVALID")),
    (lambda request: httpx.Response(200, content=b"[" * 1_000 + b"]" * 1_000,
                                    headers={"Content-Type": "application/json"}), (200, None)),
])
def test_delphire_failures_are_sanitized(demo, configured, vendor, monkeypatch, answer, expected):
    monkeypatch.setattr(settings, "camera_request_timeout_seconds", lambda: 0.2)
    vendor.latest = answer
    with visitor(demo[0]) as browser:
        response = browser.get("/api/v1/camera/latest")
    if expected[0] == 200:
        assert response.status_code == 200 and response.json()["groups"][0]["records"] == []
        return
    messages = {"CAMERA_UNAVAILABLE": "The camera service is unavailable. Try again shortly.",
                "CAMERA_INVALID": "The camera service returned an unusable response."}
    assert error(response) == expected
    assert response.json()["error"]["message"] == messages[expected[1]]
    assert "Delphire" not in response.text and "elsewhere" not in response.text
    assert [request.url.host for request in vendor.requests] == ["camera.fixture.example"], "No redirect is followed"


def test_a_failing_key_backs_off_exponentially_and_recovers(tmp_path, configured, vendor, clock):
    store = Store(tmp_path)
    calls, retries = [], {}

    def failing(request):
        calls.append(clock.now - start)
        return httpx.Response(500)
    vendor.latest = failing
    start = clock.now

    async def one_hour():
        for second in range(0, 3600, 10):
            clock.now = start + second
            try:
                await camera.latest_view(store, configured)
            except IntegrationError as exc:
                assert (exc.status, exc.code) == (503, "CAMERA_UNAVAILABLE")
                retries[second] = exc.retry_after
    asyncio.run(one_hour())
    assert calls == [0, 10, 30, 70, 150, 310, 630, 1230, 1830, 2430, 3030]
    assert spent(store) == 11, "Only calls spend the budget"
    assert (retries[0], retries[20], retries[40], retries[640]) == (10, 10, 30, 590), "Retry-After is the pause left"

    # One success resets the count: the next failure pauses 10 seconds again.
    vendor.latest = lambda request: httpx.Response(200, json=latest_body())
    clock.now = start + 3630
    asyncio.run(camera.latest_view(store, configured))
    vendor.latest = failing
    clock.now = start + 3700
    with pytest.raises(IntegrationError) as first:
        asyncio.run(camera.latest_view(store, configured))
    assert first.value.retry_after == 10


def test_a_budget_refusal_records_no_back_off(tmp_path, configured, vendor, monkeypatch):
    monkeypatch.setenv("CAMERA_CALLS_PER_DAY", "0")
    store = Store(tmp_path)
    for _ in range(2):
        with pytest.raises(IntegrationError) as refused:
            asyncio.run(camera.latest_view(store, configured))
        assert (refused.value.status, refused.value.code, refused.value.reason) == (
            429, "DAILY_LIMIT_REACHED", "camera_budget")
    assert camera._failures == {} and vendor.requests == []


def test_an_unexpected_error_still_backs_off_and_is_sanitized(demo, configured, vendor, monkeypatch, caplog, clock):
    def broken(records, binding):
        raise KeyError("Delphire text in an exception message")
    monkeypatch.setattr(camera, "project", broken)
    with caplog.at_level(logging.WARNING, logger="backend.camera"), visitor(demo[0]) as browser:
        response = browser.get("/api/v1/camera/latest")
        assert error(response) == (503, "CAMERA_UNAVAILABLE")
        assert response.json()["error"]["message"] == "The camera service is unavailable. Try again shortly."
        assert response.headers["retry-after"] == "10"
        clock.advance(9)
        assert error(browser.get("/api/v1/camera/latest")) == (503, "CAMERA_UNAVAILABLE")
    assert len(vendor.requests) == 1, "A repeat inside the pause sends nothing"
    assert "Camera latest call failed (KeyError)" in caplog.text
    assert "Delphire text" not in caplog.text and "Delphire text" not in response.text


def test_camera_routes_require_the_workspace_session(demo, configured, vendor, monkeypatch):
    app, _ = demo
    with TestClient(app, base_url=ORIGIN, headers=HEADERS) as anonymous:
        for path in ("/api/v1/camera/latest", "/api/v1/camera/images/image-a0", "/api/v1/camera/images/bad.id"):
            assert error(anonymous.get(path)) == (401, "DEMO_SESSION_REQUIRED"), path
    monkeypatch.setattr(simulator_settings, "CLOUD", True)
    with TestClient(app, base_url=ORIGIN) as outside:
        assert error(outside.get("/api/v1/camera/latest")) == (403, "WORKSPACE_CLOSED")
    assert vendor.requests == []


def test_only_the_two_delphire_endpoints_are_requested(demo, configured, vendor):
    with visitor(demo[0]) as browser:
        assert browser.get("/api/v1/camera/latest?device_id=other-device&alias=camera_b").status_code == 200
        assert browser.get("/api/v1/camera/images/image-a0?type=original&device=other").status_code == 200
        for path in ("/api/v1/camera/devices/other/latest", "/api/v1/camera/commands/request-frame",
                     "/api/v1/camera/images/image-a0/control"):
            assert browser.get(path).status_code == 404, path
        for method in ("POST", "PUT", "DELETE"):
            assert browser.request(method, "/api/v1/camera/latest").status_code == 405, method
    assert [(request.method, request.url.path, dict(request.url.params)) for request in vendor.requests] == [
        ("GET", f"/devices/{DEVICE}/latest", {}), ("GET", "/images/image-a0", {"type": "display"})]
