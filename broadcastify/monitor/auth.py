"""Radio service key check, used by the deployment gate (private.py) and the investigation behind it."""
import hmac

# Shortest API_KEY the radio service accepts, in characters. A missing or shorter
# key rejects every request instead of turning authentication off.
MIN_KEY_LENGTH = 32


def key_is_strong(configured: str | None) -> bool:
    """Report whether the configured radio API key is long enough to enforce."""
    return len(configured or "") >= MIN_KEY_LENGTH


def key_matches(supplied: str | bytes | None, configured: str | None) -> bool:
    """Compare a caller's radio API key with API_KEY in constant time; always false without a strong key."""
    if not supplied or not key_is_strong(configured):
        return False
    if isinstance(supplied, str):
        supplied = supplied.encode()
    return hmac.compare_digest(supplied, configured.encode())
