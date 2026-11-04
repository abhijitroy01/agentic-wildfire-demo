"""Read-only Delphire camera broker: the configured device's latest records and the one image they name.

This is the browser's only way to the camera (owner brief 2026-10-14, camera through the broker). The broker calls
the configured HTTPS origin, with the key only in an Authorization header, and makes exactly two requests:
GET /devices/{device_id}/latest and GET /images/{id}?type=display, following no redirect and using no proxy. It
returns only the fields the camera modal shows, serves an image only when its ID is the image record of the latest
records this process holds, as WebP or PNG within capacity.camera_image_max_kib, and refuses any Delphire answer
that contains the key. Each API process reuses an answer for every visitor for cache.camera_cache_seconds, spends
budgets.camera_calls_per_day for every Delphire call (traffic_simulator.provider_calls, the TomTom mechanism),
shares one call among concurrent requests and backs off after failures. Visitors need a demo session.

It stores no camera data (its only write is the shared daily budget counter), controls no device and opens no
stream. Without [camera] device_id and DELPHIRE_API_KEY both routes answer 503 CAMERA_NOT_CONFIGURED.
"""
import asyncio
import copy
import functools
import json
import logging
import math
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, Path
from fastapi.responses import Response
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from traffic_simulator.errors import IntegrationError
from traffic_simulator.provider_calls import ProviderCalls

from backend import api_settings as settings
from backend import budgets
from backend.dependencies import store_for

logger = logging.getLogger(__name__)

# Protocol bounds, not operational settings (D38): the largest latest-records body read from Delphire, in decoded
# bytes. Real answers are a few kilobytes; the bound stops a runaway body before json.loads.
LATEST_MAX_BYTES = 1024 * 1024
# Protocol bounds, not operational settings (D38): how much of one latest answer the projection examines.
# Delphire returns about ten alias groups with one image or reading and one event record each.
MAX_GROUPS = 32           # only the first 32 alias groups are examined; the rest are ignored
MAX_RECORDS = 20          # only the first 20 records of a group
MAX_CLAIMS = 20           # only the first 20 claims of an image record
MAX_DETECTIONS = 50       # a detection claim with more entries is dropped
# Protocol bounds, not operational settings (D38): the pause after a key's first failed Delphire call, doubled for
# each further consecutive failure up to the maximum. A failing key costs at most 11 calls per process in an hour.
FAILURE_BACKOFF_FIRST_SECONDS = 10
FAILURE_BACKOFF_MAX_SECONDS = 600
# A Delphire record ID as visitors may name it: no "." or "/", so no dot segment or extra path segment can form.
IMAGE_ID = r"^[A-Za-z0-9_-]{1,64}$"
IMAGE_MAGIC = {"image/png": lambda body: body.startswith(b"\x89PNG\r\n\x1a\n"),
               "image/webp": lambda body: body[:4] == b"RIFF" and body[8:12] == b"WEBP"}
USER_AGENT = "FireGuard-camera-broker/1"
# Set on the image response itself, so they hold in any app that mounts the router.
IMAGE_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                 "Content-Security-Policy": "default-src 'none'; sandbox"}
NOT_CONFIGURED = "The camera is not configured for this demonstration."
BUDGET_MESSAGE = ("Daily demo limit reached for camera requests. Try again tomorrow; the limit resets at "
                  "midnight UTC.")
UNAVAILABLE = "The camera service is unavailable. Try again shortly."
INVALID = "The camera service returned an unusable response."
IMAGE_NOT_FOUND = "That camera image is not in the latest camera records."
IMAGE_UNSUPPORTED = "The camera image is not a WebP or PNG image."
IMAGE_TOO_LARGE = "The camera image is larger than this demonstration accepts."


def not_configured() -> IntegrationError:
    return IntegrationError("CAMERA_NOT_CONFIGURED", NOT_CONFIGURED, 503)


def unavailable() -> IntegrationError:
    return IntegrationError("CAMERA_UNAVAILABLE", UNAVAILABLE, 503, retryable=True)


def invalid() -> IntegrationError:
    return IntegrationError("CAMERA_INVALID", INVALID, 502, retryable=True)


def image_unsupported() -> IntegrationError:
    return IntegrationError("CAMERA_IMAGE_UNSUPPORTED", IMAGE_UNSUPPORTED, 502)


def image_too_large() -> IntegrationError:
    return IntegrationError("CAMERA_IMAGE_TOO_LARGE", IMAGE_TOO_LARGE, 502)


def failed(kind: str, reason: str, error: IntegrationError) -> IntegrationError:
    """Log a failed Delphire call by its kind and reason (a status or an exception type), never its text."""
    logger.warning("Camera %s call failed (%s)", kind, reason)
    return error


def refused(kind: str, reason: str, error: IntegrationError) -> IntegrationError:
    """Log a Delphire answer the broker refuses, by its kind and the rule it broke, never its content."""
    logger.warning("Camera %s answer refused (%s)", kind, reason)
    return error


# --- The projection: the only Delphire fields that can leave the API ------------------------------------------


class Vendor(BaseModel):
    """A Delphire value the camera modal shows. Unknown fields are dropped and numbers must be finite.

    The broker validates each record or claim strictly (model_validate(value, strict=True)): strings, booleans,
    NaN and infinity are not numbers. The same models are the latest route's response model.
    """
    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)


Probability = Annotated[float, Field(ge=0, le=1)]


def printable(value: str) -> str:
    """Refuse a detection class with control characters."""
    if not value.isprintable():
        raise ValueError("A detection class must be printable")
    return value


def utc_seconds(value: str) -> str:
    """Normalize an ISO 8601 time with a zone to UTC seconds ("2026-10-14T20:15:03Z"); refuse one without a zone."""
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError("A record time must carry its zone")
        return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    except OverflowError:
        raise ValueError("A record time is out of range") from None


def round4(value: float) -> float:
    """Round a coordinate to the 4 decimals the modal shows (about 11 m)."""
    return round(value, 4)


def finite(value: object, low: float, high: float) -> bool:
    """True for an int or float (not a boolean) that is finite and inside [low, high]."""
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def boxes_or_none(value: object) -> object:
    """Keep bounding boxes only when every box is four finite pixel values in 0-100000, else none."""
    if isinstance(value, list) and all(isinstance(box, list) and len(box) == 4 and all(
            finite(item, 0, 100000) for item in box) for box in value):
        return value
    return []


def measurement(low: float, high: float) -> BeforeValidator:
    """A sensor value kept when it is a finite number in [low, high]; any other value becomes null."""
    return BeforeValidator(lambda value: value if finite(value, low, high) else None)


class Detection(Vendor):
    """A detection claim's classes with one confidence each, and pixel boxes [x_center, y_center, width, height]."""
    classes: Annotated[list[Annotated[str, Field(min_length=1, max_length=40), AfterValidator(printable)]],
                       Field(min_length=1, max_length=MAX_DETECTIONS)]
    confidences: list[Probability]
    boundingBoxes: Annotated[list[list[float]], BeforeValidator(boxes_or_none)] = []

    @model_validator(mode="after")
    def one_per_class(self):
        """Drop a claim whose confidences do not pair with its classes; boxes stay only with one per class."""
        if len(self.confidences) != len(self.classes):
            raise ValueError("A detection needs one confidence per class")
        if len(self.boundingBoxes) != len(self.classes):
            self.boundingBoxes = []
        return self


class DetectionClaim(Vendor):
    type: Literal["detection"]
    detection: Detection


class SmokeProbability(Vendor):
    smokeProb: Probability


class SmokeClaim(Vendor):
    type: Literal["smoke"]
    smoke: SmokeProbability


class FireProbability(Vendor):
    fireProb: Probability


class FireClaim(Vendor):
    type: Literal["fire"]
    fire: FireProbability


CLAIMS = {"detection": DetectionClaim, "smoke": SmokeClaim, "fire": FireClaim}


class ImageRecord(Vendor):
    """The camera's image record: its ID (the only servable image), when it was taken and its claims."""
    id: Annotated[str, Field(pattern=IMAGE_ID)]
    type: Literal["image"]
    timeRecorded: Annotated[str, AfterValidator(utc_seconds)]
    claims: list[Annotated[DetectionClaim | SmokeClaim | FireClaim, Field(discriminator="type")]] = []


class Gps(Vendor):
    latitude: Annotated[float, Field(ge=-90, le=90), AfterValidator(round4)]
    longitude: Annotated[float, Field(ge=-180, le=180), AfterValidator(round4)]


class GpsRecord(Vendor):
    type: Literal["gps"]
    gps: Gps


class Anemometer(Vendor):
    speed: Annotated[float, Field(ge=0, le=500)]  # m/s, as Delphire's spec gives it
    angle: Annotated[float | None, measurement(0, 360)] = None


class AnemometerRecord(Vendor):
    type: Literal["anemometer"]
    anemometer: Anemometer


class Gas(Vendor):
    humidity: Annotated[float | None, measurement(0, 100)] = None  # percent
    pressure: Annotated[float | None, measurement(0, 2000)] = None  # hPa


class GasRecord(Vendor):
    type: Literal["gas"]
    gas: Gas


class Voc(Vendor):
    TVOC: Annotated[float | None, measurement(0, 100000)] = None  # ppb
    CO2: Annotated[float | None, measurement(0, 100000)] = None  # ppm


class VocRecord(Vendor):
    type: Literal["voc"]
    voc: Voc


CameraAlias = Literal[settings.CAMERA_ALIASES]


class CameraGroup(Vendor):
    alias: CameraAlias
    records: Annotated[list[ImageRecord], Field(max_length=1)]


class NavigationGroup(Vendor):
    alias: Literal["navigation"]
    records: Annotated[list[GpsRecord], Field(min_length=1, max_length=1)]


class WindGroup(Vendor):
    alias: Literal["wind"]
    records: Annotated[list[AnemometerRecord], Field(min_length=1, max_length=1)]


class GasGroup(Vendor):
    alias: Literal["gas"]
    records: Annotated[list[GasRecord], Field(min_length=1, max_length=1)]


class AirQualityGroup(Vendor):
    alias: Literal["air_quality"]
    records: Annotated[list[VocRecord], Field(min_length=1, max_length=1)]


class CameraLatest(BaseModel):
    """GET /api/v1/camera/latest: the device, its camera alias and the groups the modal shows, camera group first."""
    model_config = ConfigDict(extra="ignore")
    device_id: str
    camera_alias: CameraAlias
    groups: list[Annotated[CameraGroup | NavigationGroup | WindGroup | GasGroup | AirQualityGroup,
                           Field(discriminator="alias")]]


# The sensor groups the modal shows: (alias, record type, record model).
SENSORS = (("navigation", "gps", GpsRecord), ("wind", "anemometer", AnemometerRecord), ("gas", "gas", GasRecord),
           ("air_quality", "voc", VocRecord))


@dataclass(slots=True)
class Dropped:
    """Counts of the claims and records the projection could not use, logged as numbers only."""
    claims: int = 0
    examined_claims: int = 0
    records: int = 0


def project_claims(raw: object, dropped: Dropped) -> list[dict]:
    """Keep each valid claim of the first MAX_CLAIMS; drop the rest."""
    kept = []
    for claim in raw[:MAX_CLAIMS] if isinstance(raw, list) else []:
        dropped.examined_claims += 1
        kind = claim.get("type") if isinstance(claim, dict) else None
        model = CLAIMS.get(kind) if isinstance(kind, str) else None
        try:
            if model is None:
                raise ValueError("Unknown claim type")
            kept.append(model.model_validate(claim, strict=True).model_dump())
        except ValueError:
            dropped.claims += 1
    return kept


def project_image(record: object, dropped: Dropped) -> dict | None:
    """Return an image record's ID, time and valid claims, or None when it is not a valid image record."""
    if not isinstance(record, dict) or record.get("type") != "image":
        return None
    try:
        image = ImageRecord.model_validate({key: record.get(key) for key in ("id", "type", "timeRecorded")},
                                           strict=True)
    except ValidationError:
        dropped.records += 1
        return None
    return {**image.model_dump(), "claims": project_claims(record.get("claims"), dropped)}


def project_reading(records: list, kind: str, model: type[Vendor], dropped: Dropped) -> dict | None:
    """Return the first record of the type that validates and measures something, or None."""
    for record in records:
        if not isinstance(record, dict) or record.get("type") != kind:
            continue
        try:
            reading = model.model_validate(record, strict=True)
        except ValidationError:
            dropped.records += 1
            continue
        if any(value is not None for value in getattr(reading, kind).model_dump().values()):
            return reading.model_dump()
        dropped.records += 1
    return None


def project(records: list, binding: settings.CameraBinding) -> dict:
    """Keep only what the camera modal shows from Delphire's latest records (the allowlist), as the route's view.

    The first group with each alias is used. The camera group comes first, with the first valid image record of
    the configured alias (or none); each sensor group appears only when it has a displayed value.
    """
    groups: dict[str, list] = {}
    for group in records[:MAX_GROUPS]:
        if isinstance(group, dict) and isinstance(group.get("alias"), str) and isinstance(group.get("records"), list):
            groups.setdefault(group["alias"], group["records"][:MAX_RECORDS])
    dropped = Dropped()
    camera = next(filter(None, (project_image(record, dropped) for record in groups.get(binding.alias, []))), None)
    view = [{"alias": binding.alias, "records": [camera] if camera else []}]
    for alias, kind, model in SENSORS:
        reading = project_reading(groups.get(alias, []), kind, model, dropped)
        if reading:
            view.append({"alias": alias, "records": [reading]})
    if dropped.claims or dropped.records:
        logger.warning("Camera latest projection dropped %d of %d claims and %d records", dropped.claims,
                       dropped.examined_claims, dropped.records)
    return CameraLatest.model_validate({"device_id": binding.device_id, "camera_alias": binding.alias,
                                        "groups": view}).model_dump(mode="json")


def strings(value: object) -> Iterator[str]:
    """Yield every string in a JSON value, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def image_id_of(view: dict) -> str | None:
    """The ID of the image record in a view's camera group: the only image the broker serves."""
    records = view["groups"][0]["records"]
    return records[0]["id"] if records else None


# --- The two Delphire requests ----------------------------------------------------------------------------------


def wall_time() -> float:
    """The clock of the reuse window (time.time, as the TomTom readings use); tests replace it."""
    return time.time()


def monotonic() -> float:
    """The clock of the back-off and the miss refresh (time.monotonic); tests replace it."""
    return time.monotonic()


def vendor_client(binding: settings.CameraBinding, timeout: float) -> httpx.AsyncClient:
    """Build the only HTTP client that carries the Delphire key: the configured origin, no redirect and no proxy."""
    return httpx.AsyncClient(base_url=binding.origin, timeout=httpx.Timeout(timeout), follow_redirects=False,
                             trust_env=False, headers={"Authorization": f"Bearer {binding.key}",
                                                       "User-Agent": USER_AGENT})


def media_type(response: httpx.Response) -> str:
    """The response's media type, lower case and without parameters."""
    return response.headers.get("content-type", "").split(";", 1)[0].strip().lower()


def require_ok(response: httpx.Response, kind: str) -> None:
    """Refuse any status but 200, including every redirect, which is never followed."""
    if response.status_code != 200:
        raise failed(kind, f"status {response.status_code}", unavailable())


async def read_capped(response: httpx.Response, limit: int, kind: str,
                      error: Callable[[], IntegrationError]) -> bytes:
    """Read a streamed body of at most limit decoded bytes, whatever its Content-Length says."""
    chunks, total = [], 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise refused(kind, "too large", error())
        chunks.append(chunk)
    return b"".join(chunks)


async def fetch_latest(binding: settings.CameraBinding) -> dict:
    """GET {origin}/devices/{device_id}/latest and project it; refuse an answer that holds the key."""
    timeout = settings.camera_request_timeout_seconds()
    try:
        async with (asyncio.timeout(timeout), vendor_client(binding, timeout) as client,
                    client.stream("GET", f"/devices/{quote(binding.device_id, safe='')}/latest",
                                  headers={"Accept": "application/json"}) as response):
            require_ok(response, "latest")
            if media_type(response) != "application/json":
                raise refused("latest", "content type", invalid())
            body = await read_capped(response, LATEST_MAX_BYTES, "latest", invalid)
        if binding.key.encode() in body:
            raise refused("latest", "key echoed", invalid())
        records = json.loads(body.decode("utf-8"))
        if not isinstance(records, list):
            raise refused("latest", "not a list", invalid())
        view = project(records, binding)
    except IntegrationError:
        raise
    except (httpx.HTTPError, OSError) as exc:
        raise failed("latest", type(exc).__name__, unavailable()) from None
    except (ValueError, RecursionError, TypeError) as exc:
        raise failed("latest", type(exc).__name__, invalid()) from None
    # JSON can carry the key escaped (\u0063, \/ or \"), which the byte check above misses; any string that can
    # leave the API, form a request path or reach an access log is checked once decoded.
    if any(binding.key in text for text in strings(view)):
        raise refused("latest", "key echoed", invalid())
    return {"view": view}


async def fetch_image(binding: settings.CameraBinding, image_id: str) -> dict:
    """GET {origin}/images/{image_id}?type=display as WebP or PNG within the size cap."""
    timeout = settings.camera_request_timeout_seconds()
    cap = settings.camera_image_max_kib() * 1024
    try:
        async with (asyncio.timeout(timeout), vendor_client(binding, timeout) as client,
                    client.stream("GET", f"/images/{quote(image_id, safe='')}", params={"type": "display"},
                                  headers={"Accept": "image/webp, image/png"}) as response):
            require_ok(response, "image")
            kind = media_type(response)
            if kind not in IMAGE_MAGIC:
                raise refused("image", "content type", image_unsupported())
            length = response.headers.get("content-length")
            if length is not None and not re.fullmatch(r"[0-9]{1,15}", length.strip()):
                raise refused("image", "content length", image_unsupported())
            if length is not None and int(length) > cap:
                raise refused("image", "too large", image_too_large())
            body = await read_capped(response, cap, "image", image_too_large)
    except IntegrationError:
        raise
    except (httpx.HTTPError, OSError) as exc:
        raise failed("image", type(exc).__name__, unavailable()) from None
    except (ValueError, RecursionError, TypeError) as exc:
        raise failed("image", type(exc).__name__, image_unsupported()) from None
    if not body or not IMAGE_MAGIC[kind](body):
        raise refused("image", "content", image_unsupported())
    if binding.key.encode() in body:
        raise refused("image", "key echoed", invalid())
    return {"body": body, "media_type": kind}


# --- Reuse, one flight per key, back-off and the daily budget ---------------------------------------------------


@functools.cache
def camera_calls(store) -> ProviderCalls:
    """Return this API process's camera budget and reuse cache against one job store (the TomTom mechanism)."""
    return ProviderCalls(store, "camera_calls", settings.camera_calls_per_day, settings.camera_cache_seconds)


@dataclass(slots=True)
class Failure:
    """A key's consecutive Delphire failures in this process, and when it may be called again."""
    count: int
    until: float              # monotonic()
    error: IntegrationError   # the last failure: code, message, status and retryable


# Per process (each uvicorn worker has its own), touched only from its event loop.
_inflight: dict[tuple, asyncio.Task] = {}
_failures: dict[tuple, Failure] = {}
_miss_refresh_at: dict[tuple, float] = {}      # latest key -> monotonic() of its last miss refresh


def back_off(key: tuple, error: IntegrationError) -> IntegrationError:
    """Record a failed call of key: pause min(10 x 2^(n-1), 600) s after the n-th consecutive failure."""
    previous = _failures.get(key)
    count = (previous.count if previous else 0) + 1
    pause = min(FAILURE_BACKOFF_FIRST_SECONDS * 2 ** min(count - 1, 16), FAILURE_BACKOFF_MAX_SECONDS)
    _failures[key] = Failure(count, monotonic() + pause, error)
    if error.status == 503:
        error.retry_after = pause
    return error


def paused(failure: Failure) -> IntegrationError:
    """Answer a request inside a key's back-off with its last failure; a 503 says how long the pause has left."""
    error = failure.error
    retry_after = max(1, math.ceil(failure.until - monotonic())) if error.status == 503 else None
    return IntegrationError(error.code, error.message, error.status, retryable=error.retryable,
                            retry_after=retry_after)


async def flight(store, key: tuple, fetch: Callable[[], Awaitable[dict]]) -> dict:
    """One Delphire call for key, shared by every request that waits for it: spend, fetch, then keep."""
    calls = camera_calls(store)
    try:
        allowed = await calls.spend()
    except (ClientError, BotoCoreError, sqlite3.Error) as exc:
        # The jobs table, not Delphire, failed: no back-off.
        logger.warning("Camera budget check failed (%s)", type(exc).__name__)
        raise unavailable() from None
    if not allowed:
        raise budgets.daily_limit_reached(BUDGET_MESSAGE, reason="camera_budget")
    try:
        value = await fetch()
    except IntegrationError as exc:
        raise back_off(key, exc) from None
    except Exception as exc:  # noqa: BLE001 — last net: every failed call backs off; only the type is logged
        logger.warning("Camera %s call failed (%s)", key[0], type(exc).__name__)
        raise back_off(key, unavailable()) from None
    _failures.pop(key, None)
    value["retrieved_at"] = wall_time()
    calls.keep(key, value, now=value["retrieved_at"])
    if key[0] == "latest":
        # Only the new view's image is servable now, so older images' failures need not be remembered.
        current = image_id_of(value["view"])
        for stale in [k for k in _failures if k[0] == "image" and k[1] == key[1] and k[2] != current]:
            del _failures[stale]
    return value


def finished(key: tuple, task: asyncio.Task) -> None:
    """Forget a finished flight and retrieve its exception, so a failure nobody awaited is not reported."""
    if _inflight.get(key) is task:
        del _inflight[key]
    if not task.cancelled():
        task.exception()


async def reuse(store, key: tuple, fetch: Callable[[], Awaitable[dict]], *, refresh: bool = False) -> tuple[dict, bool]:
    """Return (value, fetched): an answer inside its reuse window, else the result of one shared Delphire call.

    fetched says whether this request waited for a call. refresh skips the reuse window but keeps the single
    flight, budget and back-off. A waiter's disconnect never cancels the shared call.
    """
    if not refresh:
        kept = camera_calls(store).recent(key, now=wall_time())
        if kept is not None:
            return kept, False
    task = _inflight.get(key)
    if task is None or task.done():
        failure = _failures.get(key)
        if failure is not None and monotonic() < failure.until:
            raise paused(failure)
        task = asyncio.create_task(flight(store, key, fetch))
        _inflight[key] = task
        task.add_done_callback(functools.partial(finished, key))
    return copy.deepcopy(await asyncio.shield(task)), True


def latest_key(binding: settings.CameraBinding) -> tuple:
    return "latest", binding.device_id, binding.alias


async def latest_view(store, binding: settings.CameraBinding, *, refresh: bool = False) -> tuple[dict, bool]:
    """Return (view, fetched) for the device's latest records: reused, or one shared Delphire call."""
    value, fetched = await reuse(store, latest_key(binding), functools.partial(fetch_latest, binding),
                                 refresh=refresh)
    return value["view"], fetched


async def image_bytes(store, binding: settings.CameraBinding, image_id: str) -> dict:
    """Return {"body", "media_type"} of an image only when its ID is the image record of the latest view.

    API processes keep their own views, so a visitor's ID can come from another process's newer view: a miss on a
    view this request did not fetch refreshes the latest records once, at most once per process per reuse window.
    A miss that survives it is a 404 security event, and no image call is made.
    """
    view, fetched = await latest_view(store, binding)
    if image_id != image_id_of(view) and not fetched:
        key = latest_key(binding)
        running = _inflight.get(key)
        if running is not None and not running.done():
            view, _ = await latest_view(store, binding, refresh=True)
        elif monotonic() - _miss_refresh_at.get(key, -math.inf) >= settings.camera_cache_seconds():
            _miss_refresh_at[key] = monotonic()
            view, _ = await latest_view(store, binding, refresh=True)
    if image_id != image_id_of(view):
        raise IntegrationError("CAMERA_IMAGE_NOT_FOUND", IMAGE_NOT_FOUND, 404, reason="not_in_latest")
    value, _ = await reuse(store, ("image", binding.device_id, image_id),
                           functools.partial(fetch_image, binding, image_id))
    return value


# --- Routes -----------------------------------------------------------------------------------------------------


@contextmanager
def route_boundary(kind: str) -> Iterator[None]:
    """Pass IntegrationErrors through; turn anything else into 503 CAMERA_UNAVAILABLE, logging only its type."""
    try:
        yield
    except IntegrationError:
        raise
    except Exception as exc:  # noqa: BLE001 — route boundary: the type is logged, never the message
        logger.warning("Camera %s route failed (%s)", kind, type(exc).__name__)
        raise unavailable() from None


def configured() -> settings.CameraBinding:
    """Return the configured camera, or 503 CAMERA_NOT_CONFIGURED before any cache, budget or Delphire access."""
    binding = settings.camera_binding()
    if binding is None:
        raise not_configured()
    return binding


def create_camera_router(owner_dependency) -> APIRouter:
    """Build the camera routes; owner_dependency resolves the caller (demo_access.workspace_owner)."""
    router = APIRouter(prefix="/api/v1/camera", tags=["Camera"], include_in_schema=False)
    owner = Annotated[str, Depends(owner_dependency)]
    storage = Annotated[Any, Depends(store_for)]

    @router.get("/latest", response_model=CameraLatest)
    async def latest(_: owner, store: storage):
        """The configured device's latest records: only the fields the camera modal shows."""
        with route_boundary("latest"):
            view, _fetched = await latest_view(store, configured())
        return view

    @router.get("/images/{image_id}")
    async def image(image_id: Annotated[str, Path(pattern=IMAGE_ID)], _: owner, store: storage):
        """The image the latest records name, as WebP or PNG within capacity.camera_image_max_kib."""
        with route_boundary("image"):
            value = await image_bytes(store, configured(), image_id)
        return Response(content=value["body"], media_type=value["media_type"], headers=IMAGE_HEADERS)

    return router
