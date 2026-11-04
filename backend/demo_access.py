"""Workspace access without user sign-in: anonymous demo sessions, or one local developer.

Deployed visitors use opaque HttpOnly demo-session cookies with scoped records and same-origin writes,
only through the CloudFront workspace route while public access is open. Local development
(STORAGE_MODE=local) serves every other request as one local researcher.
Settings are read per request so the offline contract exporter never loads deployment settings.
"""
import re
import secrets
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import commit, digest, record

from backend import security_events

COOKIE = "__Host-wildfire-demo"
# The single owner of local-development requests that do not arrive through the demo route.
LOCAL_OWNER = "local-researcher"


def public_demo(request):
    """True when public access is open (PUBLIC_DEMO_MODE=true) and CloudFront's workspace route sent the request."""
    from backend import api_settings as settings
    return settings.public_demo_mode() and request.headers.get("x-lab-workspace") == "public-demo"


def access_mode(request) -> str:
    """Name how the workspace serves a request: public_demo (demo session), local (one developer) or closed."""
    if public_demo(request):
        return "public_demo"
    from traffic_simulator import settings
    return "closed" if settings.CLOUD else "local"


def same_origin(request):
    """Refuse a write unless it comes from the workspace's own HTTPS origin (FABRIC_PUBLIC_URL)."""
    from backend import api_settings as settings
    expected = settings.public_url()
    origin = request.headers.get("origin", "")
    if not expected or origin != expected or urlsplit(origin).scheme != "https":
        raise IntegrationError("ORIGIN_NOT_ALLOWED", "Use this application from its CloudFront page.", 403,
                               reason="origin_mismatch")
    site = request.headers.get("sec-fetch-site")
    if site and site != "same-origin":
        raise IntegrationError("ORIGIN_NOT_ALLOWED", "Cross-site requests are not accepted.", 403,
                               reason="cross_site_fetch")


def session_lookup(request):
    """Return (the cookie's unexpired demo-session record, None), or (None, why) for the security event log.

    The log-only reason is no_cookie, malformed_cookie, unknown_session or expired_session.
    """
    token = request.cookies.get(COOKIE, "")
    if not token:
        return None, "no_cookie"
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return None, "malformed_cookie"
    saved = request.app.state.integration_store().get(f"demo_session:{digest(token)}")
    if not saved:
        return None, "unknown_session"
    if saved["expires"] <= time.time():
        return None, "expired_session"
    return saved, None


def session_record(request):
    """Return the unexpired demo-session record that the request's cookie names, or None."""
    return session_lookup(request)[0]


def workspace_owner(request: Request) -> str:
    """Return the request owner: the demo session subject through CloudFront, or the local developer.

    There is no user sign-in. A deployed request outside the open public workspace route gets 403
    WORKSPACE_CLOSED; a missing or expired demo cookie gets 401, which the UI answers with a new session.
    Later refusals in the request name this owner, by digest, in the security event log.
    """
    mode = access_mode(request)
    if mode == "closed":
        raise IntegrationError("WORKSPACE_CLOSED", "Workspace access is not open on this route.", 403)
    if mode == "local":
        owner = LOCAL_OWNER
    else:
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            same_origin(request)
        saved, reason = session_lookup(request)
        if not saved:
            raise IntegrationError("DEMO_SESSION_REQUIRED", "Initialize a demo browser session.", 401,
                                   reason=reason)
        owner = saved["subject"]
    security_events.bind_owner(request, owner)
    return owner


def create_demo_router():
    """Build the public demo-session route: one opaque HttpOnly cookie per browser, limited per viewer IP."""
    router = APIRouter()

    @router.post("/api/v1/demo/session")
    def initialize(request: Request, response: Response):
        """Start an anonymous demo session or reuse this browser's; 429 DEMO_SESSION_LIMIT past the daily per-network count."""
        if not public_demo(request):
            raise IntegrationError("DEMO_NOT_ENABLED", "Public demo access is not enabled on this route.", 404)
        same_origin(request)
        saved = session_record(request)
        created = not saved
        if not saved:
            from traffic_simulator import settings as simulator_settings

            from backend import api_settings as settings
            from backend import budgets
            store = request.app.state.integration_store()
            # Each session has its own job limits, so new sessions per viewer IP are limited per UTC day.
            # CloudFront's viewer-request function sets the header; the digest keeps the address out of the key.
            budgets.spend_daily(store, "demo_sessions", settings.demo_sessions_per_ip_per_day(),
                                "Daily demo limit reached: your network has started its demo sessions for today. "
                                "Try again tomorrow.", code="DEMO_SESSION_LIMIT",
                                scope=request.headers.get("x-lab-viewer-ip", ""))
            lifetime = simulator_settings.demo_session_days() * 24 * 60 * 60
            token = secrets.token_urlsafe(32)
            owner = "demo-" + digest(token)
            saved = record("demo_session", digest(token), owner, expires=int(time.time())+lifetime)
            commit(store, [(saved, None)])
            # A new anonymous owner identity: audited by its digest, never by the cookie.
            security_events.bind_owner(request, owner)
            security_events.allowed("demo_session_created", request)
            response.set_cookie(COOKIE, token, max_age=lifetime, secure=True, httponly=True,
                                samesite="strict", path="/")
        response.headers["Cache-Control"] = "no-store"
        return {"ready": True, "access_mode": "public_demo", "session_created": created}
    return router
