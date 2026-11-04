"""Authenticated, bounded, stateless exposure calculations; persistence belongs to the API."""
import asyncio
import hmac
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from fire_risk_modeler import settings
from fire_risk_modeler.exposure import assess_exposure
from fire_risk_modeler.geometry import vertex_count
from fire_risk_modeler.models import (
    AssessmentResult,
    ExposureAssessmentRequest,
    LandscapeAssessmentRequest,
    build_unavailable_result,
    model_input_hash,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.validate()
    app.state.active_calculations = 0
    app.state.executor = ThreadPoolExecutor(max_workers=settings.limit("FIRE_RISK_CALCULATIONS_PER_PROCESS"))
    try:
        yield
    finally:
        # A disconnected/timed-out client must not release a slot while its calculation is still running.
        # Operations are additionally bounded by vertices and ring counts; no network I/O occurs in this pool.
        app.state.executor.shutdown(wait=True, cancel_futures=True)


def error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def authenticate(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {settings.service_key()}"
    if not hmac.compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="MODEL_AUTH_REQUIRED")


def calculate(body: ExposureAssessmentRequest | LandscapeAssessmentRequest) -> AssessmentResult:
    if body.mode == "landscape":
        from fire_risk_modeler.landscape_spread import assess_landscape
        return assess_landscape(body, maximum_seconds=settings.limit("FIRE_RISK_LANDSCAPE_CALCULATION_TIMEOUT_SECONDS"))
    if body.mode == "surface_fire":
        from fire_risk_modeler.surface_fire import assess_surface_fire
        return assess_surface_fire(body)
    return assess_exposure(body)


def create_app() -> FastAPI:
    app = FastAPI(title="Fire exposure modeler", version="1", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError):
        # The default handler repeats invalid input; emit a bounded contract error without submitted data.
        return error("MODEL_INPUT_INVALID", "Model input does not match its declared contract.", 422)

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        if exc.status_code == 401:
            return error("MODEL_AUTH_REQUIRED", "A valid private service credential is required.", 401)
        return error("MODEL_REQUEST_FAILED", "Model request could not complete.", exc.status_code)

    @app.middleware("http")
    async def bounded_body(request: Request, call_next):
        if request.method != "POST":
            return await call_next(request)
        try:
            authenticate(request.headers.get("authorization", ""))
        except HTTPException:
            return error("MODEL_AUTH_REQUIRED", "A valid private service credential is required.", 401)
        landscape = request.url.path == "/v1/landscape/assessments"
        maximum = settings.limit(
            "FIRE_RISK_LANDSCAPE_REQUEST_MAX_KIB" if landscape else "FIRE_RISK_REQUEST_BODY_MAX_KIB"
        ) * 1024
        body = bytearray()
        try:
            body_timeout = settings.limit(
                "FIRE_RISK_LANDSCAPE_CALCULATION_TIMEOUT_SECONDS" if landscape
                else "FIRE_RISK_CALCULATION_TIMEOUT_SECONDS"
            )
            async with asyncio.timeout(body_timeout or None):
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > maximum:
                        return error("MODEL_BODY_TOO_LARGE", "Model request exceeds its declared size limit.", 413)
        except TimeoutError:
            return error("MODEL_REQUEST_TIMEOUT", "Model request body exceeded its deadline.", 408)
        request._body = bytes(body)
        return await call_next(request)

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "1"}

    async def execute(body: ExposureAssessmentRequest | LandscapeAssessmentRequest):
        if model_input_hash(body) != body.input_hash:
            return error("MODEL_INPUT_HASH_MISMATCH", "Frozen model inputs do not match their hash.", 409)
        if body.mode == "landscape":
            manifest = body.landscape.manifest
            if (manifest.width * manifest.height > settings.limit("FIRE_RISK_LANDSCAPE_MAX_CELLS")
                    or body.scenario.directional_radius > settings.limit("FIRE_RISK_LANDSCAPE_DIRECTIONAL_RADIUS")):
                return error("MODEL_INPUT_LIMIT", "Landscape calculation exceeds its declared grid limits.", 422)
        elif body.parameters.ring_points > settings.limit("FIRE_RISK_RING_POINTS"):
            return error("MODEL_INPUT_LIMIT", "Requested ring resolution exceeds the declared limit.", 422)
        if body.fire.perimeter and vertex_count(body.fire.perimeter) > settings.limit("FIRE_RISK_PERIMETER_MAX_VERTICES"):
            return error("MODEL_INPUT_LIMIT", "Reported perimeter exceeds the declared vertex limit.", 422)
        maximum = settings.limit("FIRE_RISK_CALCULATIONS_PER_PROCESS")
        if app.state.active_calculations >= maximum:
            response = error("MODEL_BUSY", "Model calculation capacity is temporarily full.", 429)
            response.headers["Retry-After"] = "1"
            return response
        app.state.active_calculations += 1
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(app.state.executor, calculate, body)

        def release(_completed):
            app.state.active_calculations -= 1
        future.add_done_callback(release)
        try:
            timeout = settings.limit(
                "FIRE_RISK_LANDSCAPE_CALCULATION_TIMEOUT_SECONDS" if body.mode == "landscape"
                else "FIRE_RISK_CALCULATION_TIMEOUT_SECONDS"
            )
            async with asyncio.timeout(timeout or None):
                result = await asyncio.shield(future)
        except TimeoutError:
            return error("MODEL_CALCULATION_TIMEOUT", "Model calculation exceeded its deadline.", 503)
        except ValueError:
            if body.mode == "landscape":
                return error("MODEL_CALCULATION_FAILED", "Landscape numerical input could not be calculated.", 503)
            result = build_unavailable_result(body, ["Source geometry could not be used for this calculation."])
        except Exception as exc:  # noqa: BLE001 — submitted/provider/native details must stay off the wire
            logger.warning("Model calculation unavailable: %s", type(exc).__name__)
            return error("MODEL_UNAVAILABLE", "Model calculation is unavailable.", 503)
        encoded = json.dumps(result.model_dump(mode="json"), allow_nan=False, separators=(",", ":")).encode()
        if len(encoded) > settings.limit("FIRE_RISK_RESPONSE_MAX_KIB") * 1024:
            return error("MODEL_RESULT_TOO_LARGE", "Model result exceeds its declared size limit.", 503)
        return JSONResponse(result.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.post("/v1/exposure/assessments", response_model=AssessmentResult, dependencies=[Depends(authenticate)])
    async def assess(body: ExposureAssessmentRequest):
        return await execute(body)

    @app.post("/v1/landscape/assessments", response_model=AssessmentResult, dependencies=[Depends(authenticate)])
    async def assess_landscape(body: LandscapeAssessmentRequest):
        return await execute(body)

    return app


app = create_app()
