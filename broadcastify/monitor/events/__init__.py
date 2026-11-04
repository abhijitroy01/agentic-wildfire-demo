"""
Event system for Broadcastify Monitor.

Separates the core monitoring logic from its output: the WebSocket
handler streams monitor events to the UI.
"""

from .event_handler import EventHandler
from .event_types import EventType, MonitorEvent

__all__ = [
    "EventHandler",
    "EventType",
    "MonitorEvent",
]
