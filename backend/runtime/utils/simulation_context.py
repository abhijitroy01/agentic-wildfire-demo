"""Check the workspace broker's signed service contexts in the Runtime; canonical validation lives in Lab.

Every context purpose has its own HS256 key, derived from the signing secret (purpose_key), so a token
signed for one purpose never verifies as another. verify() raises ValueError for any malformed or invalid
token (callers answer 401) and RuntimeError when the signing secret cannot be read (callers answer 503).
The secret is re-read from Secrets Manager once MAX_LIFETIME_SECONDS (300 s) have passed, so a rotated value
takes effect within five minutes without a restart.
"""
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import threading
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from utils.aws_clients import deployment_region

ISSUER = "wildfire-simulation-broker"
AUDIENCE = "wildfire-simulation-tools-v1"
OPERATIONS = {"prepare_simulation", "submit_simulation", "get_simulation_status",
              "get_simulation_evidence", "create_simulation_variant"}
PURPOSES = ("simulation", "investigation", "firetruck_report")
# The workspace API derives the same keys: HMAC-SHA256(signing secret, KEY_LABEL + purpose).
KEY_LABEL = "wildfire-signed-context-v1:"
# Protocol bounds shared with the API's verifier (PyJWT leeway and the broker's 240-second lifetime),
# not operational settings: allowed clock skew and the longest lifetime a context may claim, in seconds.
# The signing secret stays cached for MAX_LIFETIME_SECONDS too (decision D38), so a rotated key reaches
# this reader within one context lifetime, without a restart.
CLOCK_SKEW_SECONDS = 30
MAX_LIFETIME_SECONDS = 300
SEGMENT = re.compile(r"[A-Za-z0-9_-]+")
HEX_DIGEST = re.compile(r"[0-9a-f]{64}")

_cache = {"value": None, "expires": 0.0}
_cache_lock = threading.Lock()


def read_secret():
    """Read the signing secret named by SIMULATION_CONTEXT_SECRET_ARN from Secrets Manager; it is never logged."""
    arn = os.getenv("SIMULATION_CONTEXT_SECRET_ARN", "").strip()
    if not arn:
        raise RuntimeError("SIMULATION_CONTEXT_SECRET_ARN is required to check signed service contexts")
    try:
        value = boto3.client("secretsmanager", region_name=deployment_region(),
                             config=Config(connect_timeout=3, read_timeout=5,
                                           retries={"mode": "standard", "max_attempts": 2})
                             ).get_secret_value(SecretId=arn)["SecretString"]
    except (BotoCoreError, ClientError, KeyError) as exc:
        raise RuntimeError("Could not read the signing secret named by SIMULATION_CONTEXT_SECRET_ARN") from exc
    if not isinstance(value, str) or len(value.encode()) < 32:
        raise RuntimeError("The secret named by SIMULATION_CONTEXT_SECRET_ARN must hold at least 32 bytes")
    return value


def secret():
    """Return the signing secret, read again once MAX_LIFETIME_SECONDS (300 s) have passed since the last read."""
    with _cache_lock:
        if _cache["value"] is None or time.monotonic() >= _cache["expires"]:
            _cache["value"] = read_secret()
            _cache["expires"] = time.monotonic() + MAX_LIFETIME_SECONDS
        return _cache["value"]


def clear_secret_cache():
    """Forget the cached signing secret, so the next check reads it from Secrets Manager again."""
    with _cache_lock:
        _cache.update(value=None, expires=0.0)


def purpose_key(secret_value, purpose):
    """Derive the HS256 key of one context purpose from the signing secret: HMAC-SHA256(secret, label + purpose)."""
    if purpose not in PURPOSES:
        raise ValueError("Unknown service context purpose")
    return hmac.new(secret_value.encode("utf-8"), (KEY_LABEL + purpose).encode("utf-8"), hashlib.sha256).digest()


def _segment(text):
    """Decode one base64url segment of a compact token; raise ValueError for anything else."""
    if not SEGMENT.fullmatch(text):
        raise ValueError("Malformed service context")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except binascii.Error as exc:
        raise ValueError("Malformed service context") from exc


def _json_object(text):
    """Decode a base64url segment that must hold a JSON object."""
    try:
        value = json.loads(_segment(text))
    except (ValueError, RecursionError) as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise ValueError("Malformed service context") from exc
    if isinstance(value, dict):
        return value
    # Every malformed token is a rejection (ValueError), which callers answer with 401.
    raise ValueError("Malformed service context")


def _is_uuid(value):
    """Tell whether a claim is a UUID string, as the API's turn identifiers are."""
    try:
        uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return True


def check_claims(claims, purpose, now=None):
    """Check a context's claims against the canonical set for its purpose; raise ValueError when one fails."""
    def text(name):
        """Tell whether a claim is a non-empty string."""
        return isinstance(claims.get(name), str) and bool(claims[name])

    def whole(name):
        """Tell whether a claim is an integer and not a boolean."""
        return type(claims.get(name)) is int

    operations = claims.get("allowed_operations")
    if not (claims.get("purpose") == purpose and claims.get("iss") == ISSUER and claims.get("aud") == AUDIENCE
            and whole("iat") and whole("nbf") and whole("exp") and text("sub") and text("session_id")
            and isinstance(operations, list) and all(isinstance(item, str) for item in operations)):
        raise ValueError("Invalid service context claims")
    now = time.time() if now is None else now
    if (claims["exp"] < now - CLOCK_SKEW_SECONDS or claims["nbf"] > now + CLOCK_SKEW_SECONDS
            or claims["iat"] > now + CLOCK_SKEW_SECONDS or not 0 < claims["exp"] - claims["iat"] <= MAX_LIFETIME_SECONDS):
        raise ValueError("Service context is expired or not yet valid")
    if purpose == "simulation":
        valid = (set(operations) <= OPERATIONS and text("incident_id") and text("attempt_id")
                 and text("turn_id") and _is_uuid(claims["turn_id"])
                 and whole("location_revision") and claims["location_revision"] >= 1
                 and (claims.get("draft_id") is None or isinstance(claims["draft_id"], str)))
    elif purpose == "firetruck_report":
        valid = (operations == [] and isinstance(claims.get("context_hash"), str)
                 and bool(HEX_DIGEST.fullmatch(claims["context_hash"])))
    else:
        valid = operations == []
    if not valid:
        raise ValueError("Invalid service context claims")
    return claims


def verify(token, purpose, secret_value=None):
    """Verify a broker-signed HS256 service context for one purpose; return its claims or raise ValueError.

    The token's structure is checked before the signing secret is read, so a malformed token never
    reaches Secrets Manager. A secret that cannot be read raises RuntimeError instead.
    """
    if purpose not in PURPOSES:
        raise ValueError("Unknown service context purpose")
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3:
        raise ValueError("Malformed service context")
    header, claims = _json_object(parts[0]), _json_object(parts[1])
    signature = _segment(parts[2])
    if header.get("alg") != "HS256":
        raise ValueError("Invalid service context algorithm")
    key = purpose_key(secret() if secret_value is None else secret_value, purpose)
    expected = hmac.new(key, f"{parts[0]}.{parts[1]}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        raise ValueError("Invalid service context signature")
    return check_claims(claims, purpose)


def investigation_identity(forwarded, thread_id, secret_value=None):
    """Return the (session_id, actor_id) bound by a signed investigation context, or None when unsigned.

    The workspace broker signs the context. The memory actor and session never come from unsigned
    forwarded fields; a present but invalid, unbound or wrong-purpose context raises ValueError.
    """
    token = forwarded.get("investigation_context")
    if token is None:
        return None
    claims = verify(token, "investigation", secret_value)
    if claims["session_id"] != thread_id:
        raise ValueError("Investigation context is not bound to this invocation")
    return claims["session_id"], claims["sub"]
