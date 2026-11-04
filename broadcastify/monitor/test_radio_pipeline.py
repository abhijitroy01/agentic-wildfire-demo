"""Radio regression checks with locked SDKs and mocked remote responses."""
import asyncio
import io
import json
import os
import subprocess  # nosec B404 - runs a fixed python -c import check, never a shell
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from botocore.exceptions import ClientError
from fastapi import WebSocketDisconnect

# Fixture settings, and never the machine's AWS profile: botocore resolves credentials (including a profile's
# credential_process) when the radio modules create their clients.
os.environ.update(AWS_EC2_METADATA_DISABLED="true", AWS_REGION="us-west-2",
                  S3_BUCKET="fixture", MONITOR_BEDROCK_MODEL_ID="fixture", MONITOR_MODEL_MAX_TOKENS="512",
                  AWS_CONFIG_FILE=os.devnull, AWS_SHARED_CREDENTIALS_FILE=os.devnull)

from broadcastify.monitor import investigation
from broadcastify.monitor.audio_sources.s3_source import S3AudioSource
from broadcastify.monitor.constants import SAMPLE_RATE
from broadcastify.monitor.keyword_detector import (
    AddressExtractionIncomplete,
    AddressExtractionRefused,
    KeywordDetector,
)
from broadcastify.monitor.monitors.base import DetectionEvent
from broadcastify.monitor.monitors.poc_monitor import POCMonitor
from broadcastify.monitor.transcription_client import TranscriptHandler, TranscriptionClient
from broadcastify.shared.aws_clients import AWSClients

# An incident address heard on air: it may reach the client, never a log record.
PLANTED = "17000 Sunset Blvd, Pacific Palisades"


def model_response(text=None, *, stop="end_turn", content=None):
    blocks = content if content is not None else (
        [{"type": "text", "text": text}] if text is not None else [])
    return {"body": io.BytesIO(json.dumps({"content": blocks, "stop_reason": stop}).encode())}


def transcript_event(text, partial):
    """Build one Transcribe streaming result event holding a single alternative."""
    return SimpleNamespace(transcript=SimpleNamespace(results=[
        SimpleNamespace(is_partial=partial, alternatives=[SimpleNamespace(transcript=text)])]))


def detection_dict(transcript, feed, location="Fixture address"):
    """Return what KeywordDetector.detect reports for a transmission with a location."""
    return {"timestamp": "2026-09-28T00:00:00Z", "keywords": ["fire"], "location": location,
            "confidence": 0.9, "transcript": transcript, "feed_name": feed}


class ExtractorTests(unittest.TestCase):
    def detector(self, *responses):
        with patch("broadcastify.monitor.keyword_detector.AWSClients.bedrock") as client:
            client.return_value.invoke_model.side_effect = responses
            return KeywordDetector()

    def test_empty_or_truncated_results_fail_instead_of_returning_none(self):
        for response in (model_response(), model_response(stop="max_tokens"),
                         model_response("Partial address", stop="max_tokens")):
            with self.subTest(response=response):
                detector = self.detector(response)
                with self.assertRaises(RuntimeError):
                    detector._extract_location_llm("fire transcript")
                self.assertTrue(response["body"].closed)
                self.assertFalse(detector.location_cache)

    def test_refusal_is_explicit_and_identical_segment_is_not_resubmitted(self):
        detector = self.detector(model_response(stop="refusal"), model_response("NONE"))
        for _ in range(2):
            with self.assertRaises(AddressExtractionRefused):
                detector._extract_location_llm("declined segment")
        self.assertEqual(detector.bedrock.invoke_model.call_count, 1)
        self.assertIsNone(detector._extract_location_llm("a different transmission"))
        self.assertEqual(detector.bedrock.invoke_model.call_count, 2)

    def test_street_number_alone_waits_for_a_name(self):
        for address in ("1234", "behind 1234", "at the location of 1234", "1234 block", "near number 1234"):
            with self.subTest(address=address):
                detector = self.detector(model_response(address), model_response("1234 Example Lane"))
                for _ in range(2):
                    with self.assertRaises(AddressExtractionIncomplete):
                        detector._extract_location_llm("fire behind location 1234")
                self.assertEqual(detector.bedrock.invoke_model.call_count, 1)
                self.assertEqual(detector._extract_location_llm("fire behind location 1234 Example Lane"),
                                 "1234 Example Lane")

    def test_named_highways_and_intersections_remain_locations(self):
        for address in ("Highway 101 near milepost 45", "Main Street and Example Lane", "500 block of Main Street"):
            with self.subTest(address=address):
                detector = self.detector(model_response(address))
                self.assertEqual(detector._extract_location_llm(f"fire at {address}"), address)

    def test_text_blocks_and_complete_transcript_cache(self):
        detector = self.detector(model_response("NONE"), model_response(content=[
            {"type": "thinking", "thinking": "internal"},
            {"type": "text", "text": "17000 Sunset Blvd, Pacific Palisades, CA"}]))
        prefix = "radio transmission " * 20
        self.assertIsNone(detector._extract_location_llm(prefix+"fire"))
        transcript = prefix+"fire at 17000 Sunset Blvd, Pacific Palisades, CA"
        address = detector._extract_location_llm(transcript)
        self.assertEqual(address, "17000 Sunset Blvd, Pacific Palisades, CA")
        self.assertEqual(detector._extract_location_llm(transcript), address)
        self.assertEqual(detector.bedrock.invoke_model.call_count, 2)
        request = json.loads(detector.bedrock.invoke_model.call_args.kwargs["body"])
        self.assertEqual(request["max_tokens"], 512)


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_addresses_do_not_trigger_detection(self):
        callback = Mock()
        handler = TranscriptHandler(object(), "fixture", callback)
        def event(text, partial):
            return SimpleNamespace(transcript=SimpleNamespace(results=[
                SimpleNamespace(is_partial=partial, alternatives=[SimpleNamespace(transcript=text)])]))
        await handler.handle_transcript_event(event("Fire at 170", True))
        callback.assert_not_called()
        await handler.handle_transcript_event(event("Fire at 17000 Sunset Boulevard", False))
        callback.assert_called_once()
        self.assertEqual(callback.call_args.args[0], "Fire at 17000 Sunset Boulevard")

    def stream_client(self):
        stream = SimpleNamespace(output_stream=object(), input_stream=SimpleNamespace(
            send_audio_event=AsyncMock(), end_stream=AsyncMock()))
        client = TranscriptionClient.__new__(TranscriptionClient)
        client.client = SimpleNamespace(start_stream_transcription=AsyncMock(return_value=stream))
        return client, stream

    async def test_failed_result_handler_stops_audio_before_recording_eof(self):
        client, stream = self.stream_client()
        emitted = []
        async def audio():
            for n in range(100):
                await asyncio.sleep(0)
                emitted.append(n)
                yield b"pcm"
        with patch("broadcastify.monitor.transcription_client.TranscriptHandler") as handler:
            handler.return_value.handle_events = AsyncMock(side_effect=RuntimeError("extractor failed"))
            with self.assertRaisesRegex(RuntimeError, "extractor failed"):
                await client.transcribe_stream(audio(), "fixture", Mock())
        self.assertLess(len(emitted), 100)
        stream.input_stream.end_stream.assert_awaited_once()

    async def test_v1_streams_in_the_configured_language_at_the_pipeline_sample_rate(self):
        client, _ = self.stream_client()
        async def audio():
            yield b"pcm"
        with patch("broadcastify.monitor.transcription_client.TranscriptHandler") as handler, \
                patch("broadcastify.monitor.transcription_client.TRANSCRIBE_LANGUAGE_CODE", "en-AU"):
            handler.return_value.handle_events = AsyncMock()
            await client.transcribe_stream(audio(), "fixture", Mock())
        request = client.client.start_stream_transcription.call_args.kwargs
        self.assertEqual((request["language_code"], request["media_sample_rate_hz"]), ("en-AU", SAMPLE_RATE))

    async def test_recordings_replay_at_the_radio_replay_speed(self):
        speeds = []
        async def convert(source, stream_start, *, speed_multiplier, on_progress):
            speeds.append(speed_multiplier)
            yield b"pcm"
        async def transcripts(audio, feed, callback, **kwargs):
            async for _ in audio:
                pass
        with patch("broadcastify.monitor.monitors.poc_monitor.KeywordDetector"), \
                patch("broadcastify.monitor.monitors.poc_monitor.TranscriptionClient") as transcriber, \
                patch("broadcastify.monitor.monitors.poc_monitor.S3AudioSourceFactory") as factory, \
                patch("broadcastify.monitor.audio_pipeline.FFmpegPipeline") as pipeline, \
                patch("broadcastify.monitor.monitors.poc_monitor.RADIO_REPLAY_SPEED", 2.5):
            factory.return_value.create_source.return_value.close = AsyncMock()
            pipeline.return_value.cleanup = AsyncMock()
            pipeline.return_value.convert = convert
            transcriber.return_value.transcribe_stream = AsyncMock(side_effect=transcripts)
            await POCMonitor()._process_file("fixture.mp3")
        self.assertEqual(speeds, [2.5])

    async def test_cancelled_input_cancels_consumer_and_closes_transcribe(self):
        client, stream = self.stream_client()
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def consume():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async def audio():
            await asyncio.Event().wait()
            yield b"pcm"
        with patch("broadcastify.monitor.transcription_client.TranscriptHandler") as handler:
            handler.return_value.handle_events = consume
            task = asyncio.create_task(client.transcribe_stream(audio(), "fixture", Mock()))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(cancelled.is_set())
        stream.input_stream.end_stream.assert_awaited_once()

    async def test_processing_failure_is_not_successful_completion(self):
        with patch("broadcastify.monitor.monitors.poc_monitor.KeywordDetector"), \
                patch("broadcastify.monitor.monitors.poc_monitor.TranscriptionClient"), \
                patch("broadcastify.monitor.monitors.poc_monitor.S3AudioSourceFactory") as factory:
            factory.return_value.get_files_from_config.return_value = ["fixture.mp3"]
            monitor = POCMonitor()
            monitor._process_file = AsyncMock(side_effect=RuntimeError("extractor failed"))
            result = await monitor.run()
        self.assertFalse(result.success)
        self.assertEqual(result.error_message, "extractor failed")
        self.assertEqual(result.total_detections, 0)

    async def test_refused_segment_is_disclosed_before_a_later_detection(self):
        event_handler = SimpleNamespace(log=AsyncMock(), detection=AsyncMock())
        with patch("broadcastify.monitor.monitors.poc_monitor.KeywordDetector") as detector, \
                patch("broadcastify.monitor.monitors.poc_monitor.TranscriptionClient") as transcriber, \
                patch("broadcastify.monitor.monitors.poc_monitor.S3AudioSourceFactory") as factory, \
                patch("broadcastify.monitor.audio_pipeline.FFmpegPipeline") as pipeline:
            factory.return_value.create_source.return_value.close = AsyncMock()
            pipeline.return_value.cleanup = AsyncMock()
            detector.return_value.detect.side_effect = [
                AddressExtractionRefused("declined"),
                {"timestamp": "2026-09-28T00:00:00Z", "keywords": ["fire"],
                 "location": "Fixture address", "confidence": 0.9,
                 "transcript": "Exercise fire at Fixture address", "feed_name": "fixture"}]
            async def transcripts(audio, feed, callback, **kwargs):
                # The transcription client awaits the monitor's async transcript callback.
                await callback("declined segment", feed, 1)
                await callback("a different transmission", feed, 2)
            transcriber.return_value.transcribe_stream = AsyncMock(side_effect=transcripts)
            monitor = POCMonitor(event_handler=event_handler)
            await monitor._process_file("fixture.mp3")
        self.assertEqual(len(monitor.all_detections), 1)
        self.assertEqual(monitor.first_detection.location, "Fixture address")
        event_handler.log.assert_awaited()
        self.assertIn("no location was accepted", event_handler.log.call_args.args[1])
        self.assertEqual(event_handler.detection.await_count, 1)

    async def test_async_transcript_callbacks_are_awaited(self):
        callback = AsyncMock()
        handler = TranscriptHandler(object(), "fixture", callback)
        await handler.handle_transcript_event(transcript_event("Fire at 17000 Sunset Boulevard", False))
        callback.assert_awaited_once()

    async def process_one_transmission(self, event_handler, detect):
        """Run POCMonitor._process_file over one transmission with mocked audio, Transcribe and detector."""
        with patch("broadcastify.monitor.monitors.poc_monitor.KeywordDetector") as detector, \
                patch("broadcastify.monitor.monitors.poc_monitor.TranscriptionClient") as transcriber, \
                patch("broadcastify.monitor.monitors.poc_monitor.S3AudioSourceFactory") as factory, \
                patch("broadcastify.monitor.audio_pipeline.FFmpegPipeline") as pipeline:
            factory.return_value.create_source.return_value.close = AsyncMock()
            pipeline.return_value.cleanup = AsyncMock()
            detector.return_value.detect.side_effect = detect
            async def transcripts(audio, feed, callback, **kwargs):
                await callback("Exercise fire at Fixture address", feed, 1)
            transcriber.return_value.transcribe_stream = AsyncMock(side_effect=transcripts)
            monitor = POCMonitor(event_handler=event_handler)
            await monitor._process_file("fixture.mp3")
        return monitor

    async def test_detection_runs_off_the_event_loop_and_its_ui_event_is_delivered(self):
        threads = []
        def detect(transcript, feed):
            threads.append(threading.get_ident())
            return detection_dict(transcript, feed)
        event_handler = SimpleNamespace(log=AsyncMock(), detection=AsyncMock())
        monitor = await self.process_one_transmission(event_handler, detect)
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], threading.get_ident())
        event_handler.detection.assert_awaited_once()
        self.assertFalse(monitor._background_tasks)

    async def test_a_failed_ui_event_is_logged_not_lost(self):
        canary = "private transcript and signed URL https://private.invalid/?key=secret"
        event_handler = SimpleNamespace(log=AsyncMock(), detection=AsyncMock(side_effect=RuntimeError(canary)))
        with self.assertLogs("broadcastify.monitor", "ERROR") as logs:
            monitor = await self.process_one_transmission(event_handler, detection_dict)
        self.assertIn('"operation": "ui_event"', "\n".join(logs.output))
        self.assertIn('"type": "RuntimeError"', "\n".join(logs.output))
        self.assertNotIn(canary, "\n".join(logs.output))
        self.assertTrue(all(record.exc_info is None for record in logs.records))
        self.assertFalse(monitor._background_tasks)


class BlockingWorkTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_recording_loads_off_the_event_loop(self):
        threads = []
        def get_object(**request):
            threads.append(threading.get_ident())
            return {"Body": io.BytesIO(b"\xff" * 4000)}
        with patch("broadcastify.monitor.audio_sources.s3_source.AWSClients.s3",
                   return_value=SimpleNamespace(get_object=get_object)), \
                patch("broadcastify.monitor.audio_sources.s3_source.probe_mp3_bytes_per_sec", return_value=4000):
            stream = S3AudioSource("input/fixture.mp3").stream(time.time())
            first = await anext(stream)
            await stream.aclose()
        self.assertEqual(first, b"\xff" * 400, "the first chunk holds 100 ms of the 4000 B/s recording")
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], threading.get_ident())

    def test_radio_clients_bound_their_waits_and_retries(self):
        """Each radio AWS client sets its timeouts and two standard-mode attempts; S3 keeps SigV4 for presigning."""
        for name, read_timeout in (("s3", 30), ("bedrock", 60)):
            with self.subTest(client=name), patch.object(AWSClients, f"_{name}", None):
                config = getattr(AWSClients, name)().meta.config
                self.assertEqual((config.connect_timeout, config.read_timeout), (5, read_timeout))
                self.assertEqual(config.retries, {"mode": "standard", "total_max_attempts": 2})
        with patch.object(AWSClients, "_s3", None):
            self.assertEqual(AWSClients.s3().meta.config.signature_version, "s3v4")


class ServerLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_processing_failure_logs_safe_chained_types_codes_and_frame_positions_once(self):
        canary = "private transcript https://signed.invalid/?credential=private-secret"
        websocket = SimpleNamespace(accept=AsyncMock(), send_json=AsyncMock(), close=AsyncMock())

        async def private_failure(*_args):
            try:
                raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": canary}}, "InvokeModel")
            except ClientError as cause:
                raise RuntimeError(canary) from cause

        with patch("broadcastify.monitor.monitors.poc_monitor.KeywordDetector"), \
                patch("broadcastify.monitor.monitors.poc_monitor.TranscriptionClient"), \
                patch("broadcastify.monitor.monitors.poc_monitor.S3AudioSourceFactory") as factory:
            factory.return_value.get_files_from_config.return_value = ["input/fixture.mp3"]
            monitor = POCMonitor()
            monitor._process_file = AsyncMock(side_effect=private_failure)
            with patch.object(investigation, "POCMonitor", return_value=monitor), \
                    self.assertLogs("broadcastify", "DEBUG") as logs:
                await investigation.run_monitor(websocket, "fixture", False)
        logged = [line for line in logs.output if '"operation": "monitor_processing"' in line]
        self.assertEqual(len(logged), 1, logs.output)
        self.assertTrue(logged[0].startswith("ERROR:broadcastify.monitor.investigation:radio_failure"))
        self.assertIn('"type": "ClientError"', logged[0])
        self.assertIn('"aws_code": "AccessDeniedException"', logged[0])
        self.assertIn('"function": "private_failure"', logged[0])
        self.assertRegex(logged[0], r'"line": \d+')
        self.assertNotIn(canary, "\n".join(logs.output))
        self.assertNotIn("Traceback (most recent call last)", "\n".join(logs.output))
        self.assertTrue(all(record.exc_info is None for record in logs.records))
        sent = [call.args[0] for call in websocket.send_json.await_args_list]
        self.assertEqual(sent[-1], {"type": "error", "data": {
            "message": "Radio monitoring could not complete. Please retry.", "error_type": "monitoring_error"}})
        self.assertNotIn(canary, json.dumps(sent))

    async def test_disconnect_during_monitoring_is_quiet_and_unregisters_the_session(self):
        websocket = SimpleNamespace(accept=AsyncMock(), send_json=AsyncMock(side_effect=WebSocketDisconnect()),
                                    close=AsyncMock())
        with self.assertNoLogs("broadcastify.monitor", "WARNING"):
            await investigation.run_monitor(websocket, "fixture", False)
        self.assertFalse(investigation.active_sessions)
        self.assertFalse(investigation.ws_manager._connections)
        websocket.close.assert_not_awaited()

    def test_only_the_service_entry_point_configures_logging(self):
        """Importing radio modules adds no log handler; the deployed entry point adds one, at INFO."""
        check = ("import logging; import broadcastify.monitor.investigation; root = logging.getLogger(); "
                 "assert not root.handlers, root.handlers; import broadcastify.monitor.private; "
                 "assert len(root.handlers) == 1 and root.level == logging.INFO, (root.handlers, root.level)")
        result = subprocess.run([sys.executable, "-c", check], cwd=Path(__file__).resolve().parents[2],
                                env=dict(os.environ), capture_output=True, timeout=15, check=False)  # nosec B603
        self.assertEqual(result.returncode, 0, result.stderr.decode())


class LogRedactionTests(unittest.IsolatedAsyncioTestCase):
    async def test_detection_logs_carry_sizes_not_transcripts_or_locations(self):
        with patch("broadcastify.monitor.keyword_detector.AWSClients.bedrock") as client, \
                patch("broadcastify.monitor.keyword_detector.FIRE_KEYWORDS", ["fire"]):
            client.return_value.invoke_model.return_value = model_response(PLANTED)
            detector = KeywordDetector()
        with patch("broadcastify.monitor.monitors.poc_monitor.KeywordDetector"), \
                patch("broadcastify.monitor.monitors.poc_monitor.TranscriptionClient"), \
                patch("broadcastify.monitor.monitors.poc_monitor.S3AudioSourceFactory"):
            monitor = POCMonitor()
        handler = TranscriptHandler(object(), "fixture", Mock())
        with patch("broadcastify.monitor.keyword_detector.FIRE_KEYWORDS", ["fire"]), \
                self.assertLogs("broadcastify", "DEBUG") as logs:
            detection = detector.detect(f"Engine 40 reports a fire at {PLANTED}", "fixture")
            for partial in (True, False):
                await handler.handle_transcript_event(transcript_event(f"fire at {PLANTED}", partial))
            monitor.on_detection(DetectionEvent(**detection, elapsed_time=1.0, source_id="fixture.mp3"))
            await monitor._drain_background_tasks()
        self.assertEqual(detection["location"], PLANTED)
        self.assertIn("Location extracted via LLM (36 characters)", "\n".join(logs.output))
        self.assertNotIn("Sunset", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
