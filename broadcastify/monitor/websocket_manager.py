"""
WebSocket Connection Manager

Manages the radio investigation's WebSocket connections.
Tracks active connections by ``session_id``, sends JSON events, and
handles graceful disconnect.
"""

import asyncio
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from broadcastify.monitor.diagnostics import log_failure

logger = logging.getLogger(__name__)


class WebSocketManager:
    """Per-session WebSocket connection registry.

    Threadsafe via an asyncio Lock around the connection dict.
    """

    def __init__(self) -> None:
        self._connections: dict[str, WebSocket] = {}
        self._lock = asyncio.Lock()

    async def connect(
        self, session_id: str, websocket: WebSocket, already_accepted: bool = False
    ) -> bool:
        """Accept and register a WebSocket connection.

        Args:
            session_id: Unique session identifier.
            websocket: The WebSocket connection.
            already_accepted: When True, skip ``accept()`` because the
                handler already called it. The investigation checks the key
                first, accepts the socket itself, then registers it here.

        Returns:
            True if registration succeeded, False if accept() raised.
        """
        try:
            if not already_accepted:
                await websocket.accept()
            async with self._lock:
                self._connections[session_id] = websocket
            logger.info("WebSocket connected: %s", session_id)
            return True
        except WebSocketDisconnect:
            return False
        except Exception as exc:  # noqa: BLE001 — refuse registration when socket acceptance fails
            log_failure(logger, "websocket_accept", exc, session_id=session_id)
            return False

    async def disconnect(self, session_id: str) -> None:
        """Remove a WebSocket connection (idempotent)."""
        async with self._lock:
            if session_id in self._connections:
                del self._connections[session_id]
                logger.info("WebSocket disconnected: %s", session_id)

    async def send_event(self, session_id: str, event: dict[str, Any]) -> bool:
        """Send a JSON event to a specific session.

        On disconnect or send error, the session is dropped from the
        registry and the method returns False so the caller can decide
        whether to abort the loop or just skip this event.
        """
        websocket = self._connections.get(session_id)
        if not websocket:
            return False

        try:
            await websocket.send_json(event)
            return True
        except WebSocketDisconnect:
            await self.disconnect(session_id)
            return False
        except Exception as e:  # noqa: BLE001 -- a failed send drops only this session's socket; caller sees False
            log_failure(logger, "websocket_send", e, session_id=session_id)
            await self.disconnect(session_id)
            return False
