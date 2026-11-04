"""Automatic extent reduction retains one already selected immutable road source."""
import hashlib

from traffic_simulator import network_preparation
from traffic_simulator.models import Scenario
from traffic_simulator.network import NetworkTooLarge


def test_size_reduction_pins_source_snapshot_without_resolving_a_new_date(monkeypatch, grid):
    from test_network_providers import source_manifest

    source, observed = source_manifest(), []
    scenario = Scenario(radius_km=4)

    def prepare(candidate, progress, check, *, timings, source_manifest=None):
        check()
        observed.append((candidate.radius_km, source_manifest))
        if candidate.radius_km == 4:
            raise NetworkTooLarge("Too many fixture edges", edge_count=1000, source_manifest=source)
        assert source_manifest == source
        grid[7].update(net_sha256=hashlib.sha256(grid[0].read_bytes()).hexdigest(), highway_edge_count=2,
                       gates=[], source_manifest=source, resolved_scenario=candidate.model_dump())
        return grid

    monkeypatch.setattr(network_preparation, "prepare_network", prepare)
    bundle = network_preparation.prepare(scenario, lambda *_: None, lambda: None)
    assert observed == [(4, None), (3, source)]
    assert bundle["validation"]["adjusted"] and bundle["scenario"]["radius_km"] == 3
    assert bundle["metadata"]["source_manifest"] == source
