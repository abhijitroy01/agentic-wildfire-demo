"""Retry only an identical retained-artifact operation, inside its current worker lease."""
import time

from botocore.exceptions import ClientError, ConnectionError, HTTPClientError

from traffic_simulator.errors import IntegrationError


def transient(exc):
    if isinstance(exc, (ConnectionError, HTTPClientError)):
        return True
    if isinstance(exc, ClientError):
        return (exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") in (429, 500, 502, 503, 504)
                or exc.response.get("Error", {}).get("Code") in
                ("SlowDown", "RequestTimeout", "InternalError", "ServiceUnavailable", "Throttling"))
    return False


def retained(operation, check):
    """A short storage outage does not rerun network acquisition, billed capture or simulation."""
    from traffic_simulator import settings

    attempts = settings.artifact_write_max_attempts()
    for attempt in range(attempts):
        check()
        try:
            value = operation()
            check()
            return value
        except Exception as exc:
            if not transient(exc):
                raise
            if attempt + 1 == attempts:
                raise IntegrationError("ARTIFACT_STORE_UNAVAILABLE",
                                       "Simulation artifact storage is temporarily unavailable.", 503,
                                       retryable=True) from None
            pause(min(2, 0.5 * 2 ** attempt), check)
    raise AssertionError("Artifact attempts must be positive")


def pause(seconds, check):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        check()
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    check()
