"""Logging safety for outbound HTTP: provider request URLs can carry API keys."""
import logging

# httpx logs every request URL at INFO; httpcore logs connection traces at DEBUG.
HTTP_CLIENT_LOGGERS = ("httpx", "httpcore")


def quiet_http_client_logs() -> None:
    """Raise the httpx and httpcore loggers to WARNING so request URLs and provider API keys are never logged."""
    for name in HTTP_CLIENT_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
