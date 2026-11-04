"""Decoder and consumer failures must not become successful empty investigations."""
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.update(AWS_EC2_METADATA_DISABLED="true", AWS_REGION="us-west-2", S3_BUCKET="fixture",
                  MONITOR_BEDROCK_MODEL_ID="fixture", MONITOR_MODEL_MAX_TOKENS="512")

from broadcastify.monitor.audio_pipeline import FFmpegPipeline
from broadcastify.monitor.events.event_types import MonitorEvent
from broadcastify.monitor.events.websocket_handler import WebSocketEventHandler
from broadcastify.monitor.monitors.base import DetectionEvent
from broadcastify.monitor.monitors.poc_monitor import POCMonitor


class AudioResilience(unittest.IsolatedAsyncioTestCase):
    async def test_decoder_cleanup_diagnostics_exclude_chained_provider_text(self):
        canary = "private decoder body https://signed.invalid/?credential=private-secret"
        cause = OSError(canary)
        error = RuntimeError(canary)
        error.__cause__ = cause
        pipeline = FFmpegPipeline(lambda: False)
        pipeline.ffmpeg_process = SimpleNamespace(kill=Mock(side_effect=error))
        with self.assertLogs("broadcastify.monitor.audio_pipeline", "DEBUG") as logs:
            await pipeline.cleanup()
        self.assertIsNone(pipeline.ffmpeg_process)
        self.assertIn('"operation": "decoder_cleanup"', "\n".join(logs.output))
        self.assertIn('"type": "OSError"', "\n".join(logs.output))
        self.assertNotIn(canary, "\n".join(logs.output))
        self.assertTrue(all(record.exc_info is None for record in logs.records))

    async def test_pcm_reader_failure_and_decoder_exit_propagate(self):
        class Source:
            async def stream(self, *_args, **_kwargs):
                yield b"fixture-mp3"
        for read_error, code in ((OSError("private decoder diagnostic"), 0), (None, 1)):
            process = SimpleNamespace(
                stdin=SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock(), wait_closed=AsyncMock()),
                stdout=SimpleNamespace(read=AsyncMock(side_effect=read_error, return_value=b"")),
                wait=AsyncMock(return_value=code), kill=Mock())
            pipeline = FFmpegPipeline(lambda: False)
            with patch("broadcastify.monitor.audio_pipeline.asp.create_subprocess_exec", AsyncMock(return_value=process)):
                with self.assertRaises(OSError if read_error else RuntimeError):
                    async for _chunk in pipeline.convert(Source(), time.time()):
                        pass
                await pipeline.cleanup()

    async def test_deliberate_early_stop_does_not_check_decoder_success(self):
        class Source:
            async def stream(self, *_args, **_kwargs):
                yield b"fixture-mp3"
        process = SimpleNamespace(stdin=SimpleNamespace(write=Mock()),
                                  stdout=SimpleNamespace(read=AsyncMock()), kill=Mock(),
                                  wait=AsyncMock(return_value=1))
        pipeline = FFmpegPipeline(lambda: True)
        with patch("broadcastify.monitor.audio_pipeline.asp.create_subprocess_exec", AsyncMock(return_value=process)):
            self.assertEqual([chunk async for chunk in pipeline.convert(Source(), time.time())], [])
            process.wait.assert_not_awaited()
            await pipeline.cleanup()

    async def test_lost_consumer_requests_monitor_stop(self):
        stopped = Mock()
        manager = SimpleNamespace(send_event=AsyncMock(return_value=False))
        handler = WebSocketEventHandler(manager, "session", on_disconnect=stopped)
        await handler.handle(MonitorEvent.log("INFO", "Progress"))
        self.assertTrue(handler.disconnected)
        stopped.assert_called_once()

    async def test_missing_location_is_emitted_as_missing(self):
        event_handler = SimpleNamespace(detection=AsyncMock())
        with patch("broadcastify.monitor.monitors.poc_monitor.KeywordDetector"), \
                patch("broadcastify.monitor.monitors.poc_monitor.TranscriptionClient"), \
                patch("broadcastify.monitor.monitors.poc_monitor.S3AudioSourceFactory"):
            monitor = POCMonitor(event_handler=event_handler)
            detection = DetectionEvent(timestamp="2026-10-30T00:00:00Z", keywords=["fire"], location=None,
                                       confidence=0.0, transcript="A fire was heard", feed_name="fixture")
            await monitor._emit_detection(detection)
        self.assertIsNone(event_handler.detection.call_args.kwargs["location"])
        self.assertEqual(event_handler.detection.call_args.kwargs["confidence"], 0.0)


if __name__ == "__main__":
    unittest.main()
