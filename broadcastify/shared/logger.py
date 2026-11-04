"""
Unified Logging Module for Broadcastify Package.

Modules create their loggers with ``logging.getLogger(__name__)`` and never
add handlers. The service entry point, ``monitor/private.py``, configures
handlers once with ``setup_logging``.
"""

import logging
import re
import sys

# Default log format
DEFAULT_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Track if logging has been set up
_logging_configured = False


def setup_logging(
    level: int = logging.INFO,
    format_string: str = DEFAULT_FORMAT,
    date_format: str = DEFAULT_DATE_FORMAT,
    stream: object | None = None,
) -> None:
    """Configure root logging once at application startup."""
    global _logging_configured

    if _logging_configured:
        return

    if stream is None:
        stream = sys.stdout

    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter(format_string, date_format))

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(handler)

    _logging_configured = True



# =============================================================================
# Sensitive data: transcripts, locations, questions and model answers can carry
# personal information spoken on air, and WebSocket URLs can carry the API key.
# Log their size and ids (text_size), never their text.
# =============================================================================

# An api_key query parameter value inside a logged URL or request line.
_API_KEY_IN_URL = re.compile(r"(?i)(\bapi_key=)[^&\s\"']*")


def text_size(text: str | None) -> str:
    """Describe transcript or location text for a log line by its length only, never its content."""
    return f"{len(text)} characters" if text else "none"


class RedactApiKeyFilter(logging.Filter):
    """Replace api_key query values in a log record's message and arguments with [redacted]."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite the record in place and always keep it."""
        if isinstance(record.msg, str):
            record.msg = _API_KEY_IN_URL.sub(r"\1[redacted]", record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                _API_KEY_IN_URL.sub(r"\1[redacted]", arg) if isinstance(arg, str) else arg
                for arg in record.args)
        return True


def redact_api_keys_in_server_logs() -> None:
    """Install RedactApiKeyFilter on the uvicorn access and error loggers, which log URLs with their query."""
    for name in ("uvicorn.access", "uvicorn.error"):
        server_logger = logging.getLogger(name)
        if not any(isinstance(existing, RedactApiKeyFilter) for existing in server_logger.filters):
            server_logger.addFilter(RedactApiKeyFilter())


# =============================================================================
# Log Message Templates
# =============================================================================

class LogMessages:
    """Centralized log message templates for the radio investigation (sizes, never transcript text)."""

    AUDIO_CHUNK_PROGRESS = "[{elapsed:.1f}s] Audio chunk {current}/{total}"

    TRANSCRIPTION_STARTED = "[{feed}] Transcription started"
    TRANSCRIPTION_STOPPED = "[{feed}] Transcription stopped"
    TRANSCRIPTION_PARTIAL = "[{elapsed:.1f}s] (partial) {size}"
    TRANSCRIPTION_FINAL = "[{elapsed:.1f}s] (final) {size}"

    KEYWORDS_FOUND = "Keywords detected: {keywords}"
    LOCATION_EXTRACTED = "Location extracted via LLM ({size})"
    NO_LOCATION = "No location found in transcript"
    CONFIDENCE_SCORE = "Confidence score: {score:.2f}"
    BELOW_THRESHOLD = "Detection below threshold ({threshold}), skipping"
    DUPLICATE_DETECTION = "Duplicate detection, skipping"

    FIRE_DETECTION = "FIRE DETECTED - Location: {location_size}, Keywords: {keywords}"
    FIRST_DETECTION = "FIRST FIRE DETECTION WITH LOCATION"
    DETECTION_SUMMARY = (
        "Detection Summary - File: {file}, Time: {elapsed:.1f}s, "
        "Feed: {feed}, Location: {location_size}, Keywords: {keywords}, Confidence: {confidence:.2f}"
    )

    STOP_REQUESTED = "Stop requested - detection found"
    PROCESSING_FILE = "Processing file: {file}"
    FILE_COMPLETE = "File processing complete: {file}"

    FFMPEG_STARTING = "Starting FFmpeg subprocess"
    FFMPEG_READY = "FFmpeg ready for audio conversion"

    S3_STREAMING = "Streaming from S3: {bucket}/{key}"
    S3_FILE_INFO = "File size: {size} bytes, Duration: ~{duration:.1f}s"
