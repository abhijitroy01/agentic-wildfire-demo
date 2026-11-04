"""FireGUard API assembly: capability routes and private agent, radio, firetruck, camera and exposure brokers."""
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse
from traffic_simulator import settings as simulator_settings
from traffic_simulator.errors import IntegrationError
from traffic_simulator.http_logging import quiet_http_client_logs
from traffic_simulator.store import Store

from backend import api_settings as settings

# Importing workspace settings validates the public URL and public-access mode before serving requests.
from backend import fire_risk_broker, fire_risk_jobs, security_events, simulation_jobs
from backend.camera import create_camera_router
from backend.config_routes import create_config_router
from backend.demo_access import create_demo_router, workspace_owner
from backend.dependencies import InvestigationReaders
from backend.evacuation import evidence, service
from backend.evacuation.agent_broker import create_broker_router
from backend.evacuation.routes import create_router as create_integration_router
from backend.fire_risk_broker import create_fire_exposure_router
from backend.firetruck_broker import create_firetruck_router
from backend.http_errors import install_error_handlers
from backend.investigation.broker import create_investigation_router
from backend.investigation.saved import create_incident_investigation_router
from backend.radio_broker import create_radio_router
from backend.request_limits import RequestLimits
from backend.traffic_lab.routes import create_research_router

# /sources feed requests (and, locally, the in-process worker) send provider keys in request URLs.
quiet_http_client_logs()
# uvicorn configures only its own loggers; security events need their own handler to reach the container log.
security_events.configure_logging()
VERSION = "0.2.0"
store = Store()
stop = threading.Event()


@asynccontextmanager
async def lifespan(_):
    """Run durable fire-risk preparation, plus the local SUMO worker, independently of HTTP requests."""
    fire_risk_broker.validate_settings()
    stop.clear()
    risk_thread = threading.Thread(target=fire_risk_jobs.loop, args=(store, stop), daemon=True,
                                   name="fire-risk-preparation")
    risk_thread.start()
    # Local development runs the SUMO worker in-process; AWS runs it as its own service.
    thread = None
    if not simulator_settings.CLOUD:
        from traffic_simulator.worker import loop
        thread = threading.Thread(target=loop, args=(stop,), daemon=True)
        thread.start()
    yield
    stop.set()
    risk_thread.join(timeout=10)
    if thread:
        thread.join(timeout=10)


# AWS mode serves no interactive docs or schema routes (/docs, /redoc, /openapi.json): the published contract is
# schemas/simulation/openapi.json, and /docs loads Swagger UI from a third-party CDN. Local development keeps them.
DOCS_ROUTES = {"docs_url": None, "redoc_url": None, "openapi_url": None} if simulator_settings.CLOUD else {}
app = FastAPI(title="FireGUard Workspace API", version=VERSION, lifespan=lifespan, **DOCS_ROUTES)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.state.integration_store = lambda: store
app.include_router(create_config_router())
app.include_router(create_integration_router(workspace_owner))
app.include_router(create_investigation_router(workspace_owner))
app.include_router(create_fire_exposure_router(workspace_owner))
app.include_router(create_broker_router(workspace_owner))
app.include_router(create_incident_investigation_router(workspace_owner, InvestigationReaders(
    context=service.context, owned_job=simulation_jobs.owned_job,
    build_evidence=evidence.build_evidence, planning_summary=evidence.planning_summary)))
app.include_router(create_demo_router())
app.include_router(create_radio_router(workspace_owner))
app.include_router(create_firetruck_router(workspace_owner))
app.include_router(create_camera_router(workspace_owner))
app.include_router(create_research_router(workspace_owner))
app.add_middleware(RequestLimits)
install_error_handlers(app)


@app.middleware("http")
async def response_headers(request, call_next):
    """Add security headers (nosniff, referrer policy) to every API response."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Public map tiles require a Referer. Cross-origin requests disclose only
    # the app origin, never coordinates, job paths, or authentication queries.
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
def health():
    return {"status": "ok", "version": VERSION}


@app.get("/", include_in_schema=False)
def index():
    """The UI is served by the FireGUard workspace distribution; send old bookmarks there."""
    workspace = settings.public_url()
    if not workspace.startswith("https://"):
        raise IntegrationError("UI_NOT_SERVED", "This origin serves the workspace API only.", 404)
    return RedirectResponse(workspace + "/", status_code=307)
