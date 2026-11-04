"""Simulator test setup: local storage only, shared real-SUMO fixtures, per-test scratch data."""
import os

# Tests never use AWS storage. Set before anything imports traffic_simulator.settings.
os.environ["STORAGE_MODE"] = "local"
# A worker loop under test never pre-builds demo areas, which would download regional sources; warm-up tests set
# DEMO_AREAS_TO_PREBUILD themselves.
os.environ["DEMO_AREAS_TO_PREBUILD"] = ""

import pytest

pytest_plugins = ["traffic_simulator.testing"]


@pytest.fixture(autouse=True)
def simulator_data_dir(tmp_path, monkeypatch):
    """Redirect simulator scratch data (networks, restored bundles, engine logs) into the test's tmp_path."""
    data = tmp_path / "simulator-data"
    monkeypatch.setattr("traffic_simulator.settings.DATA", data)
    return data
