"""Shared daily request budgets, with the existing UTC-day refusal contract."""
import time

from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import budget_day


def daily_limit_reached(message, *, code="DAILY_LIMIT_REACHED", reason=None):
    """Build the 429 for a used-up shared daily budget, with Retry-After until the next UTC day.

    reason is log-only, for the security event (for example camera_budget).
    """
    _, next_day = budget_day()
    return IntegrationError(code, message, 429, retryable=True, retry_after=max(1, next_day - int(time.time())),
                            reason=reason)


def spend_daily(store, budget, limit, message, *, code="DAILY_LIMIT_REACHED", scope=""):
    """Count one use of a shared daily budget, or raise 429 with Retry-After until the next UTC day."""
    if not store.spend(budget, limit, scope=scope):
        raise daily_limit_reached(message, code=code)
