"""Short-lived signed service contexts for the AgentCore Runtime; the language model never sees them.

Each context purpose has its own HS256 key, derived from the signing secret (purpose_key), so a token signed
for one purpose never verifies as another. The Runtime derives the same keys and applies the same claim rules
(backend/runtime/utils/simulation_context.py). The secret is read again once MAX_LIFETIME_SECONDS (300 s) have
passed, so a rotated value takes effect within five minutes without a restart.
"""
import hashlib
import hmac
import os
import re
import threading
import time
from uuid import UUID

import boto3
import jwt
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

ISSUER = "wildfire-simulation-broker"
AUDIENCE = "wildfire-simulation-tools-v1"
OPERATIONS = {"prepare_simulation", "submit_simulation", "get_simulation_status",
              "get_simulation_evidence", "create_simulation_variant"}
# The only purposes a context is signed or verified for; each key is HMAC-SHA256(secret, KEY_LABEL + purpose).
PURPOSES = ("simulation", "investigation", "firetruck_report")
KEY_LABEL = "wildfire-signed-context-v1:"
# Token protocol bounds shared with the Runtime's verifier, not operational settings (seconds): a signed
# context lives 240 s, verifiers allow 30 s of clock skew and refuse a claimed lifetime over 300 s. The signing
# secret stays cached for MAX_LIFETIME_SECONDS too (decision D38), so a rotated key reaches the API within one
# context lifetime, without a restart.
LIFETIME_SECONDS = 240
CLOCK_SKEW_SECONDS = 30
MAX_LIFETIME_SECONDS = 300
HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
# arn:partition:service:region:account-id:resource
FULL_ARN = re.compile(r"arn:[a-z][a-z0-9-]*:[a-z0-9-]+:([a-z0-9-]+):\d{12}:.+")

_cache = {"value": None, "expires": 0.0}
_cache_lock = threading.Lock()


def arn_region(arn: str, variable: str) -> str:
    """Return the AWS Region named by a full ARN; fail with the variable name for names or partial ARNs."""
    match = FULL_ARN.fullmatch(arn)
    if not match:
        raise RuntimeError(f"{variable} must be a full ARN (arn:partition:service:region:account-id:resource)")
    return match.group(1)


def read_secret() -> str:
    """Read the context signing secret from SIMULATION_CONTEXT_SECRET, else from SIMULATION_CONTEXT_SECRET_ARN.

    A missing, unreadable or shorter than 32-byte secret raises RuntimeError naming the variables; the value is
    never logged or included in an error.
    """
    value = os.getenv("SIMULATION_CONTEXT_SECRET", "")
    arn = os.getenv("SIMULATION_CONTEXT_SECRET_ARN", "")
    if not value and arn:
        # The secret ARN names its Region; never fall back to a hardcoded one.
        region = arn_region(arn, "SIMULATION_CONTEXT_SECRET_ARN")
        try:
            client = boto3.client("secretsmanager", region_name=region,
                                  config=Config(connect_timeout=3, read_timeout=5,
                                                retries={"mode": "standard", "max_attempts": 2}))
            value = client.get_secret_value(SecretId=arn)["SecretString"]
        except (BotoCoreError, ClientError, KeyError) as exc:
            raise RuntimeError("Could not read the signing secret named by SIMULATION_CONTEXT_SECRET_ARN") from exc
    if not isinstance(value, str) or len(value.encode()) < 32:
        raise RuntimeError("The context signing secret is not configured: SIMULATION_CONTEXT_SECRET_ARN (or "
                           "SIMULATION_CONTEXT_SECRET locally) must name a secret of at least 32 bytes")
    return value


def secret() -> str:
    """Return the context signing secret, cached and read again once MAX_LIFETIME_SECONDS (300 s) have passed.

    A failed read raises RuntimeError; the previous value is never used after its lifetime.
    """
    with _cache_lock:
        if _cache["value"] is None or time.monotonic() >= _cache["expires"]:
            _cache["value"] = read_secret()
            _cache["expires"] = time.monotonic() + MAX_LIFETIME_SECONDS
        return _cache["value"]


def clear_secret_cache() -> None:
    """Forget the cached context signing secret, so the next signature or check reads it again."""
    with _cache_lock:
        _cache.update(value=None, expires=0.0)


def purpose_key(secret_value: str, purpose: str) -> bytes:
    """Derive a context purpose's HS256 key from the signing secret: HMAC-SHA256(secret, KEY_LABEL + purpose)."""
    if purpose not in PURPOSES:
        raise ValueError("Unknown service context purpose")
    label = (KEY_LABEL + purpose).encode("utf-8")
    return hmac.new(secret_value.encode("utf-8"), label, hashlib.sha256).digest()


def sign(context: dict, purpose: str) -> str:
    """Sign a service context for one purpose with that purpose's key; the token is valid for 240 seconds.

    The signer writes the purpose claim and refuses a context that names another purpose. A signing secret
    that cannot be read raises RuntimeError, which callers answer with 503 AGENT_NOT_CONFIGURED.
    """
    if context.get("purpose", purpose) != purpose:
        raise ValueError("A service context is signed only for the purpose it names")
    key = purpose_key(secret(), purpose)
    now = int(time.time())
    return jwt.encode({**context, "purpose": purpose, "iss": ISSUER, "aud": AUDIENCE, "iat": now, "nbf": now,
                       "exp": now+LIFETIME_SECONDS}, key, algorithm="HS256")


def _is_uuid(value) -> bool:
    """Tell whether a claim is a UUID string, as the broker's turn identifiers are."""
    if not isinstance(value, str):
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def check_claims(claims: dict, purpose: str) -> dict:
    """Check a context's claims against the canonical rules for its purpose; raise ValueError when one fails.

    The Runtime applies the same rules: purpose, issuer and audience; integer times within the clock skew and
    lifetime bounds; a non-empty subject and session; and each purpose's own claims and allowed operations.
    """
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
    now = time.time()
    lifetime = claims["exp"] - claims["iat"]
    if (claims["exp"] < now-CLOCK_SKEW_SECONDS or claims["nbf"] > now+CLOCK_SKEW_SECONDS
            or claims["iat"] > now+CLOCK_SKEW_SECONDS or not 0 < lifetime <= MAX_LIFETIME_SECONDS):
        raise ValueError("Service context is expired or not yet valid")
    if purpose == "simulation":
        valid = (set(operations) <= OPERATIONS and text("incident_id") and text("attempt_id")
                 and _is_uuid(claims.get("turn_id"))
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


def verify(token: str, purpose: str) -> dict:
    """Verify a broker-signed service context with its purpose's key and return its checked claims.

    The algorithm is pinned to HS256. A malformed or invalid token raises jwt.PyJWTError or ValueError (callers
    answer 401); a signing secret that cannot be read raises RuntimeError.
    """
    key = purpose_key(secret(), purpose)
    try:
        claims = jwt.decode(token, key, algorithms=["HS256"], issuer=ISSUER, audience=AUDIENCE,
                            leeway=CLOCK_SKEW_SECONDS, options={"require": ["exp", "iat", "nbf", "sub"]})
    except (TypeError, OverflowError) as exc:
        # PyJWT lets these escape for a signed time claim that is a list, an object or out of range.
        raise ValueError("Malformed service context") from exc
    return check_claims(claims, purpose)
