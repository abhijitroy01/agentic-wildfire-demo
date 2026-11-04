"""Private surface checks; no AWS or transcription requests."""
import asyncio
import hmac
import importlib
import importlib.util
import io
import json
import logging
import os
import shutil
import subprocess  # nosec B404 - the FFmpeg check runs fixed argument lists, never a shell
import sys
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

# Settings are read at import; fixture values and empty AWS config files keep these checks offline.
os.environ.update(AWS_EC2_METADATA_DISABLED="true", AWS_REGION="us-west-2", S3_BUCKET="fixture",
                  MONITOR_BEDROCK_MODEL_ID="fixture",
                  AWS_CONFIG_FILE=os.devnull, AWS_SHARED_CREDENTIALS_FILE=os.devnull)

from broadcastify.monitor import investigation, private
from broadcastify.monitor.auth import key_matches
from broadcastify.shared import config

KEY = "k" * 40
# The broker's service credential, as the workspace API sends it to the deployed gate.
BEARER = ((b"authorization", b"Bearer " + KEY.encode()),)
RECORDING = "input/LA_County_Fire_Dispatch_2025-01-07/fixture-clip.mp3"
# The radio modules the deployed process loads when it starts; audio_pipeline loads with the first recording.
DEPLOYED_MODULES = {"broadcastify", "broadcastify.monitor", *(f"broadcastify.monitor.{name}" for name in (
    "audio_sources", "audio_sources.base", "audio_sources.s3_source", "auth", "constants", "diagnostics", "events",
    "events.event_handler", "events.event_types", "events.websocket_handler", "investigation", "keyword_detector",
    "monitors", "monitors.base", "monitors.poc_monitor", "private", "transcription_client", "websocket_manager")),
    "broadcastify.shared", "broadcastify.shared.aws_clients", "broadcastify.shared.config", "broadcastify.shared.logger"}


async def http_request(method, path, *, app=private.app):
    """Send one HTTP request through a radio ASGI app (the deployed gate by default); return (status, JSON body)."""
    headers = [(b"host", b"testserver")]
    incoming, sent = [{"type": "http.request", "body": b"", "more_body": False}], []

    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
               "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
               "root_path": "", "headers": headers, "client": ("127.0.0.1", 50000),
               "server": ("testserver", 80)}, receive, send)
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    data = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return status, json.loads(data) if data else None


async def websocket_session(path, query=b"", *, app=private.app, headers=()):
    """Open one radio WebSocket through an ASGI app (the deployed gate by default), disconnect when it waits for
    input; return what it sent."""
    incoming, sent = [{"type": "websocket.connect"}], []

    async def receive():
        return incoming.pop(0) if incoming else {"type": "websocket.disconnect", "code": 1000}

    async def send(message):
        sent.append(message)

    await app({"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws", "http_version": "1.1",
               "path": path, "raw_path": path.encode(), "query_string": query, "root_path": "",
               "headers": [(b"host", b"testserver"), *headers], "client": ("127.0.0.1", 50000),
               "server": ("testserver", 80), "subprotocols": []}, receive, send)
    return sent


async def lifespan(app, sent):
    """Start and then stop a radio ASGI app through the lifespan protocol, as uvicorn does, appending what it sends."""
    incoming = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

    async def receive():
        return incoming.pop(0)

    async def send(message):
        sent.append(message)

    await app({"type": "lifespan", "asgi": {"version": "3.0"}}, receive, send)


def events(sent):
    """Decode the JSON events a WebSocket handler sent."""
    return [json.loads(message["text"]) for message in sent if message["type"] == "websocket.send"]


class RadioBoundaryTests(unittest.TestCase):
    def test_the_deployed_entry_point_loads_only_its_own_modules_and_constructs_no_client(self):
        """Pin the radio modules that importing private.py loads; the missing profile fails any client built then."""
        env = dict(os.environ, AWS_PROFILE="missing-offline-profile", AWS_DEFAULT_PROFILE="missing-offline-profile")
        result = subprocess.run(
            [sys.executable, "-c", ("import json, sys; import broadcastify.monitor.private; "
             "print(json.dumps([name for name in sys.modules if name.split('.')[0] == 'broadcastify']))")],
            cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(set(json.loads(result.stdout.splitlines()[-1])), DEPLOYED_MODULES)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg verification runs in the radio image")
    def test_audio_input_allows_wav_and_rejects_network_playlists(self):
        spec = importlib.util.spec_from_file_location("tested_audio_constants", Path(__file__).with_name("constants.py"))
        constants = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(constants)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\x00\x00"*1600)
        command = ["ffmpeg", "-v", "error", *constants.MEDIA_INPUT_OPTIONS, "-i", "pipe:0",
                   "-map", "0:a:0", "-vn", "-sn", "-dn", "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]
        # Fixed FFmpeg argument lists with no shell; the inputs are this test's own bytes.
        converted = subprocess.run(command, input=buffer.getvalue(), check=False, capture_output=True, timeout=15)  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        self.assertEqual(converted.returncode, 0, converted.stderr.decode())
        self.assertTrue(converted.stdout)
        playlist = b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nhttp://127.0.0.1:9/untrusted.ts\n#EXT-X-ENDLIST\n"
        rejected = subprocess.run(command, input=playlist, check=False, capture_output=True, timeout=15)  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        self.assertNotEqual(rejected.returncode, 0)
        # New FFmpeg releases reject HLS during stdin probing. Force the demuxer
        # in this second check to exercise our explicit format allowlist as well.
        forced = command.copy()
        forced[forced.index("-i"):forced.index("-i")] = ["-f", "hls"]
        restricted = subprocess.run(forced, input=playlist, check=False, capture_output=True, timeout=15)  # nosec B603  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        self.assertNotEqual(restricted.returncode, 0)
        # The minimal image omits HLS entirely; broader local installations
        # reject it through the explicit format allowlist.
        error = restricted.stderr.lower()
        self.assertTrue(b"whitelist" in error or b"unknown input format" in error, error)

    @unittest.skipUnless(Path("/opt/ffmpeg/bin/ffmpeg").is_file(), "Minimal decoder verification runs in the image")
    def test_minimal_audio_runtime_has_no_network_or_video_decoder(self):
        """Inspect the actual shipped executable, not configure flags."""
        executable = "/opt/ffmpeg/bin/ffmpeg"
        # Fixed local executable and arguments; no shell or external media.
        protocols = subprocess.check_output(  # nosec B603 - fixed arguments
            [executable, "-v", "error", "-protocols"], text=True, timeout=15)
        names = {line.strip() for line in protocols.splitlines()
                 if line.strip() not in ("Supported file protocols:", "Input:", "Output:")}
        self.assertEqual(names, {"pipe"})
        decoders = subprocess.check_output(  # nosec B603 - fixed arguments
            [executable, "-v", "error", "-decoders"], text=True, timeout=15)
        self.assertFalse(any(line.strip().startswith("V") and "=" not in line for line in decoders.splitlines()))
        formats = subprocess.check_output(  # nosec B603 - fixed arguments
            [executable, "-v", "error", "-demuxers"], text=True, timeout=15)
        for name in ("mp3", "wav", "mov", "ogg", "flac"):
            self.assertTrue(any(name in line.split(",")[0].split() for line in formats.splitlines()), name)
        libraries = subprocess.check_output(  # nosec B603 B607 - fixed arguments
            ["ldd", executable], text=True, timeout=15)
        for name in ("not found", "libxml2", "libcjson", "librsvg", "libexpat", "libjxl", "libcurl"):
            self.assertNotIn(name, libraries)

    def test_only_authenticated_fixed_websocket_and_health_are_forwarded(self):
        forwarded = []
        async def monitor(scope, receive, send):
            forwarded.append(scope)
        module = types.ModuleType("broadcastify.monitor.investigation")
        module.app = monitor
        spec = importlib.util.spec_from_file_location("tested_radio_private", Path(__file__).with_name("private.py"))
        private = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"broadcastify.monitor.investigation": module}):
            spec.loader.exec_module(private)
        async def exercise():
            responses = []
            async def receive():
                return {"type": "websocket.connect"}
            async def send(event):
                responses.append(event)
            base = {"type": "websocket", "path": "/ws/monitor/start", "headers": [], "query_string": b"playback=true"}
            with patch.dict(os.environ, {"API_KEY": "x"*64}):
                await private.app(base, receive, send)
                self.assertEqual(responses[-1]["code"], 1008)
                self.assertFalse(forwarded)
                auth = {**base, "headers": [(b"authorization", b"Bearer "+b"x"*64)]}
                await private.app(auth, receive, send)
                self.assertEqual(len(forwarded), 1)
                await private.app({**auth, "path": "/ws/transcribe"}, receive, send)
                await private.app({**auth, "query_string": b"url=http://attacker.invalid"}, receive, send)
                await private.app({"type": "http", "path": "/api/report", "method": "POST"}, receive, send)
                self.assertEqual(len(forwarded), 1)
                self.assertEqual(responses[-2]["status"], 404)
                await private.app({"type": "http", "path": "/health", "method": "GET"}, receive, send)
                self.assertEqual(len(forwarded), 2)
        asyncio.run(exercise())


class PlaybackRecording:
    """Stands in for POCMonitor: offers one recording to the playback callback, then completes without a detection."""

    def __init__(self, *, feed_name, event_handler, on_audio_source):
        self.on_audio_source = on_audio_source

    def request_stop(self):
        pass

    async def run(self):
        if self.on_audio_source:
            await self.on_audio_source(RECORDING)
        return types.SimpleNamespace(success=True, total_detections=0, sources_processed=1, first_detection=None)


class PrivateServiceTests(unittest.IsolatedAsyncioTestCase):
    """The deployed entry point as the radio task runs it: private.app in front of the real investigation app."""

    async def test_startup_refuses_a_missing_or_short_key(self):
        for configured in (None, "", "k" * 31):
            sent = []
            with self.subTest(key_length=None if configured is None else len(configured)), patch.dict(os.environ):
                os.environ.pop("API_KEY", None)
                if configured is not None:
                    os.environ["API_KEY"] = configured
                with self.assertRaisesRegex(RuntimeError, "API_KEY must contain at least 32 characters"):
                    await lifespan(private.app, sent)
                self.assertEqual([message["type"] for message in sent], ["lifespan.startup.failed"])
        sent = []
        with patch.dict(os.environ, {"API_KEY": KEY}):
            await lifespan(private.app, sent)
            status, body = await http_request("GET", "/health", app=private.app)
        self.assertEqual([message["type"] for message in sent],
                         ["lifespan.startup.complete", "lifespan.shutdown.complete"])
        self.assertEqual((status, body["status"]), (200, "healthy"))

    async def test_playback_presigns_each_recording_for_the_bearer_authenticated_relay(self):
        signer = Mock(generate_presigned_url=Mock(return_value="https://fixture.invalid/fixture-clip.mp3?signed"))
        with patch.dict(os.environ, {"API_KEY": KEY}), patch.object(investigation, "POCMonitor", PlaybackRecording), \
                patch("broadcastify.shared.aws_clients.AWSClients.s3", return_value=signer):
            played = await websocket_session("/ws/monitor/start", b"playback=true", app=private.app, headers=BEARER)
            silent = await websocket_session("/ws/monitor/start", b"playback=false", app=private.app, headers=BEARER)
        self.assertEqual([event["type"] for event in events(played)], ["connected", "simulation_audio", "complete"])
        self.assertEqual(events(played)[1]["data"],
                         {"url": "https://fixture.invalid/fixture-clip.mp3?signed", "file": "fixture-clip.mp3"})
        signer.generate_presigned_url.assert_called_once_with(
            "get_object", Params={"Bucket": "fixture", "Key": RECORDING},
            ExpiresIn=investigation.PRESIGNED_URL_EXPIRY_SECONDS)
        self.assertEqual(played[-1], {"type": "websocket.close", "code": 1000, "reason": "Monitoring complete"})
        # Without playback the investigation runs the same way, and nothing is presigned or sent for audio.
        self.assertEqual([event["type"] for event in events(silent)], ["connected", "complete"])
        self.assertNotIn(KEY, json.dumps(played + silent))
        self.assertEqual(investigation.active_sessions, {})

    def test_uvicorn_log_lines_never_show_the_api_key(self):
        lines = (("uvicorn.error", '%s - "WebSocket %s" [accepted]',
                  ("127.0.0.1:50000", f"/ws/monitor/start?playback=true&api_key={KEY}")),
                 ("uvicorn.access", '%s - "%s %s HTTP/%s" %d',
                  ("127.0.0.1:50000", "GET", f"/health?api_key={KEY}&probe=1", "1.1", 200)))
        for name, message, args in lines:
            with self.assertLogs(name, "INFO") as logs:
                logging.getLogger(name).info(message, *args)
            self.assertNotIn(KEY, logs.output[0])
            self.assertIn("api_key=[redacted]", logs.output[0])


class RadioSettingsTests(unittest.TestCase):
    def test_only_the_deployed_settings_are_required_at_import(self):
        deployed = {name: os.environ[name] for name in ("AWS_REGION", "S3_BUCKET", "MONITOR_BEDROCK_MODEL_ID")}
        try:
            with patch("dotenv.load_dotenv"), patch.dict(os.environ, deployed, clear=True):
                importlib.reload(config)
                for name in deployed:
                    with self.subTest(missing=name), patch.dict(os.environ), self.assertRaisesRegex(ValueError, name):
                        del os.environ[name]
                        importlib.reload(config)
        finally:
            importlib.reload(config)

    def test_numbers_are_parsed_strictly_and_name_the_variable(self):
        for value, number in (("", 7), ("  ", 7), (" 12 ", 12), ("-8", -8)):
            with patch.dict(os.environ, {"FIXTURE_COUNT": value}):
                self.assertEqual(config.Config.get_int("FIXTURE_COUNT", 7), number)
        for value in ("ten", "2.5", "1e3"):
            with patch.dict(os.environ, {"FIXTURE_COUNT": value}), \
                    self.assertRaisesRegex(RuntimeError, "FIXTURE_COUNT must be a whole number"):
                config.Config.get_int("FIXTURE_COUNT", 7)
        for value, number in (("", 0.5), ("0.25", 0.25), ("3", 3.0)):
            with patch.dict(os.environ, {"FIXTURE_RATIO": value}):
                self.assertEqual(config.Config.get_float("FIXTURE_RATIO", 0.5), number)
        for value in ("half", "nan", "inf"):
            with patch.dict(os.environ, {"FIXTURE_RATIO": value}), \
                    self.assertRaisesRegex(RuntimeError, "FIXTURE_RATIO must be a finite number"):
                config.Config.get_float("FIXTURE_RATIO", 0.5)
        # A malformed setting stops the import instead of silently falling back to its default.
        try:
            with patch.dict(os.environ, {"MONITOR_CONFIDENCE_THRESHOLD": "high"}), \
                    self.assertRaisesRegex(RuntimeError, "MONITOR_CONFIDENCE_THRESHOLD"):
                importlib.reload(config)
        finally:
            importlib.reload(config)

    def test_key_check_is_constant_time_and_never_accepts_a_weak_configured_key(self):
        with patch("broadcastify.monitor.auth.hmac.compare_digest", wraps=hmac.compare_digest) as compare:
            self.assertTrue(key_matches(KEY, KEY))
            self.assertTrue(key_matches(KEY.encode(), KEY))
            self.assertFalse(key_matches("k" * 39 + "x", KEY))
        self.assertEqual(compare.call_count, 3)
        for supplied, configured in ((KEY, None), (KEY, ""), ("short", "short"), (None, KEY), ("", KEY)):
            self.assertFalse(key_matches(supplied, configured))


if __name__ == "__main__":
    unittest.main()
