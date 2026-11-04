"""
Unified Configuration Module for Broadcastify Package.

Single source of truth for the settings of the deployed investigation, read from
the environment when this module is imported.
"""

import logging
import math
import os
from pathlib import Path

from dotenv import load_dotenv

# A local run may keep its settings in broadcastify/.env (copied from .env.example). Only that file is
# read, never a .env found by searching parent folders. The radio image contains no .env file.
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.is_file():
    load_dotenv(_env_file)

_logger = logging.getLogger(__name__)


class Config:
    """
    Unified configuration loader with type-safe helpers.

    Usage:
        from broadcastify.shared.config import Config, S3_BUCKET, TEST_FILES

        # Or use helpers directly
        bucket = Config.get_required("S3_BUCKET")
        chunk_ms = Config.get_int("MONITOR_PCM_CHUNK_MS", 100)
    """

    @staticmethod
    def get_required(key: str) -> str:
        """Get required environment variable. Raises ValueError if not set."""
        value = os.getenv(key)
        if value is None:
            raise ValueError(
                f"Required environment variable '{key}' is not set")
        return value

    @staticmethod
    def get_optional(key: str, default: str | None = None) -> str | None:
        """Get optional environment variable with default."""
        return os.getenv(key, default)

    @staticmethod
    def get_int(key: str, default: int) -> int:
        """Read a whole-number environment variable; unset or blank gives the default, anything else raises."""
        value = os.getenv(key, "").strip()
        if not value:
            return default
        try:
            return int(value)
        except ValueError:
            raise RuntimeError(f"{key} must be a whole number; received {os.getenv(key)!r}.") from None

    @staticmethod
    def get_float(key: str, default: float) -> float:
        """Read a finite-number environment variable; unset or blank gives the default, anything else raises."""
        value = os.getenv(key, "").strip()
        if not value:
            return default
        try:
            number = float(value)
        except ValueError:
            number = math.nan
        if not math.isfinite(number):
            raise RuntimeError(f"{key} must be a finite number; received {os.getenv(key)!r}.")
        return number

    @staticmethod
    def get_list(key: str, default: list[str] | None = None, lowercase: bool = False) -> list[str]:
        """Get comma-separated list from environment variable."""
        if default is None:
            default = []
        value = os.getenv(key)
        if value is None:
            return default
        items = [item.strip() for item in value.split(",")]
        return [item.lower() for item in items] if lowercase else items


# =============================================================================
# AWS Configuration (Shared)
# =============================================================================
AWS_REGION = Config.get_required("AWS_REGION")
S3_BUCKET = Config.get_required("S3_BUCKET")

# =============================================================================
# Monitor Configuration
# =============================================================================
MONITOR_BEDROCK_MODEL_ID = Config.get_required("MONITOR_BEDROCK_MODEL_ID")
PCM_CHUNK_MS = Config.get_int("MONITOR_PCM_CHUNK_MS", 100)
# Fallback bitrate (bps) used by S3AudioSource when ffprobe cannot determine the
# real bitrate of the source MP3. Should match the most common bitrate of your
# feeds. 32000 = Broadcastify dispatch (32 kbps mono); 128000 = music-quality MP3.
MONITOR_DEFAULT_MP3_BITRATE_BPS = Config.get_int(
    "MONITOR_DEFAULT_MP3_BITRATE_BPS", 32000)
TRANSCRIPT_WINDOW_SECONDS = Config.get_int(
    "MONITOR_TRANSCRIPT_WINDOW_SECONDS", 15)
DEDUP_WINDOW_MINUTES = Config.get_int("MONITOR_DEDUP_WINDOW_MINUTES", 5)
LOCATION_CACHE_SIZE = Config.get_int("MONITOR_LOCATION_CACHE_SIZE", 100)
CONFIDENCE_THRESHOLD = Config.get_float("MONITOR_CONFIDENCE_THRESHOLD", 0.5)

# Fire Detection Keywords
FIRE_KEYWORDS = Config.get_list("FIRE_KEYWORDS", [], lowercase=True)
EXCLUSION_PATTERNS = Config.get_list("EXCLUSION_PATTERNS", [], lowercase=True)

# Recordings the investigation replays: S3 keys, or prefixes ending in "/" for every MP3 under them.
TEST_FILES = Config.get_list("MONITOR_TEST_FILES", [])
# How many times real time the investigation replays its recordings, so a long clip detects in seconds.
# The S3 source caps it at MAX_REPLAY_SPEED (4) so Amazon Transcribe streaming keeps up. Default: 3.
RADIO_REPLAY_SPEED = Config.get_float("RADIO_REPLAY_SPEED", 3.0)

# =============================================================================
# Transcription Configuration
# =============================================================================
TRANSCRIBE_LANGUAGE_CODE = Config.get_optional(
    "TRANSCRIBE_LANGUAGE_CODE", "en-US")

# Custom Vocabulary for domain accuracy.
# Set TRANSCRIBE_VOCABULARY_NAME to the name of a custom vocabulary you've
# created in this account/region (via `aws transcribe create-vocabulary`).
# When set, the streaming transcription passes it to Transcribe so domain
# jargon (Squad/Battalion/Code 3/incident codes/street names) is recognized
# correctly. Leave unset to use the base model only.
TRANSCRIBE_VOCABULARY_NAME = Config.get_optional(
    "TRANSCRIBE_VOCABULARY_NAME", None)

# Streaming-only: partial results stabilization (high/medium/low). "high"
# (Transcribe default) optimizes for stability so partials rarely change;
# "medium" trades a little stability for accuracy and is the typical sweet
# spot for noisy radio audio. "low" prioritizes accuracy but can thrash
# visibly during static. Disabled (=None) means Transcribe uses its default.
TRANSCRIBE_PARTIAL_STABILITY = Config.get_optional(
    "TRANSCRIBE_PARTIAL_STABILITY", "medium")

# =============================================================================
# Monitor LLM Configuration (for keyword_detector.py)
# =============================================================================
# Location extraction needs only a short answer
MONITOR_MODEL_MAX_TOKENS = Config.get_int("MONITOR_MODEL_MAX_TOKENS", 512)
MONITOR_MODEL_TEMPERATURE = Config.get_float("MONITOR_MODEL_TEMPERATURE", 0.0)

# =============================================================================
# Recording Playback
# =============================================================================
PRESIGNED_URL_EXPIRY_SECONDS = Config.get_int(
    "PRESIGNED_URL_EXPIRY_SECONDS", 3600)


def log_config() -> None:
    """Log the investigation's main settings at startup; the values were already parsed and checked at import."""
    _logger.info("Configuration loaded: AWS_REGION %s, S3_BUCKET %s, %d fire keywords, %d exclusion patterns, "
                 "recordings %s, replay speed %sx", AWS_REGION, S3_BUCKET, len(FIRE_KEYWORDS),
                 len(EXCLUSION_PATTERNS), TEST_FILES, RADIO_REPLAY_SPEED)
