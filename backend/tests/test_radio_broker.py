"""Radio broker: the fixed upstream, its event relay and redaction, and the shared daily agent budget."""
import json

from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

from backend import radio_broker
from backend.tests.builders import client

# One playback investigation as the radio service sends it on /ws/monitor/start: investigation.run_monitor's
# connected, simulation_audio and complete frames around POCMonitor's log, step and detection events.
RADIO_RUN = [
    {"type": "connected", "data": {"session_id": "mon-fixture", "status": "starting"}},
    {"type": "log", "data": {"level": "INFO", "message": "Starting Broadcastify Monitor (POC Mode)"},
     "timestamp": "2026-10-14T12:00:00+00:00"},
    {"type": "log", "data": {"level": "INFO", "message": "Found 1 files to process"},
     "timestamp": "2026-10-14T12:00:00+00:00"},
    {"type": "simulation_audio", "data": {"url": "https://fixture.invalid/fixture-clip.mp3?signed",
                                          "file": "fixture-clip.mp3"}},
    {"type": "log", "data": {"level": "INFO", "message": "[1/1] Processing: input/fixture-clip.mp3"},
     "timestamp": "2026-10-14T12:00:01+00:00"},
    {"type": "step_start", "data": {"step": 1, "name": "Processing fixture-clip.mp3", "description": ""},
     "timestamp": "2026-10-14T12:00:01+00:00"},
    {"type": "detection", "data": {"location": "Fixture address", "keywords": ["fire"], "confidence": 0.9,
                                   "source_id": "input/fixture-clip.mp3",
                                   "transcript_excerpt": "Exercise fire at Fixture address"},
     "timestamp": "2026-10-14T12:00:09+00:00"},
    {"type": "step_complete", "data": {"step": 1, "name": "Audio processing", "success": True},
     "timestamp": "2026-10-14T12:00:09+00:00"},
    {"type": "complete", "data": {"success": True, "total_detections": 1, "sources_processed": 1,
                                  "first_detection": {"location": "Fixture address", "keywords": ["fire"],
                                                      "confidence": 0.9}}},
]
# The same run failing in processing: investigation.send_error_and_close sends its error, then closes with 1011.
FAILED_RADIO_RUN = [*RADIO_RUN[:6], {"type": "error", "data": {
    "message": "Radio monitoring could not complete. Please retry.", "error_type": "monitoring_error"}}]


class RadioUpstream:
    """Stand-in radio service WebSocket: answers recv() with recorded frames, then closes with the given code."""

    def __init__(self, frames, code):
        self.frames, self.code = [json.dumps(frame) for frame in frames], code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def recv(self):
        if self.frames:
            return self.frames.pop(0)
        closed = ConnectionClosedOK if self.code == 1000 else ConnectionClosedError
        raise closed(Close(self.code, "Monitoring complete" if self.code == 1000 else "Monitoring error"), None)


def relayed(response):
    """Decode the event frames a radio stream response carried, without its heartbeats."""
    return [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines()
            if line.startswith("data: ")]


def test_normal_transport_close_without_terminal_event_is_explicit_failure(demo, monkeypatch):
    monkeypatch.setenv("RADIO_SERVICE_URL", "http://radio.internal")
    monkeypatch.setenv("BROADCASTIFY_PRIVATE_KEY", "private-fixture-key")
    monkeypatch.setattr(radio_broker, "connect", lambda *_args, **_kwargs: RadioUpstream(RADIO_RUN[:7], 1000))
    with client(demo[0]) as browser:
        browser.post("/api/v1/demo/session")
        response = browser.post("/api/v1/radio/stream", json={})
    events = relayed(response)
    assert any(event["type"] == "detection" for event in events), "Partial real observations are retained"
    assert events[-1] == radio_broker.INVESTIGATION_FAILED
    assert not any(event["type"] == "complete" for event in events)


def test_radio_relays_only_the_private_service_and_redacts_its_key(demo, monkeypatch):
    """WAPI-4: the older public upstream variables configure nothing; the private service's key never reaches the
    browser, and the browser cannot name the upstream."""
    key = "private-fixture-key-" + "x" * 40
    calls = []

    def connect(url, **kwargs):
        calls.append(url)
        return RadioUpstream([{"type": "complete", "data": {"detail": key}}], 1000)

    monkeypatch.setattr(radio_broker, "connect", connect)
    monkeypatch.setenv("BROADCASTIFY_WS_URL", "wss://radio.example")
    monkeypatch.setenv("BROADCASTIFY_API_KEY", "server-fixture-key")
    with client(demo[0]) as browser:
        browser.post("/api/v1/demo/session")
        refused = browser.post("/api/v1/radio/stream", json={"playback": True})
        assert refused.status_code == 503 and refused.json()["error"]["code"] == "RADIO_NOT_CONFIGURED"
        assert calls == []
        monkeypatch.setenv("RADIO_SERVICE_URL", "http://radio.fixture.internal:8000")
        monkeypatch.setenv("BROADCASTIFY_PRIVATE_KEY", key)
        response = browser.post("/api/v1/radio/stream", json={"playback": True})
        assert response.status_code == 200 and key not in response.text
        assert relayed(response) == [{"type": "complete", "data": {"detail": "[redacted]"}}]
        assert browser.post("/api/v1/radio/stream", json={
            "playback": True, "url": "http://169.254.169.254"}).status_code == 422
    assert calls == ["ws://radio.fixture.internal:8000/ws/monitor/start?playback=true"]


def test_private_radio_uses_service_header_without_credential_in_url(demo, monkeypatch):
    monkeypatch.setenv("RADIO_SERVICE_URL", "http://radio.fixture.internal:8000")
    monkeypatch.setenv("BROADCASTIFY_PRIVATE_KEY", "private-fixture-key-"+"x"*40)
    captured = []

    def connect(url, **kwargs):
        captured.append((url, kwargs))
        return RadioUpstream([{"type": "complete"}], 1000)

    monkeypatch.setattr(radio_broker, "connect", connect)
    with client(demo[0]) as browser:
        browser.post("/api/v1/demo/session")
        assert browser.post("/api/v1/radio/stream", json={"playback": True}).status_code == 200
    url, options = captured[0]
    assert url == "ws://radio.fixture.internal:8000/ws/monitor/start?playback=true"
    assert options["additional_headers"]["Authorization"].startswith("Bearer private-fixture-key-")


def test_radio_relay_forwards_the_investigation_events_and_one_fixed_error(demo, monkeypatch):
    """BCFY-4: every event the radio service sends reaches the browser in order; a failure reaches it once, as the
    broker's own text in the shape the UI reads (data.message), with no upstream detail."""
    monkeypatch.setenv("RADIO_SERVICE_URL", "http://radio.fixture.internal:8000")
    monkeypatch.setenv("BROADCASTIFY_PRIVATE_KEY", "private-fixture-key-"+"x"*40)
    outside_the_contract = {"type": "status", "data": {"detail": "not sent on /ws/monitor/start"}}
    upstreams = [RadioUpstream([*RADIO_RUN[:1], outside_the_contract, *RADIO_RUN[1:]], 1000),
                 RadioUpstream(FAILED_RADIO_RUN, 1011)]

    def connect(url, **kwargs):
        if not upstreams:
            raise OSError("radio service unreachable")
        return upstreams.pop(0)

    monkeypatch.setattr(radio_broker, "connect", connect)
    with client(demo[0]) as browser:
        browser.post("/api/v1/demo/session")
        streams = [browser.post("/api/v1/radio/stream", json={"playback": True}) for _ in range(3)]
    assert [stream.status_code for stream in streams] == [200, 200, 200]
    completed, failed, unreachable = (relayed(stream) for stream in streams)
    assert completed == RADIO_RUN
    assert failed == [*RADIO_RUN[:6], {"type": "error", "data": {"message": "Radio investigation could not complete."}}]
    assert unreachable == [{"type": "error", "data": {"message": "Radio connection unavailable. Please retry."}}]
    assert "monitoring_error" not in streams[1].text and "Radio monitoring could not complete" not in streams[1].text


def test_radio_investigations_spend_the_shared_daily_agent_budget(demo, monkeypatch):
    """Each radio stream is one request from the shared daily agent budget; past it, 429 before any radio call."""
    monkeypatch.setenv("AGENT_REQUESTS_PER_DAY", "1")
    monkeypatch.setenv("RADIO_SERVICE_URL", "http://radio.fixture.internal:8000")
    monkeypatch.setenv("BROADCASTIFY_PRIVATE_KEY", "private-fixture-key-"+"x"*40)
    calls = []

    def connect(url, **kwargs):
        calls.append(url)
        return RadioUpstream([{"type": "complete"}], 1000)

    monkeypatch.setattr(radio_broker, "connect", connect)
    with client(demo[0]) as alice, client(demo[0]) as bob:
        for browser in (alice, bob):
            browser.post("/api/v1/demo/session")
        assert alice.post("/api/v1/radio/stream", json={"playback": True}).status_code == 200
        limited = bob.post("/api/v1/radio/stream", json={"playback": True})
        assert limited.status_code == 429 and limited.json()["error"]["code"] == "DAILY_LIMIT_REACHED"
        assert "Daily demo limit reached for AI agent requests" in limited.json()["error"]["message"]
        assert int(limited.headers["retry-after"]) > 0
    assert len(calls) == 1
