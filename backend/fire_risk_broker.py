"""Enrich completed, source-backed chat answers once; owner-scoped reads have no provider side effects."""
import json
import logging
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

import httpx
from boto3.dynamodb.types import TypeSerializer
from fastapi import APIRouter, Depends, Request
from fire_risk_modeler.models import (
    AssessmentResult,
    ExposureParameters,
    FireObservation,
    SurfaceFireScenario,
    WeatherObservation,
    build_unavailable_result,
    model_version,
)
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import commit, digest, dynamo_value, owned, record

from backend.budgets import spend_daily
from backend.dependencies import store_for

logger = logging.getLogger(__name__)
# DynamoDB's item maximum is 400 KiB. Reserve headroom for attribute/storage representation; this protocol
# envelope is independent of operational geometry/response bounds declared in deployment/settings.toml.
MAX_RETAINED_BYTES = 350 * 1024
_calculations = None
_calculations_lock = threading.Lock()


def api_settings():
    from backend import api_settings
    return api_settings


def calculation_executor():
    """Create one process-wide bounded executor from the same declared capacity as the private modeler."""
    global _calculations
    with _calculations_lock:
        if _calculations is None:
            _calculations = ThreadPoolExecutor(
                max_workers=api_settings().fire_risk_limit("calculations_per_process"),
                thread_name_prefix="fire-risk",
            )
        return _calculations


def retained_size(document):
    """Conservatively bound the serialized DynamoDB attribute representation, including nested values."""
    attributes = {key: TypeSerializer().serialize(dynamo_value(value)) for key, value in document.items()}
    return len(json.dumps(attributes, ensure_ascii=False, separators=(",", ":")).encode())


def bound_assessment_record(document, request):
    """Keep one copy of provenance and refuse oversized geometry without losing the source fire point."""
    if retained_size(document) <= MAX_RETAINED_BYTES:
        return document
    fallback = build_unavailable_result(request, ["Assessment exceeds the retained evidence size limit."])
    if fallback.fire.perimeter is not None:
        fallback = fallback.model_copy(update={"fire": fallback.fire.model_copy(update={"perimeter": None})})
        fallback.warnings.append("Reported perimeter omitted from this retained unavailable result.")
    document = {**document, "result": fallback.model_dump(mode="json")}
    if retained_size(document) > MAX_RETAINED_BYTES:
        raise IntegrationError("EXPOSURE_RECORD_TOO_LARGE", "Fire exposure evidence is too large to retain.", 503)
    return document


def assessment_record(request, user, result):
    """Result owns fire/weather/scenario provenance; never store a second full request/geometry copy."""
    return bound_assessment_record(record(
        "fire_exposure", request.assessment_id, user, input_hash=request.input_hash, mode=request.mode,
        parameters=request.parameters.model_dump(mode="json"), result=result.model_dump(mode="json"),
    ), request)


def finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def distance_km(first, second):
    """Haversine distance handles dateline and zero coordinates without an extra API-image dependency."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*first, *second))
    value = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 12742 * math.asin(math.sqrt(min(1, max(0, value))))


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.isoformat() if parsed.utcoffset() is not None else None
    except ValueError:
        return None


def weather_value(weather, name):
    item = weather.get(name)
    return item if isinstance(item, dict) else {}


def normalize_weather(raw, fire):
    """Use only finite source measurements with explicit units, coordinate provenance and freshness."""
    if not isinstance(raw, dict):
        return None
    query = weather_value(raw, "queryLocation")
    lat, lon = query.get("latitude"), query.get("longitude")
    if not finite(lat) or not finite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        return None
    if distance_km((lat, lon), (fire.latitude, fire.longitude)) > api_settings().fire_risk_limit(
            "weather_max_distance_km"):
        return None
    observed = timestamp(raw.get("timestamp"))
    age = (datetime.now(UTC)-datetime.fromisoformat(observed)).total_seconds() if observed else None
    fresh = age is not None and -300 <= age <= api_settings().fire_risk_limit("weather_max_age_seconds")
    wind = weather_value(raw, "wind")
    speed, direction = weather_value(wind, "speed"), weather_value(wind, "direction")
    units = {"mph": 0.44704, "km/h": 1/3.6, "kph": 1/3.6, "m/s": 1.0}
    value = speed.get("value")
    velocity = value * units[speed["unit"]] if finite(value) and speed.get("unit") in units else None
    if velocity is not None and not 0 <= velocity <= 150:
        velocity = None
    bearing = direction.get("value")
    if not finite(bearing) or not 0 <= bearing <= 360 or direction.get("unit") not in (
            "degrees", "degree", "deg"):
        bearing = None
    temperature = weather_value(raw, "temperature")
    degrees = temperature.get("value")
    if not finite(degrees):
        degrees = None
    elif temperature.get("unit") in ("F", "°F", "degF"):
        degrees = (degrees-32)*5/9
    elif temperature.get("unit") not in ("C", "°C", "degC"):
        degrees = None
    humidity = weather_value(raw, "relativeHumidity")
    percent = humidity.get("value")
    if not finite(percent) or not 0 <= percent <= 100 or humidity.get("unit") not in ("percent", "%"):
        percent = None
    return WeatherObservation(
        query_latitude=float(lat), query_longitude=float(lon), observed_at=observed,
        availability="available" if fresh else "stale",
        wind_speed_mps=float(velocity) if velocity is not None else None,
        wind_from_degrees=float(bearing) if bearing is not None else None,
        temperature_c=float(degrees) if degrees is not None and -100 <= degrees <= 100 else None,
        relative_humidity_percent=float(percent) if percent is not None else None,
    )


def fetch_weather(fires, deadline, operation_id=None):
    """One IAM-authenticated invocation of the existing DTN batch adapter; no provider key leaves Lambda."""
    import boto3
    from botocore.config import Config
    function = api_settings().weather_function()
    if not function or time.time() >= deadline:
        return []
    timeout = min(api_settings().fire_risk_limit("weather_timeout_seconds"), max(0.1, deadline-time.time()))
    from traffic_simulator import settings as storage_settings
    client = boto3.client("lambda", region_name=storage_settings.REGION or None, config=Config(
        connect_timeout=timeout, read_timeout=timeout, retries={"total_max_attempts": 1}))
    payload = {"locations": [{"latitude": fire.latitude, "longitude": fire.longitude} for fire in fires]}
    if operation_id is not None:
        payload["operationId"] = str(UUID(operation_id))
    response = client.invoke(FunctionName=function, InvocationType="RequestResponse", Payload=json.dumps(payload).encode())
    body = response["Payload"]
    try:
        content = body.read(int(api_settings().fire_risk_limit("response_max_kib"))*1024+1)
    finally:
        body.close()
    if response.get("FunctionError") or len(content) > api_settings().fire_risk_limit("response_max_kib")*1024:
        raise ValueError("Weather source unavailable")
    result = json.loads(content)
    if "body" in result:
        result = json.loads(result["body"]) if isinstance(result["body"], str) else result["body"]
    return result.get("locations", []) if isinstance(result, dict) else []


def parameters():
    return ExposureParameters(
        distance_bands_km=[float(api_settings().fire_risk_limit(name)) for name in ("inner_km", "middle_km", "outer_km")],
        ring_points=api_settings().fire_risk_limit("ring_points"),
        wind_shape_scale=float(api_settings().fire_risk_limit("wind_shape_scale")),
        max_wind_stretch=float(api_settings().fire_risk_limit("max_wind_stretch")),
    )


def scenario():
    return SurfaceFireScenario(
        fuel_model=api_settings().fire_risk_limit("surface_fuel_model"),
        moisture_profile=api_settings().fire_risk_moisture_profile(), slope_degrees=0.0,
        wind_height_assumption="DTN wind treated as 10 m above ground; open unsheltered fuel assumed",
        initialization="reported point treated as demo ignition",
        horizons_minutes=[float(api_settings().fire_risk_limit(name)) for name in (
            "surface_first_minutes", "surface_second_minutes", "surface_third_minutes")],
    )


def validate_settings():
    """Fail API startup on malformed operational settings before a completed answer needs them."""
    parameters()
    scenario()
    for name in ("assessments_per_day", "weather_calls_per_day", "max_fires_per_turn",
                 "postprocessing_seconds", "weather_timeout_seconds", "response_max_kib",
                 "calculation_timeout_seconds", "calculations_per_process", "weather_max_age_seconds",
                 "weather_max_distance_km", "landscape_request_max_kib", "landscape_max_cells",
                 "landscape_directional_radius", "landscape_calculation_timeout_seconds",
                 "landscape_acquisition_timeout_seconds", "landscape_source_max_mib", "landscape_initial_radius_m",
                 "landscape_idle_timeout_seconds", "landscape_max_attempts", "retry_max_seconds",
                 "model_admission_seconds"):
        api_settings().fire_risk_limit(name)
    maximum = (int(api_settings().fire_risk_limit("landscape_max_cells") ** 0.5)-2)*30/2
    if api_settings().fire_risk_limit("landscape_initial_radius_m") > maximum:
        raise ValueError("Initial landscape extent exceeds the configured cell bound including its halo")


def invoke_model(request, deadline):
    binding = api_settings().fire_risk_service()
    if not binding:
        if request.mode == "landscape":
            raise IntegrationError("MODELER_NOT_CONFIGURED", "Fire risk modeler is not configured.", 503)
        return build_unavailable_result(request, ["Fire risk modeler is not configured."])
    url, key = binding
    remaining = deadline-time.time()
    if remaining <= 0:
        if request.mode == "landscape":
            raise IntegrationError("CALCULATION_LIMIT", "Fire risk calculation did not complete.", 503)
        return build_unavailable_result(request, ["Assessment time budget reached."])
    landscape = request.mode == "landscape"
    configured = api_settings().fire_risk_limit(
        "landscape_calculation_timeout_seconds" if landscape else "calculation_timeout_seconds")
    timeout = min(configured, remaining) if configured else None
    endpoint = "/v1/landscape/assessments" if landscape else "/v1/exposure/assessments"
    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client, client.stream(
            "POST", url+endpoint, json=request.model_dump(mode="json"),
            headers={"Authorization": "Bearer "+key}) as response:
        response.raise_for_status()
        chunks, count = [], 0
        for chunk in response.iter_bytes():
            count += len(chunk)
            if count > api_settings().fire_risk_limit("response_max_kib")*1024 or time.time() >= deadline:
                raise ValueError("Assessment response exceeded its bound")
            chunks.append(chunk)
    result = AssessmentResult.model_validate_json(b"".join(chunks))
    if (result.assessment_id != request.assessment_id or result.fire_ref != request.fire_ref
            or result.input_hash != request.input_hash or result.fire != request.fire
            or result.weather != request.weather or result.scenario != request.scenario
            or result.model_version != model_version(request.mode)
            or (request.mode == "surface_fire") != (result.mode == "surface_fire")
            or (request.mode == "landscape") != (result.mode == "landscape")):
        raise ValueError("Modeler response does not match frozen source inputs")
    if landscape and result.landscape and (
            result.landscape.manifest != request.landscape.manifest
            or result.landscape.directional_radius != request.scenario.directional_radius):
        raise ValueError("Modeler response does not match the sealed landscape")
    return result


def assess_once(store, user, request, deadline):
    """Reserve the immutable intent before billing/calling providers; duplicate execution reads that intent."""
    identifier = request.assessment_id
    prior = store.get("fire_exposure:"+identifier)
    if prior:
        if prior.get("subject") != user or prior.get("input_hash") != request.input_hash:
            raise IntegrationError("IDEMPOTENCY_CONFLICT", "Assessment inputs changed.", 409)
        return prior["result"]
    fallback = build_unavailable_result(request, ["Assessment did not complete within this answer."])
    receipt = assessment_record(request, user, fallback)
    try:
        commit(store, [(receipt, None)])
    except IntegrationError as exc:
        if exc.code != "REVISION_CONFLICT":
            raise
        return owned(store, "fire_exposure", identifier, user)["result"]
    try:
        if time.time() >= deadline:
            result = fallback
        else:
            spend_daily(store, "fire_risk_assessments", api_settings().fire_risk_limit("assessments_per_day"),
                        "Fire exposure assessment budget reached.")
            result = invoke_model(request, deadline)
    except Exception as exc:  # noqa: BLE001 — preserve fire observations and keep provider details server-side
        logger.warning("Fire exposure unavailable: reason=%s", type(exc).__name__)
        result = build_unavailable_result(request, ["Fire exposure calculation is unavailable."])
    # A terminal answer can fence a slow request by finalizing its placeholder first.
    if time.time() >= deadline:
        result = fallback
    receipt.update(revision=2, result=result.model_dump(mode="json"))
    receipt = bound_assessment_record(receipt, request)
    try:
        commit(store, [(receipt, 1)])
    except IntegrationError as exc:
        if exc.code != "REVISION_CONFLICT":
            raise
        return owned(store, "fire_exposure", identifier, user)["result"]
    return receipt["result"]


def observed_fires(content):
    """Derive coordinates only from validated tool-backed mapData; the chat search center is never an ignition."""
    locations = (content.get("mapData") or {}).get("locations") or []
    selected, seen = [], set()
    for location in locations:
        fire = location.get("fire")
        if not isinstance(fire, dict):
            continue
        lat, lon = location.get("latitude"), location.get("longitude")
        if (not finite(lat) or not finite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180):
            continue
        identity = digest([fire.get("sourceId"), lat, lon])
        if identity in seen:
            continue
        seen.add(identity)
        observation = FireObservation(
            latitude=float(lat), longitude=float(lon), name=fire.get("name"),
            source_id=fire.get("sourceId"), source_timestamp=timestamp(fire.get("lastUpdated")))
        selected.append((identity, observation, location))
        if len(selected) >= api_settings().fire_risk_limit("max_fires_per_turn"):
            break
    return selected


def enrich_answer(store, user, session_id, attempt, content, deadline):
    """Attach durable background references; new answers never fabricate distance or flat-terrain results."""
    from backend.fire_risk_jobs import enqueue
    content = deepcopy(content)
    content.pop("fireExposureError", None)
    content["fireExposureData"] = []
    content["fireExposureJobs"] = enqueue(store, user, session_id, attempt, content)
    return content


def create_fire_exposure_router(owner_dependency):
    from backend.fire_risk_contracts import FireRiskJobResponse, FireRiskRetryRequest
    from backend.fire_risk_jobs import read_job, retry_job
    router = APIRouter()

    @router.get("/api/v1/fire-exposure/jobs/{job_id}", response_model=FireRiskJobResponse)
    def preparation_job(job_id: UUID, request: Request, user: str = Depends(owner_dependency)):
        return read_job(store_for(request), job_id, user)

    @router.post("/api/v1/fire-exposure/jobs/{job_id}/cancel", response_model=FireRiskJobResponse)
    def cancel_preparation(job_id: UUID, request: Request, user: str = Depends(owner_dependency)):
        store = store_for(request)
        read_job(store, job_id, user)
        store.cancel_run(str(job_id))
        return read_job(store, job_id, user)

    @router.post("/api/v1/fire-exposure/jobs/{job_id}/retry", response_model=FireRiskJobResponse, status_code=202)
    def retry_preparation(job_id: UUID, body: FireRiskRetryRequest, request: Request,
                          user: str = Depends(owner_dependency)):
        return retry_job(store_for(request), job_id, user, body.request_id)

    @router.get("/api/v1/fire-exposure/assessments/{assessment_id}", response_model=AssessmentResult)
    def retained_assessment(assessment_id: str, request: Request, user: str = Depends(owner_dependency)):
        receipt = owned(store_for(request), "fire_exposure", assessment_id, user)
        if receipt["expires"] <= time.time():
            raise IntegrationError("NOT_FOUND", "Fire exposure assessment not found.", 404)
        return receipt["result"]

    return router
