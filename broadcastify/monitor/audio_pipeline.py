"""
Audio Pipeline Module

FFmpeg-based audio conversion pipeline for converting MP3 to PCM.
Designed for use with Amazon Transcribe real-time streaming.
"""

import asyncio
import asyncio.subprocess as asp
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from broadcastify.monitor.audio_sources.base import AudioSource
from broadcastify.monitor.constants import MEDIA_INPUT_OPTIONS, SAMPLE_RATE, calculate_pcm_chunk_size
from broadcastify.monitor.diagnostics import log_failure
from broadcastify.shared.config import PCM_CHUNK_MS
from broadcastify.shared.logger import LogMessages

logger = logging.getLogger(__name__)


class FFmpegPipeline:
    """
    Converts MP3 audio stream to PCM using FFmpeg subprocess.

    PCM output specs (Amazon Transcribe requirements):
    - Sample rate: 16kHz
    - Channels: mono (1)
    - Format: signed 16-bit little-endian (s16le)
    - Chunk size: configurable via PCM_CHUNK_MS

    The pipeline is a thin shim — it does not impose any pacing of its
    own. Whatever rate the audio source yields MP3 at is the rate FFmpeg
    gets fed at, and PCM comes out at the same effective rate. The
    ``speed_multiplier`` argument to :meth:`convert` is forwarded to the
    source unchanged.
    """

    def __init__(
        self,
        stop_check: Callable[[], bool],
        chunk_ms: int = PCM_CHUNK_MS,
    ):
        self.stop_check = stop_check
        self.chunk_size = calculate_pcm_chunk_size(chunk_ms)
        self.ffmpeg_process: asp.Process | None = None
        self._read_task: asyncio.Task | None = None

    async def convert(
        self,
        audio_source: AudioSource,
        stream_start: float,
        *,
        speed_multiplier: float = 1.0,
        on_progress: Callable[[int, float], Awaitable[None]] | None = None,
        start_seconds: float = 0.0,
    ) -> AsyncIterator[bytes]:
        """Convert MP3 audio stream to PCM chunks.

        Args:
            audio_source: AudioSource instance providing MP3 data.
            stream_start: Unix timestamp when streaming started — wall-clock
                anchor for the source's pacing math and for progress logs.
            speed_multiplier: Forwarded to the source. Replay sources honor
                it (clamped to their safe upper bound); live sources ignore
                it. ``1.0`` is real-time.
            on_progress: Optional callback ``(chunk_count, elapsed_time)``.
            start_seconds: Where in the *media* to begin. Forwarded to the
                source.
        """
        # Start FFmpeg subprocess: a fixed argument list with no shell; audio arrives on stdin and
        # MEDIA_INPUT_OPTIONS stops the media from choosing a protocol or demuxer.
        logger.info(LogMessages.FFMPEG_STARTING)
        self.ffmpeg_process = await asp.create_subprocess_exec(  # nosemgrep: python.lang.security.audit.dangerous-asyncio-create-exec-audit
            "ffmpeg",
            *MEDIA_INPUT_OPTIONS,
            "-i", "pipe:0",           # Input from stdin (MP3)
            "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-f", "s16le",            # Output format: PCM
            "-acodec", "pcm_s16le",   # Codec: signed 16-bit little-endian
            "-ar", str(SAMPLE_RATE),  # Sample rate: 16kHz
            "-ac", "1",               # Channels: mono
            "-loglevel", "error",     # Only log errors
            "pipe:1",                 # Output to stdout
            stdin=asp.PIPE,
            stdout=asp.PIPE,
            stderr=asp.DEVNULL,
        )
        logger.info(LogMessages.FFMPEG_READY)

        # Track PCM buffer and progress
        pcm_buffer = b""
        pcm_chunks_sent = 0

        # Task to read PCM from FFmpeg stdout
        async def read_pcm():
            """Background task to continuously read PCM from FFmpeg."""
            nonlocal pcm_buffer
            while not self.stop_check():
                try:
                    data = await asyncio.wait_for(
                        self.ffmpeg_process.stdout.read(8192), timeout=0.5
                    )
                    if not data:
                        break
                    pcm_buffer += data
                except TimeoutError:
                    continue
                except Exception:  # The caller must observe decoder failure, rather than successful empty PCM.
                    if not self.stop_check():
                        raise
                    break

        # Start background PCM reader
        self._read_task = asyncio.create_task(read_pcm())

        # Feed MP3 to FFmpeg stdin and yield PCM chunks. A failure here propagates to the caller unlogged:
        # the investigation's run_monitor logs it once, with its traceback.
        async for mp3_chunk in audio_source.stream(
            stream_start,
            speed_multiplier=speed_multiplier,
            start_seconds=start_seconds,
        ):
            if self.stop_check():
                logger.info(LogMessages.STOP_REQUESTED)
                break
            if self._read_task.done():
                self._read_task.result()

            # Write MP3 to FFmpeg
            self.ffmpeg_process.stdin.write(mp3_chunk)
            await self.ffmpeg_process.stdin.drain()

            # Yield available PCM chunks
            while len(pcm_buffer) >= self.chunk_size:
                if self.stop_check():
                    break

                chunk = pcm_buffer[:self.chunk_size]
                pcm_buffer = pcm_buffer[self.chunk_size:]
                pcm_chunks_sent += 1

                # Progress logging - every 100 chunks (~10 seconds of audio)
                if pcm_chunks_sent % 100 == 0:
                    elapsed = time.time() - stream_start
                    logger.info("[%.1fs] Progress: %d PCM chunks sent", elapsed, pcm_chunks_sent)

                    # Call progress callback if provided
                    if on_progress:
                        await on_progress(pcm_chunks_sent, elapsed)

                yield chunk

            # Let event loop process
            await asyncio.sleep(0)

        # Signal end of input to FFmpeg
        if not self.stop_check():
            self.ffmpeg_process.stdin.close()
            await self.ffmpeg_process.stdin.wait_closed()
            await self._read_task
            if await self.ffmpeg_process.wait() != 0:
                raise RuntimeError("Audio decoder could not complete")

            # Yield remaining PCM chunks
            while len(pcm_buffer) >= self.chunk_size:
                chunk = pcm_buffer[:self.chunk_size]
                pcm_buffer = pcm_buffer[self.chunk_size:]
                yield chunk

            # Yield final partial chunk if any
            if pcm_buffer:
                yield pcm_buffer

            elapsed = time.time() - stream_start
            logger.info("PCM streaming complete: %d chunks in %.1fs", pcm_chunks_sent, elapsed)
        else:
            # Stop requested - cancel the read task
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

    async def cleanup(self) -> None:
        """
        Clean up FFmpeg subprocess and resources.

        This method is idempotent - safe to call multiple times.
        Should be called after convert() completes or on error.
        """
        if self.ffmpeg_process is None:
            return

        # Cancel read task if still running
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        # Kill FFmpeg process
        try:
            self.ffmpeg_process.kill()
            try:
                await asyncio.wait_for(self.ffmpeg_process.wait(), timeout=2.0)
            except TimeoutError:
                logger.warning(
                    "FFmpeg process did not terminate within timeout")
        except Exception as exc:  # noqa: BLE001 — cleanup must not replace the failure being raised
            log_failure(logger, "decoder_cleanup", exc, level=logging.DEBUG)

        # Reset state
        self.ffmpeg_process = None
        self._read_task = None
