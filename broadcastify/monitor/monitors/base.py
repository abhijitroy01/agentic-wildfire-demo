"""
Base Monitor Module
Abstract base class for all monitor implementations.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class DetectionEvent:
    """Represents a fire detection event."""

    timestamp: str  # ISO format timestamp
    keywords: list[str]  # Keywords that triggered the detection
    location: str | None  # Extracted location (if any)
    confidence: float  # Confidence score (0.0 to 1.0)
    transcript: str  # Full transcript text
    feed_name: str  # Name of the audio feed
    elapsed_time: float = 0.0  # Time into the audio when detected
    source_id: str | None = None  # Source identifier (file key, feed ID)
    metadata: dict[str, Any] = field(
        default_factory=dict)  # Additional metadata


@dataclass
class MonitorResult:
    """Result of a monitoring session."""

    success: bool  # Whether monitoring completed successfully
    mode: str  # 'poc', the recorded-audio investigation
    start_time: datetime  # When monitoring started
    end_time: datetime | None = None  # When monitoring ended
    total_detections: int = 0  # Total number of detections
    # First detection (if any)
    first_detection: DetectionEvent | None = None
    all_detections: list[DetectionEvent] = field(default_factory=list)
    sources_processed: int = 0  # Number of audio sources processed
    error_message: str | None = None  # Error message if failed
    # The exception that failed the run, so its caller can raise from it and log the root cause once.
    error: Exception | None = field(default=None, repr=False, compare=False)


class BaseMonitor(ABC):
    """
    Abstract base class for monitoring behavior.

    Monitors orchestrate the full pipeline:
    - Audio source streaming
    - FFmpeg MP3 → PCM conversion
    - Amazon Transcribe streaming
    - Keyword detection
    - Event handling

    Subclasses implement specific behaviors:
    - POCMonitor: Stops after first detection with location
    """

    def __init__(self):
        self._stop_requested = False
        self._result: MonitorResult | None = None

    @abstractmethod
    async def run(self) -> MonitorResult:
        """
        Main monitoring loop.

        Implementations should:
        1. Initialize audio sources
        2. Process each source through the pipeline
        3. Call on_detection() for each detection
        4. Check should_stop() for early termination
        5. Return MonitorResult when complete

        Returns:
            MonitorResult with monitoring summary
        """

    @abstractmethod
    def on_detection(self, detection: DetectionEvent) -> None:
        """
        Handle a fire detection event.

        Called whenever keyword detection finds a match.

        Implementations can:
        - Log the detection
        - Store for reporting
        - Trigger external notifications
        - Set stop flag for early termination

        Args:
            detection: The detection event details
        """

    @abstractmethod
    def should_stop(self) -> bool:
        """
        Check if monitoring should stop.

        Called during the monitoring loop.

        Returns:
            True if monitoring should stop early
        """

    def request_stop(self) -> None:
        """
        Request early termination of monitoring.

        Can be called externally (e.g., signal handler, API request).
        """
        self._stop_requested = True
