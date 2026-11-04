"""
S3 replay audio source.

Streams MP3 files from S3, paced relative to real-time so Amazon
Transcribe streaming sees audio "as close to real-time as possible"
([AWS guidance](https://docs.aws.amazon.com/transcribe/latest/dg/streaming.html)).
The caller picks the multiplier on every ``stream()`` invocation:

- ``1.0``  — real-time (the default).
- ``> 1.0`` — compressed wall-clock for demos. Clamped to
  ``MAX_REPLAY_SPEED`` so we stay inside Transcribe's tolerance.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator

from broadcastify.monitor.audio_sources.base import AudioSource
from broadcastify.monitor.constants import (
    calculate_mp3_chunk_size,
    probe_mp3_bytes_per_sec,
)
from broadcastify.shared.aws_clients import AWSClients
from broadcastify.shared.config import (
    MONITOR_DEFAULT_MP3_BITRATE_BPS,
    PCM_CHUNK_MS,
    S3_BUCKET,
)
from broadcastify.shared.logger import LogMessages

logger = logging.getLogger(__name__)


# Upper bound for replay speed. AWS docs ask for streaming audio "as close
# to real-time as possible"; in practice Transcribe still emits stable
# partial results up to ~3-4× real-time, after which the partial-results
# stabilization layer starts dropping events and downstream keyword
# detection misfires. 4 is a conservative ceiling that matches what AWS
# samples use for non-real-time file replay.
MAX_REPLAY_SPEED = 4.0


class S3AudioSource(AudioSource):
    """Replay an MP3 from S3, paced at ``speed_multiplier × real-time``."""

    def __init__(self, file_key: str, feed_name: str = "S3 File"):
        super().__init__()
        self.file_key = file_key
        self.feed_name = feed_name
        self.bucket = S3_BUCKET
        self.s3 = AWSClients.s3()

        # Loaded on first stream
        self._mp3_data: bytes | None = None
        self._file_size: int = 0
        self._duration_seconds: float = 0.0

        # Per-file values populated by _load_file(). Pacing math depends
        # on the *actual* bitrate — assuming a constant value makes
        # streaming run at N× real-time when the source bitrate doesn't
        # match the assumption (the original 32 kbps vs. 128 kbps bug).
        self._bytes_per_sec: int = MONITOR_DEFAULT_MP3_BITRATE_BPS
        self._mp3_chunk_size: int = 0

        # Pacing state, set when stream() starts: each chunk is scheduled
        # against the audio time and wall time of the stream's first chunk.
        self._speed_multiplier: float = 1.0
        self._pacing_anchor_audio_seconds: float = 0.0
        self._pacing_anchor_wall_seconds: float = 0.0

    # ------------------------------------------------------------------
    # Speed control
    # ------------------------------------------------------------------

    @staticmethod
    def _clamp_speed(value: float) -> float:
        clamped = max(1.0, min(value, MAX_REPLAY_SPEED))
        if clamped != value:
            logger.warning(
                "Speed %.2fx clamped to %.2fx (max replay speed)",
                value, clamped,
            )
        return clamped

    # ------------------------------------------------------------------
    # File loading
    # ------------------------------------------------------------------

    def _load_file(self) -> None:
        """Load MP3 bytes from S3 and probe their actual bitrate."""
        if self._mp3_data is not None:
            return

        logger.info(
            LogMessages.S3_STREAMING.format(
                bucket=self.bucket, key=self.file_key)
        )

        response = self.s3.get_object(Bucket=self.bucket, Key=self.file_key)
        self._mp3_data = response["Body"].read()
        self._file_size = len(self._mp3_data)

        # Probe actual bitrate so pacing matches reality. Without this,
        # a 32 kbps source streamed at the 128 kbps default feeds
        # Transcribe at 4× real-time and transcripts drift ahead of audio.
        self._bytes_per_sec = probe_mp3_bytes_per_sec(
            self._mp3_data, MONITOR_DEFAULT_MP3_BITRATE_BPS
        )
        self._duration_seconds = self._file_size / self._bytes_per_sec
        self._mp3_chunk_size = calculate_mp3_chunk_size(
            PCM_CHUNK_MS, self._bytes_per_sec
        )

        logger.info(
            LogMessages.S3_FILE_INFO.format(
                size=self._file_size, duration=self._duration_seconds
            )
        )
        logger.info(
            "Probed bitrate: %d B/s (%.1f kbps); chunk size: %d bytes",
            self._bytes_per_sec, self._bytes_per_sec * 8 / 1000,
            self._mp3_chunk_size,
        )

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream(
        self,
        start_time: float,
        *,
        speed_multiplier: float = 1.0,
        start_seconds: float = 0.0,
    ) -> AsyncIterator[bytes]:
        self.reset_stop()
        # The S3 download and the ffprobe run block, so they run in a worker thread: one process serves every
        # radio session and /health, and the event loop must keep pacing the others meanwhile.
        await asyncio.to_thread(self._load_file)

        self._speed_multiplier = self._clamp_speed(speed_multiplier)

        # Snap start_seconds to a chunk boundary; floor so we never start
        # later than requested.
        if self._mp3_chunk_size <= 0 or self._bytes_per_sec <= 0:
            start_byte = 0
        else:
            requested_byte = int(max(0.0, start_seconds) * self._bytes_per_sec)
            start_byte = (requested_byte // self._mp3_chunk_size) * \
                self._mp3_chunk_size
            if start_byte >= self._file_size:
                logger.info(
                    "stream(start_seconds=%.2f) past EOF (%.2fs); nothing to yield",
                    start_seconds, self._duration_seconds,
                )
                return

        total_chunks = (self._file_size + self._mp3_chunk_size -
                        1) // max(self._mp3_chunk_size, 1)
        remaining = total_chunks - (start_byte // max(self._mp3_chunk_size, 1))
        logger.info(
            "Streaming %d MP3 chunks from %.2fs at %.1fx real-time",
            remaining,
            start_byte / max(self._bytes_per_sec, 1),
            self._speed_multiplier,
        )

        self._pacing_anchor_audio_seconds = (
            start_byte / max(self._bytes_per_sec, 1)
        )
        self._pacing_anchor_wall_seconds = start_time

        for i in range(start_byte, self._file_size, self._mp3_chunk_size):
            if self.should_stop():
                logger.info("Stopping MP3 stream — stop requested")
                return

            chunk = self._mp3_data[i: i + self._mp3_chunk_size]
            chunk_num = i // max(self._mp3_chunk_size, 1)

            # Schedule against the speed multiplier. This is the only
            # place we sleep.
            chunk_audio_seconds = i / self._bytes_per_sec
            audio_delta = (
                chunk_audio_seconds - self._pacing_anchor_audio_seconds
            )
            expected_wall_time = (
                self._pacing_anchor_wall_seconds
                + audio_delta / self._speed_multiplier
            )
            now = time.time()
            if now < expected_wall_time:
                await asyncio.sleep(expected_wall_time - now)

            yield chunk

            if chunk_num > 0 and chunk_num % 100 == 0:
                logger.debug(
                    LogMessages.AUDIO_CHUNK_PROGRESS.format(
                        elapsed=i / max(self._bytes_per_sec, 1),
                        current=chunk_num,
                        total=total_chunks,
                    )
                )

        logger.info("MP3 streaming complete")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        self._mp3_data = None
        self._file_size = 0
        self._duration_seconds = 0.0
        self._mp3_chunk_size = 0
        self._bytes_per_sec = MONITOR_DEFAULT_MP3_BITRATE_BPS
        logger.debug("Closed S3 audio source: %s", self.file_key)


class S3AudioSourceFactory:
    """Build :class:`S3AudioSource` instances from configuration."""

    def __init__(self) -> None:
        self.s3 = AWSClients.s3()
        self.bucket = S3_BUCKET

    def get_files_from_config(self, test_files: list[str]) -> list[str]:
        files: list[str] = []
        for test_file in test_files:
            if test_file.endswith("/"):
                response = self.s3.list_objects_v2(
                    Bucket=self.bucket, Prefix=test_file)
                for obj in response.get("Contents", []):
                    if obj["Key"].endswith(".mp3"):
                        files.append(obj["Key"])
            else:
                files.append(test_file)
        return sorted(files)

    def create_source(
        self,
        file_key: str,
        feed_name: str | None = None,
    ) -> S3AudioSource:
        """Create an :class:`S3AudioSource` for ``file_key``.

        Pacing is per-stream, not per-instance: callers select the
        multiplier on each ``stream()`` invocation.
        """
        name = feed_name or file_key.split("/")[-1]
        return S3AudioSource(file_key=file_key, feed_name=name)
