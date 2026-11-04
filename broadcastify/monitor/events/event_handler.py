"""
Abstract base class for event handlers.

Event handlers receive events from the monitor and deliver them (the
WebSocket handler streams them to the broker). Only convenience methods
backed by an actual emitter in the codebase are exposed here.
"""

from abc import ABC, abstractmethod

from .event_types import MonitorEvent


class EventHandler(ABC):
    """Abstract base class for handling monitor events."""

    @abstractmethod
    async def handle(self, event: MonitorEvent) -> None:
        """Handle a monitor event."""

    # ---------------------------------------------------------------- log

    async def log(self, level: str, message: str) -> None:
        await self.handle(MonitorEvent.log(level, message))

    # ---------------------------------------------------------- step progress

    async def step_start(
        self, step: int, name: str, description: str = ""
    ) -> None:
        await self.handle(MonitorEvent.step_start(step, name, description))

    async def step_complete(
        self, step: int, name: str, success: bool = True
    ) -> None:
        await self.handle(MonitorEvent.step_complete(step, name, success))

    # ---------------------------------------------------------------- domain

    async def detection(
        self,
        location: str | None,
        keywords: list,
        confidence: float,
        source_id: str,
        transcript_excerpt: str = "",
    ) -> None:
        await self.handle(
            MonitorEvent.detection(
                location, keywords, confidence, source_id, transcript_excerpt
            )
        )
