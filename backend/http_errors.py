import json
import logging
import traceback
from uuid import uuid4

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.requests import HTTPConnection
from traffic_simulator.errors import IntegrationError

from backend import security_events

logger = logging.getLogger(__name__)


def correlation(connection):
    if not getattr(connection.state, "api_request_id", None):
        connection.state.api_request_id = str(uuid4())
    return connection.state.api_request_id


def operational_failure(connection, exc, code, status):
    """Record safe diagnostic context once at the HTTP boundary, without exception messages or locals."""
    route = connection.scope.get("route")
    frames = traceback.extract_tb(exc.__traceback__)
    event = {"operational_event": "request_failed", "code": code, "status": status,
             "request_id": correlation(connection), "edge_request_id": security_events.request_id(connection),
             "method": connection.scope.get("method"), "route": getattr(route, "path", None),
             "exception_type": type(exc).__name__,
             "frames": [{"file": frame.filename.rsplit("/", 1)[-1], "line": frame.lineno,
                         "function": frame.name} for frame in frames[-8:]]}
    logger.warning("%s", json.dumps({k: v for k, v in event.items() if v is not None}, sort_keys=True))


class ErrorBoundary:
    """Bind a correlation ID before ingestion, and render unhandled failures without raw server tracebacks."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/v1/"):
            return await self.app(scope, receive, send)
        connection = HTTPConnection(scope)
        request_id = correlation(connection)
        started = False

        async def response_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message["headers"] = [*message.get("headers", []), (b"x-request-id", request_id.encode())]
            await send(message)

        try:
            await self.app(scope, receive, response_send)
        except Exception as exc:  # noqa: BLE001 — stable boundary; raw exception text can contain credentials
            operational_failure(connection, exc, "INTERNAL_ERROR", 500)
            if started:
                # HTTP status cannot change after streaming starts. Terminate the incomplete stream;
                # consumers require a terminal event and report interruption.
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                return
            response = JSONResponse(envelope("INTERNAL_ERROR", "The request could not complete.",
                                             request_id=request_id), status_code=500)
            await response(scope, receive, response_send)


def envelope(code, message, *, details=None, retryable=False, request_id=None):
    return {"error": {"code": code, "message": message, "retryable": retryable,
                      "request_id": request_id, "details": details or {}}}


def install_error_handlers(app):
    """Render API errors as the v1 envelope and log access and limit refusals as security events."""
    app.add_middleware(ErrorBoundary)

    @app.exception_handler(ValidationError)
    async def domain_validation_error(request: Request, exc: ValidationError):
        return JSONResponse(status_code=422, content=envelope(
            "VALIDATION_ERROR", "Simulator arguments do not match the supported contract.",
            request_id=correlation(request),
            details={"violations": [{"location": list(e["loc"]), "type": e["type"]} for e in exc.errors()]}))

    @app.exception_handler(IntegrationError)
    async def integration_error(request: Request, exc: IntegrationError):
        # Authentication, authorization, ownership and limit refusals are security events (server log only).
        security_events.refused(request, exc)
        if exc.status >= 500:
            operational_failure(request, exc, exc.code, exc.status)
        # Busy and over-budget answers tell the caller when to retry (seconds), as WAF's own 429 does.
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        return JSONResponse(status_code=exc.status, headers=headers,
                            content=envelope(exc.code, exc.message, retryable=exc.retryable,
                                             request_id=correlation(request)))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if not request.url.path.startswith("/api/v1/"):
            return await request_validation_exception_handler(request, exc)
        # Pydantic input/context can contain credentials or exception text. Return locations/types only.
        errors = [{"location": list(e["loc"]), "type": e["type"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content=envelope(
            "VALIDATION_ERROR", "Request does not match the simulation contract.",
            request_id=correlation(request),
            details={"violations": jsonable_encoder(errors)}))

    @app.exception_handler(HTTPException)
    async def framework_error(request: Request, exc: HTTPException):
        if not request.url.path.startswith("/api/v1/"):
            return await http_exception_handler(request, exc)
        # Framework errors (for example an unknown route or method) never echo their detail text.
        return JSONResponse(status_code=exc.status_code,
                            content=envelope("REQUEST_FAILED", "The request could not be completed.",
                                             request_id=correlation(request)),
                            headers=exc.headers)
