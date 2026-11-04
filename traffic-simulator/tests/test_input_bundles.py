import copy
import hashlib

import pytest
import sumolib
from traffic_simulator import input_bundles, network_preparation
from traffic_simulator.errors import IntegrationError
from traffic_simulator.models import Scenario
from traffic_simulator.persistence import digest
from traffic_simulator.simulation import demand_for, simulate


def test_frozen_population_is_a_stable_prefix_and_departures_use_unrounded_fractions(grid, monkeypatch):
    scenario = Scenario(vehicles=20, crews=2, radius_km=3, duration_minutes=5, departure_minutes=1)
    grid[7]["net_sha256"] = hashlib.sha256(grid[0].read_bytes()).hexdigest()
    bundle = input_bundles.freeze_inputs(scenario, grid, {"fixture": "not live data"}, None)
    original = demand_for(scenario, *grid[1:6])
    assert input_bundles.demand_for_bundle(bundle, scenario) == original
    extended = input_bundles.demand_for_bundle(bundle, scenario.model_copy(update={"vehicles": 60}))
    assert extended[0][:20] == original[0] and extended[1] == original[1]
    changed = input_bundles.demand_for_bundle(bundle, scenario.model_copy(update={"departure_minutes": 3}))
    for a, b in zip(original[0], changed[0], strict=True):
        assert a["route"] == b["route"] and a["origin"] == b["origin"]
        assert b["depart"] == round(a["departure_fraction"]*180, 1)
    monkeypatch.setattr("traffic_simulator.network.discover", lambda *_: pytest.fail("Variant refetched network"))
    restored = input_bundles.restore(bundle)
    assert restored[0].read_bytes() == grid[0].read_bytes()
    assert [e.getID() for e in restored[2]] == [e.getID() for e in grid[2]]
    assert [e.getID() for e in restored[4]] == [e.getID() for e in grid[4]]


def test_tampered_or_incompatible_bundle_is_rejected(grid):
    scenario = Scenario(vehicles=20, crews=0, radius_km=3, duration_minutes=5, departure_minutes=1)
    grid[7]["net_sha256"] = hashlib.sha256(grid[0].read_bytes()).hexdigest()
    bundle = input_bundles.freeze_inputs(scenario, grid, {}, None)
    bad = copy.deepcopy(bundle)
    bad["population"][0]["depart"] += 1
    with pytest.raises(IntegrationError) as error:
        input_bundles.validate_bundle(bad)
    assert error.value.code == "BUNDLE_INTEGRITY_ERROR"
    with pytest.raises(IntegrationError) as error:
        input_bundles.demand_for_bundle(bundle, scenario.model_copy(update={"crews": 1}))
    assert error.value.code == "UNSUPPORTED_VARIANT"


@pytest.mark.parametrize("kind", ["frozen run inputs", "prepared road network"])
def test_a_sealed_network_that_cannot_be_rebuilt_leaves_no_folder_in_the_data_dir(grid, simulator_data_dir, kind):
    """A correctly sealed bundle naming an edge its network lacks fails to load, and its scratch folder is removed."""
    scenario = Scenario(vehicles=20, crews=0, radius_km=3, duration_minutes=5, departure_minutes=1)
    grid[7]["net_sha256"] = hashlib.sha256(grid[0].read_bytes()).hexdigest()
    if kind == "frozen run inputs":
        content = input_bundles.freeze_inputs(scenario, grid, {}, None)
    else:
        content = {"version": 1, "engine_version": sumolib.__version__, "scenario": scenario.model_dump(),
                   **input_bundles.freeze_network(grid), "validation": {}}
    content = {key: value for key, value in content.items() if key != "sha256"}
    content["edge_ids"] = [*content["edge_ids"], "edge-missing-from-the-network"]
    bundle = {**content, "sha256": digest(content)}
    with pytest.raises(KeyError, match="edge-missing-from-the-network"):
        if kind == "frozen run inputs":
            input_bundles.restore(bundle)
        else:
            network_preparation.restore(bundle, scenario)
    assert list(simulator_data_dir.iterdir()) == []


@pytest.mark.integration
def test_real_sumo_replays_a_frozen_bundle_without_changing_policy_outcomes(grid):
    scenario = Scenario(vehicles=20, crews=2, radius_km=3, duration_minutes=5, departure_minutes=1,
                        hazard_radius_m=0, hazard_growth_m_min=0, policies=["baseline", "coordinated", "phased"])
    grid[7]["net_sha256"] = hashlib.sha256(grid[0].read_bytes()).hexdigest()
    bundle = input_bundles.freeze_inputs(scenario, grid, {}, None)
    direct = simulate(scenario, grid, lambda *_: None, lambda: None)
    replay = simulate(scenario, input_bundles.restore(bundle), lambda *_: None, lambda: None,
                      demand=input_bundles.demand_for_bundle(bundle, scenario))
    assert direct["policies"] == replay["policies"]
