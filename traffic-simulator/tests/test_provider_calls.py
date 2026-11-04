"""ProviderCalls: one named shared daily budget per provider, and per-process reuse inside a window."""
import asyncio

from traffic_simulator.persistence import budget_day
from traffic_simulator.provider_calls import ProviderCalls
from traffic_simulator.store import Store


def test_calls_count_in_their_own_budget_and_stop_at_its_limit(tmp_path):
    store = Store(tmp_path)
    calls = ProviderCalls(store, "camera_calls", lambda: 2, lambda: 5)

    async def spend_three():
        return [await calls.spend() for _ in range(3)]

    assert asyncio.run(spend_three()) == [True, True, False]
    day, _ = budget_day()
    assert store.get(f"budget:camera_calls:{day}")["count"] == 2
    # Another provider's budget is a separate counter in the same table.
    assert store.get(f"budget:tomtom_calls:{day}") is None


def test_results_are_reused_inside_the_window_as_copies_and_then_forgotten():
    calls = ProviderCalls(None, "camera_calls", lambda: 2, lambda: 5)
    assert asyncio.run(calls.spend()) is True, "Without a store nothing is counted"
    calls.keep(("latest", "device"), {"retrieved_at": 100.0, "view": {"groups": []}}, now=100.0)
    reused = calls.recent(("latest", "device"), now=105.0)
    assert reused == {"retrieved_at": 100.0, "view": {"groups": []}}
    reused["view"]["groups"].append("changed by a caller")
    assert calls.recent(("latest", "device"), now=105.0)["view"]["groups"] == []
    assert calls.recent(("latest", "device"), now=105.1) is None
    calls.keep(("image", "device", "id"), {"retrieved_at": 106.0}, now=106.0)
    assert list(calls.saved) == [("image", "device", "id")], "keep() forgets results that left the window"


def test_a_zero_window_keeps_nothing():
    calls = ProviderCalls(None, "camera_calls", lambda: 2, lambda: 0)
    calls.keep("key", {"retrieved_at": 1.0}, now=1.0)
    assert calls.recent("key", now=1.0) is None and calls.saved == {}
