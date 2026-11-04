"""Safe domain errors; never expose provider responses or credentials."""
import logging

logger = logging.getLogger(__name__)

# Stable public text for failures whose own message can carry tool output, provider data or server paths.
PUBLIC_FAILURES = (
    (TimeoutError, "A data service or road network conversion timed out. Retry later or reduce the area radius."),
    (OSError, "A server file or network operation failed. Retry later."),
)
UNEXPECTED_FAILURE = "Unexpected server error. Operators can find the details in the server log."
MAX_PUBLIC_MESSAGE = 500


class IntegrationError(Exception):
    """A user-safe API error: stable code, message, HTTP status, retry hint and optional Retry-After seconds.

    reason is an optional server-side detail for the security event log (for example other_owner on a 404);
    it is never sent to the client.
    """

    def __init__(self, code: str, message: str, status: int = 422, *, retryable: bool = False,
                 retry_after: int | None = None, reason: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after
        self.reason = reason


def public_message(exc: BaseException) -> str:
    """Return user-safe failure text: curated domain messages only, never tool output or provider text.

    IntegrationError, plain ValueError/RuntimeError and this package's own subclasses of them carry
    messages written in this codebase; library, OS and engine errors get a stable message per type.
    """
    if isinstance(exc, IntegrationError):
        return exc.message
    kind = type(exc)
    if kind in (ValueError, RuntimeError) or (
            issubclass(kind, (ValueError, RuntimeError)) and kind.__module__.startswith("traffic_simulator.")):
        return str(exc)[:MAX_PUBLIC_MESSAGE]
    return next((message for base, message in PUBLIC_FAILURES if isinstance(exc, base)), UNEXPECTED_FAILURE)


def provider_failure(exc: Exception, label: str, unavailable: str, invalid: str, *,
                     keyless: bool = False) -> IntegrationError:
    """Map a public-provider failure to a retryable 503 (unreachable) or 502 (unusable reply).

    Clients get a stable code and generic message, never provider text. The server log keeps the
    exception type, plus the traceback only for keyless providers whose errors cannot carry API keys.
    """
    logger.warning("%s failed (%s)", label, type(exc).__name__, exc_info=exc if keyless else None)
    if isinstance(exc, OSError):
        return IntegrationError(unavailable, f"{label} is unavailable. Retry later.", 503, retryable=True)
    return IntegrationError(invalid, f"{label} returned an unusable response. Retry later.", 502,
                            retryable=True)
