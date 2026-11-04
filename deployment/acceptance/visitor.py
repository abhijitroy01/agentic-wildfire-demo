"""The anonymous visitor's workspace API client and receipt writer, shared by the acceptance checks.

There is no user sign-in: a client sends the headers a same-origin browser sends, and the demo session is the cookie
that POST demo/session sets on it.
"""
import json

import httpx

# Seconds per request: above CloudFront's 60-second origin read timeout (deployment/infra/lib/stacks.ts), so a slow
# API answer arrives as the edge's own response rather than as a client timeout.
TIMEOUT_SECONDS = 65


class AcceptanceError(RuntimeError):
    """A workspace API request made during acceptance did not succeed."""


def visitor_client(origin):
    """Open an httpx client for the workspace API at origin with a same-origin browser's Origin headers."""
    origin = origin.rstrip("/")
    return httpx.Client(base_url=origin + "/simulation-api/v1/", timeout=TIMEOUT_SECONDS,
                        headers={"Origin": origin, "Sec-Fetch-Site": "same-origin"})


def error_code(body):
    """Return the code of a v1 error envelope ({"error": {"code": ...}}), or "" for any other body."""
    try:
        value = json.loads(body)
    except ValueError:
        return ""
    error = value.get("error") if isinstance(value, dict) else None
    return error.get("code", "") if isinstance(error, dict) else ""


def checked(response):
    """Return a successful (2xx) response; otherwise raise AcceptanceError with the request, status and v1 code."""
    if response.is_success:
        return response
    response.read()  # A streamed response's error body is read only now.
    request = response.request
    # The path only: a query string can carry a presigned credential.
    raise AcceptanceError(f"{request.method} {request.url.path}: HTTP {response.status_code} "
                          f"{error_code(response.content) or '(no v1 error code)'}")


def write_report(path, report):
    """Write an acceptance receipt as indented JSON, creating its folder first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")
