"""Describe failures for the server log without the values they quote.

Exception messages can quote prompts, alert text, model output or searched
coordinates (a pydantic ValidationError repeats its input, for example), so
logs and clients get this summary instead of ``str(exc)``.
"""

from botocore.exceptions import ClientError
from pydantic import ValidationError


def error_summary(exc: BaseException) -> str:
    """Describe a failure for the log by its type, AWS error code or validation error types, never its message."""
    if isinstance(exc, ValidationError):
        types = sorted({error["type"] for error in exc.errors(include_url=False, include_input=False)})
        return f"{type(exc).__name__} ({exc.error_count()} errors: {', '.join(types)})"
    if isinstance(exc, ClientError):
        return f"{type(exc).__name__} {exc.response.get('Error', {}).get('Code', 'unknown')}"
    return type(exc).__name__
