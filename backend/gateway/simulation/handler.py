"""Thin private-network adapter. Canonical validation and ownership live in Lab.

The adapter holds no signing key: it checks only the shape of a tool call and forwards the signed context
unchanged. The workspace API's internal tool route (backend/evacuation/agent_broker.py, verified by
backend/trusted_context.py) checks the context's signature, purpose, expiry and allowed operations.
"""
import http.client
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache

logger = logging.getLogger(__name__)
OPERATIONS = {"prepare_simulation", "submit_simulation", "get_simulation_status",
              "get_simulation_evidence", "create_simulation_variant"}
# The simulator's v1 error envelope for these statuses reaches the agent unchanged; anything else is generic.
FORWARDED_STATUSES = {400, 401, 403, 404, 409, 410, 422, 429, 503}


@lru_cache(maxsize=1)
def simulator_origin():
    """Validate SIMULATION_INTERNAL_URL once: HTTPS, or plain HTTP only to an internal AWS load balancer."""
    url = urllib.parse.urlsplit(os.environ.get("SIMULATION_INTERNAL_URL", ""))
    host = url.hostname or ""
    # The stack output is http://<internal ALB DNS name>; that hop stays inside the VPC (decisions D4, D7).
    internal = host.startswith("internal-") and host.endswith(".elb.amazonaws.com")
    allowed = (url.scheme == "https" and bool(host)) or (url.scheme == "http" and internal)
    if not allowed or "@" in url.netloc or url.query or url.fragment:
        raise RuntimeError("SIMULATION_INTERNAL_URL must be https://<host> or http://<internal load "
                           f"balancer DNS name>; got scheme {url.scheme!r} and host {host!r}")
    return f"{url.scheme}://{url.netloc}{url.path.rstrip('/')}"


@lru_cache(maxsize=1)
def opener():
    """Build an HTTP(S)-only URL opener with no redirect, proxy, file, FTP or data handlers."""
    # Without a redirect handler every 3xx raises HTTPError, so the signed context stays on one origin.
    director = urllib.request.OpenerDirector()
    for handler_class in (urllib.request.UnknownHandler, urllib.request.HTTPHandler,
                          urllib.request.HTTPSHandler, urllib.request.HTTPDefaultErrorHandler,
                          urllib.request.HTTPErrorProcessor):
        director.add_handler(handler_class())
    return director


def unavailable():
    """Return the generic error the agent sees whenever the simulator API gives no usable answer."""
    return {"error": {"code": "SIMULATOR_UNAVAILABLE", "message": "Simulator API is unavailable."}}


def error_envelope(exc):
    """Return the simulator's v1 error envelope from a forwardable HTTP error, or None."""
    if exc.code not in FORWARDED_STATUSES:
        return None
    try:
        body = json.loads(exc.read())
    except (ValueError, OSError, http.client.HTTPException):
        return None
    return body if isinstance(body, dict) and isinstance(body.get("error"), dict) else None


def forward(name, payload, token):
    """POST one authorized tool call to the private simulator API and return its JSON or a stable error."""
    try:
        url = f"{simulator_origin()}/api/internal/v1/simulations/{name}"
    except (RuntimeError, ValueError):
        logger.exception("The simulation adapter is misconfigured")
        return unavailable()
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Simulation-Context": token}, method="POST")
    try:
        with opener().open(request, timeout=55) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        envelope = error_envelope(exc)
        if envelope is not None:
            return envelope
        logger.warning("Simulator API answered HTTP %s without a forwardable error envelope", exc.code)
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        logger.warning("Simulator API request failed: %s", type(exc).__name__)
    return unavailable()


def handler(event, context):
    """Check the shape of one AgentCore Gateway simulation tool call, then forward it with its signed context.

    The workspace API verifies the context and authorizes the operation; a malformed call never leaves here.
    """
    try:
        token = event["trusted_context"]
        if not isinstance(token, str) or not token:
            raise ValueError("Missing service context")
        custom = context.client_context.custom
        name = custom["bedrockAgentCoreToolName"].split("___")[-1]
        if name not in OPERATIONS:
            raise ValueError("Unknown operation")
        payload = json.loads(event["payload"])
        if not isinstance(payload, dict):
            raise TypeError("Invalid payload")
    except (ValueError, KeyError, AttributeError, TypeError):
        return {"error": {"code": "INVALID_SERVICE_CONTEXT", "message": "Invalid simulation request."}}
    return forward(name, payload, token)
