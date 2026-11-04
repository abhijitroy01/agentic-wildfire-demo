"""
POC Monitor Module
Monitor implementation for POC/testing mode.
Processes S3 audio files and stops after first detection with location.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from broadcastify.monitor.audio_sources.s3_source import S3AudioSource, S3AudioSourceFactory
from broadcastify.monitor.diagnostics import log_failure
from broadcastify.monitor.keyword_detector import (
    AddressExtractionIncomplete,
    AddressExtractionRefused,
    KeywordDetector,
)
from broadcastify.monitor.monitors.base import BaseMonitor, DetectionEvent, MonitorResult
from broadcastify.monitor.transcription_client import TranscriptionClient
from broadcastify.shared.config import RADIO_REPLAY_SPEED, TEST_FILES
from broadcastify.shared.logger import LogMessages, text_size

if TYPE_CHECKING:
    from broadcastify.monitor.events.event_handler import EventHandler

logger = logging.getLogger(__name__)


class POCMonitor(BaseMonitor):
    """
    POC Monitor for testing with S3 audio files.

    Behavior:
    - Streams MP3 files from S3 at real-time rate
    - Converts MP3 to PCM using FFmpeg
    - Sends PCM to Amazon Transcribe for real-time transcription
    - Detects fire keywords in transcripts
    - STOPS after first detection with location
    """

    def __init__(
        self,
        feed_name: str = "POC Test",
        on_detection_callback: Callable[[DetectionEvent], None] | None = None,
        event_handler: EventHandler | None = None,
        on_audio_source: Callable | None = None,
    ):
        """
        Initialize POC monitor.

        Args:
            feed_name: Name to use for the test feed
            on_detection_callback: Optional callback for detection events
            event_handler: Optional event handler for streaming events to UI
        """
        super().__init__()
        self.feed_name = feed_name
        self.on_detection_callback = on_detection_callback
        self.event_handler = event_handler
        self.on_audio_source = on_audio_source

        # Components
        self.transcription_client = TranscriptionClient()
        self.keyword_detector = KeywordDetector()
        self.source_factory = S3AudioSourceFactory()

        # State
        self.first_detection: DetectionEvent | None = None
        self.all_detections: list[DetectionEvent] = []
        self.current_source: S3AudioSource | None = None
        self.sources_processed = 0
        self._current_step = 0
        # UI event tasks started from synchronous callbacks; kept until done so none is lost.
        self._background_tasks: set[asyncio.Task] = set()

    def _spawn(self, coroutine) -> None:
        """Run a UI event coroutine in the background, keeping a reference until it finishes."""
        task = asyncio.get_running_loop().create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_done)

    def _background_done(self, task: asyncio.Task) -> None:
        """Forget a finished UI event task and log its failure, if any."""
        self._background_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log_failure(logger, "ui_event", task.exception())

    async def _drain_background_tasks(self) -> None:
        """Wait for pending UI event tasks so their events precede the monitor's next step."""
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

    async def _emit_log(self, level: str, message: str) -> None:
        """Emit a log event to the event handler if configured."""
        if self.event_handler:
            await self.event_handler.log(level, message)

    async def _emit_step_start(self, step: int, name: str) -> None:
        """Emit step start event."""
        self._current_step = step
        if self.event_handler:
            await self.event_handler.step_start(step, name)

    async def _emit_step_complete(self, step: int, name: str, success: bool = True) -> None:
        """Emit step complete event."""
        if self.event_handler:
            await self.event_handler.step_complete(step, name, success)

    async def _emit_detection(self, detection: DetectionEvent) -> None:
        """Emit detection event."""
        if self.event_handler:
            await self.event_handler.detection(
                location=detection.location,
                keywords=detection.keywords,
                confidence=detection.confidence,
                source_id=detection.source_id,
                transcript_excerpt=detection.transcript[:
                                                        200] if detection.transcript else "",
            )

    def should_stop(self) -> bool:
        """Stop after first detection with location."""
        return self._stop_requested or (
            self.first_detection is not None and self.first_detection.location is not None
        )

    def on_detection(self, detection: DetectionEvent) -> None:
        """Handle a fire detection event."""
        self.all_detections.append(detection)

        # Log the detection
        if detection.location and self.first_detection is None:
            # First detection with location - this is the important one
            logger.warning(LogMessages.FIRST_DETECTION)
            logger.warning(
                LogMessages.DETECTION_SUMMARY.format(
                    file=detection.source_id,
                    elapsed=detection.elapsed_time,
                    feed=detection.feed_name,
                    location_size=text_size(detection.location),
                    keywords=", ".join(detection.keywords),
                    confidence=detection.confidence,
                )
            )
            self.first_detection = detection
            self._stop_requested = True  # Signal to stop

            # Signal the audio source to stop
            if self.current_source:
                self.current_source.request_stop()
        else:
            # Detection without location or subsequent detection
            logger.info(
                LogMessages.FIRE_DETECTION.format(
                    location_size=text_size(detection.location),
                    keywords=", ".join(detection.keywords),
                )
            )

        # Call external callback if provided
        if self.on_detection_callback:
            self.on_detection_callback(detection)

        # Emit event for UI
        self._spawn(self._emit_detection(detection))

    async def run(self) -> MonitorResult:
        """
        Main POC monitoring loop.

        Processes S3 files until first detection with location.
        """
        logger.info("Recorded radio investigation starting")
        await self._emit_log("INFO", "Starting Broadcastify Monitor (POC Mode)")
        start_time = datetime.now(UTC)

        # Get files to process
        files = self.source_factory.get_files_from_config(TEST_FILES)
        logger.info("Found %d files to process", len(files))
        await self._emit_log("INFO", f"Found {len(files)} files to process")

        try:
            for i, file_key in enumerate(files, 1):
                if self.should_stop():
                    logger.info("[%d/%d] Skipping - first detection found", i, len(files))
                    continue

                logger.info("[%d/%d] Processing: %s", i, len(files), file_key)
                if self.on_audio_source:
                    await self.on_audio_source(file_key)
                await self._emit_log("INFO", f"[{i}/{len(files)}] Processing: {file_key}")
                await self._emit_step_start(1, f"Processing {file_key.split('/')[-1]}")

                try:
                    await self._process_file(file_key)
                    await self._emit_step_complete(1, "Audio processing")
                    self.sources_processed += 1

                    if self.should_stop():
                        logger.info("First detection found - stopping")
                        break

                except Exception:
                    # Context only: run_monitor logs safe cause diagnostics once.
                    logger.error("Radio processing stopped at recording %d/%d", i, len(files))
                    raise

            # Build result
            self._result = MonitorResult(
                success=True,
                mode="poc",
                start_time=start_time,
                end_time=datetime.now(UTC),
                total_detections=len(self.all_detections),
                first_detection=self.first_detection,
                all_detections=self.all_detections,
                sources_processed=self.sources_processed,
            )

            # Log summary
            logger.info("Processing complete")
            if self.first_detection:
                logger.info("First detection location: %s", text_size(self.first_detection.location))
                logger.info("First detection at: %.1fs", self.first_detection.elapsed_time)

            return self._result

        # Any processing failure becomes an unsuccessful result that carries the exception; run_monitor
        # raises from it and logs safe cause diagnostics once.
        except Exception as e:  # noqa: BLE001 -- converted into the result, see above
            self._result = MonitorResult(
                success=False,
                mode="poc",
                start_time=start_time,
                end_time=datetime.now(UTC),
                total_detections=len(self.all_detections),
                first_detection=self.first_detection,
                all_detections=self.all_detections,
                sources_processed=self.sources_processed,
                error_message=str(e),
                error=e,
            )
            return self._result

    async def _process_file(self, file_key: str) -> None:
        """
        Process a single audio file through the full pipeline.

        Args:
            file_key: S3 file key to process
        """
        from broadcastify.monitor.audio_pipeline import FFmpegPipeline

        logger.info(LogMessages.PROCESSING_FILE.format(file=file_key))
        stream_start = time.time()

        # Create audio source
        self.current_source = self.source_factory.create_source(
            file_key, self.feed_name)

        # Create FFmpeg pipeline
        pipeline = FFmpegPipeline(stop_check=self.should_stop)

        # Progress callback, awaited by pipeline.convert in pcm_audio_stream below
        async def on_progress(chunks: int, elapsed: float):
            logger.info("[%.1fs] Progress: %d PCM chunks sent", elapsed, chunks)
            if chunks % 500 == 0 and self.event_handler:
                await self._emit_log("INFO", f"Progress: {chunks} PCM chunks sent")

        # Transcript callback - creates DetectionEvent on keyword match. The
        # detector blocks on Bedrock, so it runs in a worker thread while audio
        # keeps streaming; the transcription client awaits this coroutine.
        async def on_transcript(transcript: str, feed: str, elapsed_time: float):
            try:
                detection_dict = await asyncio.to_thread(self.keyword_detector.detect, transcript, feed)
            except AddressExtractionRefused:
                message = ("The model declined address extraction for this segment; no location was accepted. "
                           "Listening for another transmission.")
                logger.warning(message)
                await self._emit_log("WARNING", message)
                return
            except AddressExtractionIncomplete:
                message = "A street number was heard; waiting for the street name before resolving a location."
                logger.info(message)
                await self._emit_log("INFO", message)
                return
            if detection_dict:
                detection = DetectionEvent(
                    timestamp=detection_dict["timestamp"],
                    keywords=detection_dict["keywords"],
                    location=detection_dict.get("location"),
                    confidence=detection_dict["confidence"],
                    transcript=detection_dict["transcript"],
                    feed_name=detection_dict["feed_name"],
                    elapsed_time=elapsed_time,
                    source_id=file_key,
                )
                self.on_detection(detection)

        # Replay the demo file faster than real-time (RADIO_REPLAY_SPEED) so a
        # 30 s clip detects in seconds, not 30. AWS guidance for streaming is
        # "as close to real-time as possible" — the source clamps the
        # multiplier to MAX_REPLAY_SPEED to stay inside Transcribe's tolerance.
        async def pcm_audio_stream():
            async for chunk in pipeline.convert(
                self.current_source,
                stream_start,
                speed_multiplier=RADIO_REPLAY_SPEED,
                on_progress=on_progress,
            ):
                yield chunk

        # Transcribe with early termination support
        try:
            await self.transcription_client.transcribe_stream(
                pcm_audio_stream(),
                self.feed_name,
                on_transcript,
                stop_check=self.should_stop,
            )
        except Exception as exc:
            # An error while stopping after the first detection is expected; any other propagates to run().
            if not self.should_stop():
                raise
            log_failure(logger, "transcribe_after_stop", exc, level=logging.DEBUG)
        finally:
            await self._drain_background_tasks()
            await pipeline.cleanup()
            await self.current_source.close()
            self.current_source = None

        total_time = time.time() - stream_start
        if self.first_detection:
            logger.info("Stopped at %.1fs after first detection", total_time)
        else:
            logger.info(LogMessages.FILE_COMPLETE.format(file=file_key))
