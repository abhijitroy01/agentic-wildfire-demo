"""Workspace API test setup: local storage only, shared real-SUMO fixtures, per-test scratch data."""
import os

# Tests never use AWS storage. Set before anything imports traffic_simulator.settings.
os.environ["STORAGE_MODE"] = "local"
# An in-process worker under test never pre-builds demo areas, which would download from Overpass.
os.environ["DEMO_AREAS_TO_PREBUILD"] = ""

import pytest
from traffic_simulator.testing import grid

__all__ = ["api_app", "demo", "grid", "runtime_binding", "signing_secret", "simulator_data_dir"]


@pytest.fixture
def runtime_binding(monkeypatch):
    """Native transport tests explicitly configure the same pinned binding as the deployment."""
    monkeypatch.setenv("SIMULATION_RUNTIME_ENDPOINT", "RELEASE_A")
    monkeypatch.setenv("SIMULATION_RUNTIME_VERSION", "39")


@pytest.fixture(autouse=True)
def simulator_data_dir(tmp_path, monkeypatch):
    """Redirect simulator scratch data (networks, restored bundles, engine logs) into the test's tmp_path."""
    data = tmp_path / "simulator-data"
    monkeypatch.setattr("traffic_simulator.settings.DATA", data)
    return data


@pytest.fixture
def api_app():
    """Return a test app builder: api_app(store, owner, *routers) serves each router factory for one owner.

    owner is a fixed owner name or an owner dependency; the app answers errors in the v1 envelope.
    """
    from fastapi import FastAPI

    from backend.config_routes import create_config_router
    from backend.http_errors import install_error_handlers

    def build(store, owner, *routers):
        app = FastAPI()
        app.state.integration_store = lambda: store
        app.include_router(create_config_router())
        dependency = owner if callable(owner) else (lambda: owner)
        for router in routers:
            app.include_router(router(dependency))
        install_error_handlers(app)
        return app
    return build


@pytest.fixture
def demo(tmp_path, monkeypatch, api_app):
    """Build a public-demo app (demo sessions, v1, agent, radio, camera and Traffic Lab routes) over a local store."""
    from traffic_simulator.store import Store

    from backend import camera, demo_access, radio_broker
    from backend.evacuation import agent_broker
    from backend.evacuation.routes import create_router
    from backend.investigation.broker import create_investigation_router
    from backend.request_limits import RequestLimits
    from backend.tests.builders import ORIGIN
    from backend.traffic_lab import routes as research

    monkeypatch.setenv("PUBLIC_DEMO_MODE", "true")
    monkeypatch.setenv("FABRIC_PUBLIC_URL", ORIGIN)
    monkeypatch.setenv("SIMULATION_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fixture")
    store = Store(tmp_path)
    app = api_app(store, demo_access.workspace_owner, create_router, create_investigation_router, agent_broker.create_broker_router,
                  radio_broker.create_radio_router, camera.create_camera_router,
                  research.create_research_router)
    app.add_middleware(RequestLimits)
    app.include_router(demo_access.create_demo_router())
    return app, store


@pytest.fixture
def signing_secret(monkeypatch):
    """Configure the context signing secret for one test, with a cold cache around it."""
    from backend import trusted_context
    from backend.tests.builders import SECRET

    monkeypatch.setenv("SIMULATION_CONTEXT_SECRET", SECRET)
    monkeypatch.delenv("SIMULATION_CONTEXT_SECRET_ARN", raising=False)
    trusted_context.clear_secret_cache()
    yield SECRET
    trusted_context.clear_secret_cache()
