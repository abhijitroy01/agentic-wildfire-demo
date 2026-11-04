"""A billed lookup is reserved before invocation; retries rejoin saved evidence, never replay Lambda."""
import time

from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import commit, digest, record

from backend import budgets

# Longer than the geocoder's existing single 2-second connect/12-second read attempt and persistence grace.
PENDING_SECONDS = 30


def lookup(store, owner, request_id, body, resolve):
    """Return a retained lookup, or reserve and perform it once for this owner/request/body."""
    key = digest([owner, "incident_geocode", str(request_id)])
    identifier, body_hash = f"incident_geocode:{key}", digest(body)
    saved = store.get(identifier)
    if saved and saved["body_hash"] != body_hash:
        raise IntegrationError("IDEMPOTENCY_CONFLICT", "Request ID was reused with different inputs.", 409)
    # A refused daily admission proves Lambda was never invoked. Only that state may be reserved again
    # once its stated retry time arrives; invocation ambiguity and semantic refusal never replay.
    refused = saved and saved["status"] == "not_invoked" and time.time() >= saved["retry_at"]
    if saved is None or refused:
        revision = saved["revision"] if saved else None
        reservation = ({**saved, "revision": revision + 1, "status": "invoking", "started_at": time.time()}
                       if saved else record("incident_geocode", key, owner, body_hash=body_hash,
                                            status="invoking", started_at=time.time()))
        try:
            commit(store, [(reservation, revision)])
        except IntegrationError as exc:
            if exc.code != "REVISION_CONFLICT":
                raise
            saved = store.get(identifier)
        else:
            return perform(store, reservation, resolve)
    if saved is None:
        raise unknown()
    if saved["status"] == "resolved":
        return saved["receipt"]
    if saved["status"] in ("refused", "not_invoked"):
        raise IntegrationError(saved["error_code"], saved["error"], saved["http_status"],
                               retryable=saved.get("retryable", False),
                               retry_after=max(1, int(saved["retry_at"] - time.time()))
                               if saved["status"] == "not_invoked" else saved.get("retry_after"))
    if saved["status"] == "invoking" and time.time() - saved["started_at"] < PENDING_SECONDS:
        raise IntegrationError("GEOCODER_IN_PROGRESS", "This address lookup is still being resolved. Retry the "
                               "same request to recover its saved result.", 409, retryable=True, retry_after=1)
    raise unknown()


def perform(store, reservation, resolve):
    from backend import api_settings as settings

    revision = reservation["revision"]

    def save(**values):
        reservation.update(revision=revision + 1, **values)
        commit(store, [(reservation, revision)])

    try:
        budgets.spend_daily(store, "geocoding_requests", settings.geocoding_requests_per_day(),
                            "Daily demo limit reached for address lookups. Try again tomorrow; "
                            "the limit resets at midnight UTC.")
    except IntegrationError as exc:
        # No Lambda invocation occurred. Retry admission only after the explicit daily refusal expires.
        save(status="not_invoked", error_code=exc.code, error=exc.message, http_status=exc.status,
             retryable=exc.retryable, retry_at=time.time() + (exc.retry_after or 86400))
        raise
    try:
        result = resolve()
    except Exception as exc:
        # A failed/absent response does not prove the billed invocation never ran.
        if isinstance(exc, IntegrationError) and exc.code in (
                "LOCATION_UNRESOLVED", "LOCATION_AMBIGUOUS", "GEOCODER_NOT_CONFIGURED"):
            save(status="refused", error_code=exc.code, error=exc.message, http_status=exc.status,
                 retryable=exc.retryable, retry_after=exc.retry_after)
            raise
        save(status="unknown")
        raise unknown() from None
    save(status="resolved", receipt=result)
    return result


def unknown():
    return IntegrationError("GEOCODER_OUTCOME_UNKNOWN", "The address lookup outcome could not be confirmed. "
                            "Its paid invocation was not repeated. Start a new location lookup explicitly "
                            "if you want another attempt.", 409)
