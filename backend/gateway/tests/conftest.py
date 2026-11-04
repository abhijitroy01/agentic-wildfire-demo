"""Shared gateway Lambda test fixtures: load each handler from its file, as its Lambda package does."""
import importlib.util
from pathlib import Path

import pytest

GATEWAY = Path(__file__).resolve().parents[1]


@pytest.fixture
def load_handler(monkeypatch):
    """Return a loader that imports a gateway Lambda file afresh, so import-time settings re-read the environment."""
    def load(relative):
        if relative == "weather/lambda_weather_dtn.py":
            for name, value in {"DTN_OPERATION_TIMEOUT_SECONDS": "100", "DTN_REQUEST_TIMEOUT_SECONDS": "15",
                                "DTN_MAX_ATTEMPTS": "2", "DTN_SERIALIZATION_RESERVE_SECONDS": "3"}.items():
                monkeypatch.setenv(name, value)
        name = "gateway_" + relative.removesuffix(".py").replace("/", "_")
        spec = importlib.util.spec_from_file_location(name, GATEWAY / relative)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    return load
