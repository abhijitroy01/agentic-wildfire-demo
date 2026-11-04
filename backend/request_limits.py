"""Bound public JSON request ingestion before FastAPI allocates decoded models."""
import asyncio

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

from backend import api_settings as settings
from backend.http_errors import correlation, envelope


class RequestLimits:
    """ASGI middleware: 413 past REQUEST_BODY_MAX_KIB and 408 past REQUEST_BODY_SECONDS for /api/v1/ bodies."""

    def __init__(self, app):
        self.app = app
        # Read once when the application builds its middleware stack (decision D23).
        self.max_bytes = settings.request_body_max_kib() * 1024
        self.seconds = settings.request_body_seconds()

    async def __call__(self, scope, receive, send):
        """Cap /api/v1/ request bodies by size and upload time before the app reads them."""
        if scope["type"] != "http" or not scope["path"].startswith("/api/v1/"):
            return await self.app(scope, receive, send)
        chunks, total = [], 0
        try:
            async with asyncio.timeout(self.seconds):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    total += len(message.get("body", b""))
                    if total > self.max_bytes:
                        response = JSONResponse(envelope("BODY_TOO_LARGE", "Reduce the request size.",
                                                         request_id=correlation(HTTPConnection(scope))),
                                                status_code=413)
                        return await response(scope, receive, send)
                    chunks.append(message.get("body", b""))
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            response = JSONResponse(envelope("REQUEST_TIMEOUT", "Request body timed out.",
                                             request_id=correlation(HTTPConnection(scope))), status_code=408)
            return await response(scope, receive, send)
        body, delivered = b"".join(chunks), False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
