"""
Event type definitions for Broadcastify Monitor.

Only the event types and factories actually emitted by the package are
defined here. ``investigation.py`` sends its connected, simulation_audio,
complete and error frames directly via ``websocket.send_json(...)``.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class EventType(Enum):
    """Types of events emitted by the monitor."""

    # Logging events
    LOG = "log"

    # Step progress events (one step per recording)
    STEP_START = "step_start"
    STEP_COMPLETE = "step_complete"

    # Detection events
    DETECTION = "detection"


@dataclass
class MonitorEvent:
    """Event emitted by the monitor.

    Attributes:
        type: The type of event
        data: Event-specific payload
        timestamp: ISO-8601 UTC timestamp string
    """

    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        """Convert event to dictionary for JSON serialization."""
        return {
            "type": self.type.value,
            "data": self.data,
            "timestamp": self.timestamp,
        }

    @classmethod
    def log(cls, level: str, message: str) -> MonitorEvent:
        """Create a log event."""
        return cls(
            type=EventType.LOG,
            data={"level": level, "message": message},
        )

    @classmethod
    def step_start(cls, step: int, name: str, description: str = "") -> MonitorEvent:
        """Create a step start event."""
        return cls(
            type=EventType.STEP_START,
            data={"step": step, "name": name, "description": description},
        )

    @classmethod
    def step_complete(
        cls, step: int, name: str, success: bool = True
    ) -> MonitorEvent:
        """Create a step complete event."""
        return cls(
            type=EventType.STEP_COMPLETE,
            data={"step": step, "name": name, "success": success},
        )

    @classmethod
    def detection(
        cls,
        location: str | None,
        keywords: list,
        confidence: float,
        source_id: str,
        transcript_excerpt: str = "",
    ) -> MonitorEvent:
        """Create a fire detection event."""
        return cls(
            type=EventType.DETECTION,
            data={
                "location": location,
                "keywords": keywords,
                "confidence": confidence,
                "source_id": source_id,
                "transcript_excerpt": transcript_excerpt,
            },
        )
