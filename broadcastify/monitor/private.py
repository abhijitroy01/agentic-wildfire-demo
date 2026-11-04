"""Deployment entry point: expose only authenticated radio investigation."""
import os
from urllib.parse import parse_qsl, urlencode

from broadcastify.monitor.auth import key_matches
from broadcastify.monitor.investigation import app as monitor
from broadcastify.shared.logger import setup_logging

BEARER = b"Bearer "
# The deployed entry point configures logging once (stdout at INFO, for CloudWatch); the modules only create
# loggers. uvicorn configures its own loggers.
setup_logging()


async def app(scope, receive, send):
    """Forward only GET /health and the bearer-authenticated monitor WebSocket to the radio monitor API."""
    if scope["type"] == "lifespan":
        return await monitor(scope, receive, send)
    path = scope.get("path")
    if scope["type"] == "http" and path == "/health" and scope.get("method") == "GET":
        return await monitor(scope, receive, send)
    headers = dict(scope.get("headers", []))
    key = os.getenv("API_KEY", "")
    supplied = headers.get(b"authorization", b"")
    authorized = supplied.startswith(BEARER) and key_matches(supplied[len(BEARER):], key)
    if scope["type"] != "websocket":
        await send({"type": "http.response.start", "status": 404, "headers": []})
        return await send({"type": "http.response.body", "body": b""})
    if not authorized or path != "/ws/monitor/start":
        return await send({"type": "websocket.close", "code": 1008})
    query = dict(parse_qsl(scope.get("query_string", b"").decode()))
    if set(query)-{"playback"} or query.get("playback", "false") not in ("true", "false"):
        return await send({"type": "websocket.close", "code": 1008})
    # Compatibility with the existing monitor. Only the outer broker-authenticated
    # connection reaches this adapter; the credential never appears in its URL.
    internal = {**scope, "query_string": urlencode({**query, "api_key": key}).encode()}
    return await monitor(internal, receive, send)
