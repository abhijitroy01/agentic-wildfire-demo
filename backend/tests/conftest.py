"""API fixtures scoped to this suite; no nested pytest plugin registration."""
import importlib

import pytest

from backend.tests.fixtures import api_app, demo, grid, runtime_binding, signing_secret, simulator_data_dir

__all__ = ["api_app", "demo", "grid", "runtime_binding", "signing_secret", "simulator_data_dir"]


@pytest.fixture(autouse=True)
def inline_fire_risk_transport(monkeypatch):
    """Job protocol tests isolate provider calls; actual spawn/cancellation has its own transport suite."""
    from traffic_simulator.transport_work import TransportError, failure

    from backend import fire_risk_jobs, fire_risk_transport

    def run(module_name, operation, payload, maximum_bytes, check_cancel):
        check_cancel()
        try:
            value = getattr(importlib.import_module(module_name), operation)(payload)
        except Exception as exc:  # noqa: BLE001 — reproduce the production transport's safe protocol boundary
            raise TransportError(**failure(fire_risk_transport.transport_failure(exc, operation))) from None
        check_cancel()
        return value

    monkeypatch.setattr(fire_risk_jobs, "run_transport", run)
