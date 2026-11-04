"""Billed-provider call accounting shared by the TomTom readings and the workspace API's camera broker.

Limits are callables read at call time, so importing this module reads no settings (local_server.py and
configure_tomtom.py import the TomTom module before STORAGE_MODE is set).
"""
import asyncio
import copy
import threading
import time
from collections.abc import Callable, Hashable


class ProviderCalls:
    """One process's calls to a billed provider: a share of a daily call budget and recent results reused.

    spend() counts one call in the named budget in the jobs table, shared by every visitor and process. A result
    kept here is reused for its key while now - result["retrieved_at"] is within reuse_seconds(); keep() also
    forgets results that left the window. Without a store (tests, local scripts) nothing is counted. Callers can
    run in background threads, so a lock guards the results.
    """

    def __init__(self, store, budget: str, calls_per_day: Callable[[], int], reuse_seconds: Callable[[], float]):
        self.store = store
        self.budget = budget
        self.calls_per_day = calls_per_day
        self.reuse_seconds = reuse_seconds
        self.saved = {}
        self.lock = threading.Lock()

    def recent(self, key: Hashable, now: float | None = None) -> dict | None:
        """Return a copy of the result kept for key while it is inside the reuse window, else None."""
        now = time.time() if now is None else now
        with self.lock:
            saved = self.saved.get(key)
            if saved is None or now - saved["retrieved_at"] > self.reuse_seconds():
                return None
            return copy.deepcopy(saved)

    def keep(self, key: Hashable, result: dict, now: float | None = None) -> None:
        """Keep a successful result for reuse and forget results that left the reuse window."""
        now = time.time() if now is None else now
        window = self.reuse_seconds()
        with self.lock:
            self.saved = {k: v for k, v in self.saved.items() if now - v["retrieved_at"] <= window}
            if window:
                self.saved[key] = copy.deepcopy(result)

    async def spend(self) -> bool:
        """Take one call from today's shared budget (calls_per_day()); False once it is used up."""
        if self.store is None:
            return True
        # The counter is a blocking table write; keep it off the event loop.
        return await asyncio.to_thread(self.store.spend, self.budget, self.calls_per_day())
