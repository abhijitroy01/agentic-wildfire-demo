"""
Audio Constants Module
Technical constants for audio processing that are derived from standard specifications.
These should NOT be in .env as they are technical requirements, not tunable parameters.
"""

import json
import logging
import shutil
import subprocess  # nosec B404 - runs only the fixed ffprobe argument list below, never a shell

logger = logging.getLogger(__name__)

# Amazon Transcribe requirements
SAMPLE_RATE = 16000  # Hz - Required by Amazon Transcribe for streaming
# Audio arrives on stdin from a deployment-controlled source. Never let media
# metadata select a network/file protocol, image demuxer, or playlist.
MEDIA_INPUT_OPTIONS = (
    "-protocol_whitelist", "pipe",
    "-format_whitelist", "mp3,wav,mov,ogg,flac",
)

# The MP3 bitrate is probed per file (probe_mp3_bytes_per_sec()); MONITOR_DEFAULT_MP3_BITRATE_BPS in shared.config
# is the fallback.

# PCM encoding (derived from sample rate)
# 32000 bytes/sec for 16-bit mono (2 bytes per sample)
PCM_BYTES_PER_SEC = SAMPLE_RATE * 2


def calculate_pcm_chunk_size(chunk_ms: int) -> int:
    """
    Calculate PCM chunk size in bytes for a given chunk duration.

    Args:
        chunk_ms: Chunk duration in milliseconds

    Returns:
        Chunk size in bytes
    """
    return int(PCM_BYTES_PER_SEC * chunk_ms / 1000)


def calculate_mp3_chunk_size(chunk_ms: int, bytes_per_sec: int) -> int:
    """
    Calculate MP3 chunk size in bytes for a given chunk duration and bitrate.

    Args:
        chunk_ms: Chunk duration in milliseconds
        bytes_per_sec: Bytes per second for this specific MP3 (use
            probe_mp3_bytes_per_sec() to derive from the file itself)

    Returns:
        Chunk size in bytes
    """
    return int(bytes_per_sec * chunk_ms / 1000)


def probe_mp3_bytes_per_sec(
    mp3_data: bytes,
    fallback_bytes_per_sec: int,
) -> int:
    """
    Probe an MP3 byte buffer with ffprobe to determine the actual bytes/sec.

    Pacing audio at the wrong bitrate causes Transcribe to receive audio at
    N× real-time, which makes transcripts arrive way ahead of (or behind) the
    user's audio playback. We probe each file because Broadcastify dispatch
    feeds are 32 kbps but the codebase historically assumed 128 kbps.

    Strategy:
        1. Pipe the MP3 buffer to ffprobe (no disk write).
        2. Read the format-level bitrate.
        3. Return bytes/sec; fall back to the configured value on any failure.

    Args:
        mp3_data: Raw MP3 bytes
        fallback_bytes_per_sec: Value to return if ffprobe is missing or fails

    Returns:
        Bytes per second of the audio (used to pace streaming)
    """
    if not shutil.which("ffprobe"):
        logger.warning(
            "ffprobe not found on PATH; using fallback bitrate (%d B/s)",
            fallback_bytes_per_sec,
        )
        return fallback_bytes_per_sec

    try:
        # Fixed ffprobe argument list with no shell; the audio bytes arrive on stdin only.
        proc = subprocess.run(  # nosec B603 - fixed argument list, no shell, input on stdin
            [
                "ffprobe",
                "-v", "error",
                *MEDIA_INPUT_OPTIONS,
                "-print_format", "json",
                "-show_entries", "format=bit_rate",
                "-i", "pipe:0",
            ],
            input=mp3_data,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0:
            logger.warning(
                "ffprobe returned %d; using fallback bitrate (%d B/s). stderr=%s",
                proc.returncode, fallback_bytes_per_sec,
                proc.stderr.decode(errors="replace")[:200],
            )
            return fallback_bytes_per_sec

        payload = json.loads(proc.stdout.decode(errors="replace"))
        bit_rate_str = payload.get("format", {}).get("bit_rate")
        if not bit_rate_str:
            logger.warning(
                "ffprobe did not report bit_rate; using fallback (%d B/s)",
                fallback_bytes_per_sec,
            )
            return fallback_bytes_per_sec
        bit_rate = int(bit_rate_str)
        bytes_per_sec = bit_rate // 8
        if bytes_per_sec <= 0:
            return fallback_bytes_per_sec
        return bytes_per_sec
    except (subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as e:
        logger.warning(
            "ffprobe failed (%s); using fallback bitrate (%d B/s)",
            e, fallback_bytes_per_sec,
        )
        return fallback_bytes_per_sec
