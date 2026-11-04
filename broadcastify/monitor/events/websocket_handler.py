"""
WebSocket event handler for UI integration.

Streams events over the investigation's WebSocket connection.
Used by investigation.py, whose events the workspace API relays to the UI.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING

from .event_handler import EventHandler
from .event_types import MonitorEvent

if TYPE_CHECKING:
    from ..websocket_manager import WebSocketManager


class WebSocketEventHandler(EventHandler):
    """
    Event handler that streams events via WebSocket.

    Used by the investigation to send real-time updates to the
    connected workspace API relay.
    """

    def __init__(self, ws_manager: WebSocketManager, session_id: str,
                 on_disconnect: Callable[[], None] | None = None):
        """
        Initialize WebSocket handler.

        Args:
            ws_manager: WebSocket connection manager
            session_id: Session ID for this monitoring session
        """
        self.ws_manager = ws_manager
        self.session_id = session_id
        self.on_disconnect = on_disconnect
        self.disconnected = False

    async def handle(self, event: MonitorEvent) -> None:
        """
        Handle an event by sending it via WebSocket.

        Args:
            event: The event to send
        """
        if not await self.ws_manager.send_event(self.session_id, event.to_dict()):
            self.disconnected = True
            if self.on_disconnect:
                self.on_disconnect()
