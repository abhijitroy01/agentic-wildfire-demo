"""
Keyword Detector
Detects fire-related keywords in transcripts and extracts location using Bedrock LLM.
"""

import hashlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta

from broadcastify.shared.aws_clients import AWSClients
from broadcastify.shared.config import (
    CONFIDENCE_THRESHOLD,
    DEDUP_WINDOW_MINUTES,
    EXCLUSION_PATTERNS,
    FIRE_KEYWORDS,
    LOCATION_CACHE_SIZE,
    MONITOR_BEDROCK_MODEL_ID,
    MONITOR_MODEL_MAX_TOKENS,
    MONITOR_MODEL_TEMPERATURE,
)
from broadcastify.shared.logger import LogMessages, text_size

logger = logging.getLogger(__name__)
_REFUSED = object()
_INCOMPLETE = object()


class AddressExtractionRefused(RuntimeError):
    """The model declined this segment; it supplied no location evidence."""


class AddressExtractionIncomplete(RuntimeError):
    """A street number without a street name cannot identify an incident."""


class KeywordDetector:
    """Detects fire keywords and extracts locations from transcripts using LLM."""

    def __init__(self):
        self.recent_detections = []  # For deduplication
        self.location_cache = {}  # Cache LLM results
        self.bedrock = AWSClients.bedrock()
        self.model_id = MONITOR_BEDROCK_MODEL_ID
        logger.info("Bedrock LLM initialized: %s", self.model_id)

    def detect(self, transcript_text: str, feed_name: str = "Unknown") -> dict:
        """
        Analyze transcript for fire keywords and extract location via LLM.

        Blocks on a synchronous Bedrock call, so async callers run it in a
        worker thread (``asyncio.to_thread``), one call at a time, as
        ``POCMonitor`` does; the detector's caches are not thread-safe.

        Args:
            transcript_text: Transcript text to analyze
            feed_name: Name of the feed source

        Returns:
            Detection dict or None if no detection
        """
        text_lower = transcript_text.lower()

        # Check for exclusions first
        if self._has_exclusions(text_lower):
            return None

        # Find fire keywords
        keywords_found = self._find_keywords(text_lower)
        if not keywords_found:
            return None

        logger.info(LogMessages.KEYWORDS_FOUND.format(keywords=keywords_found))

        # Extract location using LLM
        location = self._extract_location_llm(transcript_text)
        if location:
            logger.info(LogMessages.LOCATION_EXTRACTED.format(
                size=text_size(location)))
        else:
            logger.debug(LogMessages.NO_LOCATION)

        # Calculate confidence
        confidence = self._calculate_confidence(
            keywords_found, location, transcript_text
        )
        logger.info(LogMessages.CONFIDENCE_SCORE.format(score=confidence))

        # Filter low confidence
        if confidence < CONFIDENCE_THRESHOLD:
            logger.debug(
                LogMessages.BELOW_THRESHOLD.format(
                    threshold=CONFIDENCE_THRESHOLD)
            )
            return None

        # Check for duplicates
        if self._is_duplicate(location, keywords_found):
            logger.debug(LogMessages.DUPLICATE_DETECTION)
            return None

        # Create detection event
        detection = {
            "timestamp": datetime.now(UTC).isoformat(),
            "keywords": keywords_found,
            "location": location,
            "confidence": confidence,
            "transcript": transcript_text,
            "feed_name": feed_name,
        }

        # Store for deduplication
        self.recent_detections.append(detection)
        self._cleanup_old_detections()

        return detection

    def _find_keywords(self, text: str) -> list:
        """Find all fire keywords in text."""
        found = []
        for keyword in FIRE_KEYWORDS:
            if keyword in text:
                found.append(keyword)
        return found

    def _extract_location_llm(self, transcript: str) -> str:
        """Extract location from text using Amazon Bedrock LLM.

        Bedrock failures are NOT swallowed: we log the error and let the
        exception propagate so the caller (and the websocket handler
        above it) surfaces a real error to the operator instead of a
        "successful" detection with ``location=None``. A null location
        is reserved for the case where the LLM ran successfully and
        determined no fire location was present.
        """
        # Check cache first
        # Partial transcripts can share a prefix while adding the actual address.
        # A prefix-only cache can retain NONE after the address becomes available.
        cache_key = hashlib.sha256(transcript.encode()).hexdigest()
        if cache_key in self.location_cache:
            cached = self.location_cache[cache_key]
            if cached is _REFUSED:
                raise AddressExtractionRefused("Model declined address extraction for this segment")
            if cached is _INCOMPLETE:
                raise AddressExtractionIncomplete("Waiting for a street name")
            return cached

        # Prompt for location extraction
        prompt = f"""You are analyzing emergency radio traffic for fire incidents. Extract ONLY the fire/emergency location from this transcript.

Transcript: "{transcript}"

Rules:
- Return ONLY the location/address as a simple string
- Include street numbers, street names, and any identifiers
- When the speaker spells a name letter by letter, use that spelling instead of the phonetic transcription. For example, "Maine Street, M A I N" means "Main Street"
- A street number with only words such as "behind", "at", or "location" is incomplete; return only that number so monitoring can wait for the street name. Do not invent a street name
- If multiple locations, return the most specific one related to a fire
- If no clear fire location is mentioned, return exactly: NONE
- Do not include explanations, just the location

Examples:
- "1234 Example Lane"
- "Highway 101 near milepost 45"
- "500 block of Main Street"
- "NONE"

Location:"""

        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": MONITOR_MODEL_MAX_TOKENS,
            "temperature": MONITOR_MODEL_TEMPERATURE,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
        })

        # Provider failures propagate to the investigation's safe cause diagnostics; messages may contain audio text.
        response = self.bedrock.invoke_model(
            modelId=self.model_id,
            body=body,
        )

        stream = response["body"]
        try:
            response_body = json.loads(stream.read())
        finally:
            stream.close()
        blocks = response_body.get("content", [])
        if response_body.get("stop_reason") == "refusal":
            self._cache_location(cache_key, _REFUSED)
            raise AddressExtractionRefused("Model declined address extraction for this segment")
        if response_body.get("stop_reason") != "end_turn" or not isinstance(blocks, list):
            raise RuntimeError("Address extraction did not return a complete model response")
        location = "\n".join(
            block["text"] for block in blocks
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
        ).strip()
        if not location or len(location) > 500:
            raise RuntimeError("Address extraction returned no usable model text")

        # Normalize response
        if location.upper() == "NONE":
            location = None
        elif not any(character.isalpha() for character in re.sub(
                r"\b(?:behind|at|near|the|location|of|address|number|in|front|rear|block)\b",
                "", location, flags=re.IGNORECASE)):
            self._cache_location(cache_key, _INCOMPLETE)
            raise AddressExtractionIncomplete("Waiting for a street name")

        self._cache_location(cache_key, location)
        return location

    def _cache_location(self, cache_key, location):
        self.location_cache[cache_key] = location
        if len(self.location_cache) > LOCATION_CACHE_SIZE:
            keys_to_remove = list(self.location_cache.keys())[
                : LOCATION_CACHE_SIZE // 2
            ]
            for k in keys_to_remove:
                del self.location_cache[k]

    def _has_exclusions(self, text: str) -> bool:
        """Check if text contains exclusion patterns."""
        for exclusion in EXCLUSION_PATTERNS:
            if exclusion in text:
                return True
        return False

    def _calculate_confidence(
        self, keywords: list, location: str, transcript: str
    ) -> float:
        """
        Calculate confidence score (0.0 to 1.0).

        Factors:
        - Number of keywords (more = higher)
        - Location present (yes = much higher with LLM)
        - Transcript length (longer = more context)
        """
        score = 0.0

        # Keyword score (0.0 to 0.4)
        keyword_score = min(len(keywords) * 0.15, 0.4)

        # Location score (0.5 if present - higher because LLM is more accurate)
        location_score = 0.5 if location else 0.0

        # Context score (0.1 if transcript has good length)
        context_score = 0.1 if len(transcript) > 50 else 0.0

        score = keyword_score + location_score + context_score

        return min(score, 1.0)

    def _is_duplicate(self, location: str, keywords: list) -> bool:
        """Check if similar detection in the deduplication window."""
        if not location:
            return False

        cutoff_time = datetime.now(UTC) - timedelta(minutes=DEDUP_WINDOW_MINUTES)

        for detection in self.recent_detections:
            detection_time = datetime.fromisoformat(detection["timestamp"])

            if detection_time < cutoff_time:
                continue

            # Same location?
            if detection.get("location") == location:
                return True

        return False

    def _cleanup_old_detections(self):
        """Remove detections older than the deduplication window."""
        cutoff_time = datetime.now(UTC) - timedelta(minutes=DEDUP_WINDOW_MINUTES)

        self.recent_detections = [
            d for d in self.recent_detections
            if datetime.fromisoformat(d["timestamp"]) > cutoff_time
        ]
