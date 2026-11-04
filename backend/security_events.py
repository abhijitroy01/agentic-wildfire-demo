"""Security event log for the workspace API: one JSON line per access decision or audited agent action.

A line names the event and its outcome (allowed or denied); for a refusal, the stable error code, HTTP
status and log-only reason; and the route template, the caller's owner digest and the CloudFront or load
balancer request ID. It never carries tokens, cookies, prompts, addresses, request bodies or raw paths.
main.py, the API entry point, writes the lines to the container log (CloudWatch); find them by their
security_event field.
"""
import json
import logging
import re

from starlette.requests import HTTPConnection
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import digest

logger = logging.getLogger(__name__)
# Refusals that record an access or abuse decision: authentication (401), authorization and same-origin
# writes (403), ownership and unknown IDs (404, which never tells a caller whether another owner's record
# exists) and per-session, per-network and daily limits (429).
REFUSED_STATUSES = frozenset({401, 403, 404, 429})
# CloudFront's request ID (the x-edge-request-id of its access logs), else the load balancer's trace ID,
# which requests from the Gateway adapter carry.
REQUEST_ID_HEADERS = ("x-amz-cf-id", "x-amzn-trace-id")
REQUEST_ID = re.compile(r"[A-Za-z0-9+/=;:._-]{1,200}")


def configure_logging() -> None:
    """Write security events at INFO and above to stderr as bare JSON lines; the API entry point calls this.

    uvicorn configures only its own loggers, so without this handler the INFO events would be dropped.
    """
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def bind_owner(connection: HTTPConnection, owner: str) -> None:
    """Name the caller in this request's security events by the owner's digest, never the owner or cookie."""
    connection.state.security_owner = digest(owner)


def allowed(event: str, connection: HTTPConnection, **fields) -> None:
    """Log an allowed security event at INFO, for example a new demo session or an agent tool call."""
    emit(logging.INFO, {"security_event": event, "outcome": "allowed", **fields}, connection)


def refused(connection: HTTPConnection, exc: IntegrationError) -> None:
    """Log a 401, 403, 404 or 429 IntegrationError as a denied security event at WARNING; skip the rest."""
    if exc.status in REFUSED_STATUSES:
        emit(logging.WARNING, {"security_event": "request_refused", "outcome": "denied", "code": exc.code,
                               "status": exc.status, "reason": exc.reason}, connection)


def request_id(connection: HTTPConnection) -> str | None:
    """Return the CloudFront or load balancer request ID that ties an event to the edge logs, if valid."""
    for header in REQUEST_ID_HEADERS:
        value = connection.headers.get(header, "")
        if REQUEST_ID.fullmatch(value):
            return value
    return None


def emit(level: int, event: dict, connection: HTTPConnection) -> None:
    """Add the method, route template, owner digest and request ID to a security event; log it as JSON."""
    if not logger.isEnabledFor(level):
        return
    route = connection.scope.get("route")
    event.update(method=connection.scope.get("method", "WEBSOCKET"), route=getattr(route, "path", None),
                 owner_digest=getattr(connection.state, "security_owner", None),
                 request_id=request_id(connection))
    logger.log(level, "%s", json.dumps({k: v for k, v in event.items() if v is not None}, sort_keys=True))
