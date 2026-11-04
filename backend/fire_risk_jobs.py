"""Durable landscape preparation in existing API tasks, independent of the chat lease.

The broker owns source identity and authorization. A dedicated queue supplies resumable leases; numerical
work stays in the private modeler. Every read serves saved evidence and never invokes a provider.
"""
import gzip
import io
import json
import logging
import random
import threading
import time
import zlib
from copy import deepcopy
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError
from fire_risk_modeler.models import (
    AssessmentFailure,
    AssessmentResult,
    FireObservation,
    LandscapeAssessmentRequest,
    LandscapeInput,
    LandscapeScenario,
    WeatherObservation,
    model_input_hash,
    model_version,
)
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import (
    DAY_SECONDS,
    budget_day,
    commit,
    digest,
    dynamo_value,
    expires_at,
    record,
)
from traffic_simulator.transport_work import TransportError, run_transport

from backend.fire_risk_contracts import FireRiskJobReference, FireRiskJobResponse

logger = logging.getLogger(__name__)
KIND = "fire_risk_prepare"


class PreparationInterrupted(RuntimeError):
    """Cancellation, shutdown or lost lease interrupts owned local transport before publication."""


def cancellation_check(store, job_id, token, stop=None, lost=None):
    checked_at = 0.0

    def check():
        nonlocal checked_at
        if (stop and stop.is_set()) or (lost and lost.is_set()):
            raise PreparationInterrupted("Preparation interrupted")
        now = time.monotonic()
        if now-checked_at >= 1:
            try:
                current = store.get(job_id) or {}
            except Exception as exc:  # Unknown lease authority cannot become a provider failure.
                if lost is not None:
                    lost.set()
                raise PreparationInterrupted("Lease authority could not be confirmed") from exc
            checked_at = now
            if (current.get("cancel") or current.get("leaseToken") != token
                    or current.get("leaseUntil", 0) < time.time()):
                raise PreparationInterrupted("Preparation interrupted")
    return check


def broker():
    from backend import fire_risk_broker
    return fire_risk_broker


def enqueue(store, owner, session_id, attempt, content):
    """Commit one deterministic job and answer reference before dispatch; replay never creates new work."""
    selected = broker().observed_fires(content)
    if not selected:
        return []
    answer_id = digest([owner, session_id, attempt])
    job_id = str(uuid5(NAMESPACE_URL, digest([answer_id, KIND])))
    job = store.get(job_id)
    if job is None:
        entries = []
        for identity, fire, location in selected:
            weather = broker().normalize_weather(location.get("currentWeather") or {}, fire)
            entries.append({
                "fire_ref": digest([answer_id, identity]),
                "assessment_id": str(uuid5(NAMESPACE_URL, digest([answer_id, identity, "landscape"]))),
                "fire": fire.model_dump(mode="json"),
                "weather": weather.model_dump(mode="json") if weather else None,
                "scenario": scenario().model_dump(mode="json"),
            })
        now = time.time()
        job = {"id": job_id, "owner": owner, "kind": KIND, "status": "queued", "created": now,
               "updated": now, "expires": expires_at(now), "revision": 1, "cancel": False,
               "outbox_pending": True, "phase": "Queued for terrain preparation", "progress": 0.0,
               "entries": entries, "assessment_ids": [], "weather_state": "new", "charges": {}}
        if broker().retained_size(job) > broker().MAX_RETAINED_BYTES:
            raise IntegrationError("EXPOSURE_RECORD_TOO_LARGE", "Fire source evidence is too large.", 503)
        try:
            commit(store, [(job, None)])
        except IntegrationError as exc:
            if exc.code != "REVISION_CONFLICT":
                raise
            job = store.get(job_id)
    if job.get("owner") != owner or job.get("kind") != KIND:
        raise IntegrationError("IDEMPOTENCY_CONFLICT", "Fire risk request identity changed.", 409)
    from backend.simulation_jobs import dispatch
    dispatch(store, job_id)
    return [FireRiskJobReference(job_id=job_id, fire_refs=[item["fire_ref"] for item in job["entries"]])
            .model_dump(mode="json")]


def read_job(store, job_id, owner):
    """Return only owned, unexpired public state; never expose inputs, leases or provider credentials."""
    job = store.get(str(job_id))
    if (not job or job.get("owner") != owner or job.get("kind") != KIND
            or job.get("expires", 0) <= time.time()):
        raise IntegrationError("NOT_FOUND", "Fire risk job not found.", 404)
    results = []
    for identifier in job.get("assessment_ids", []) if job["status"] == "completed" else []:
        receipt = store.get("fire_exposure:"+identifier)
        if receipt and receipt.get("subject") == owner and receipt.get("expires", 0) > time.time():
            results.append(receipt["result"])
    return FireRiskJobResponse(job_id=job["id"], fire_refs=[entry["fire_ref"] for entry in job["entries"]],
                               status=job["status"], phase=job["phase"], progress=float(job["progress"]),
                               assessments=results, error=job.get("error"))


def retry_job(store, job_id, owner, request_id):
    """An explicit owned attempt preserves previous evidence and never replays ambiguous paid weather."""
    view = read_job(store, job_id, owner)
    retry_id = str(uuid5(NAMESPACE_URL, digest([owner, str(job_id), str(request_id), "terrain-retry"])))
    prior = store.get(retry_id)
    if prior:
        return read_job(store, retry_id, owner)
    original = store.get(str(job_id))
    if (view.status not in ("completed", "failed", "cancelled")
            or original.get("weather_state") not in ("completed", "failed")):
        raise IntegrationError("TERRAIN_RETRY_UNAVAILABLE",
                               "Retry needs a finished attempt with retained weather; start a new investigation "
                               "if its weather invocation was interrupted.", 409)
    unavailable = {value.assessment_id for value in view.assessments if value.availability == "unavailable"}
    entries = []
    for old in original["entries"]:
        if view.status == "completed" and old["assessment_id"] not in unavailable:
            continue
        entry = {key: deepcopy(old[key]) for key in ("fire_ref", "fire", "weather", "scenario")}
        entry["assessment_id"] = str(uuid5(NAMESPACE_URL, digest([retry_id, old["assessment_id"]])))
        if not usable_weather(WeatherObservation.model_validate(entry["weather"]) if entry["weather"] else None):
            continue
        if old.get("landscape_input_hash"):
            frozen = input_artifact(store, str(job_id), old["assessment_id"], old["landscape_input_hash"])
            if frozen:
                value = LandscapeAssessmentRequest.model_validate(frozen)
                value = value.model_copy(update={"assessment_id": entry["assessment_id"]})
                value = value.model_copy(update={"input_hash": model_input_hash(value)})
                entry["landscape_input_hash"] = value.input_hash
                input_artifact(store, retry_id, entry["assessment_id"], value.input_hash, value.model_dump(mode="json"))
        entries.append(entry)
    if not entries:
        raise IntegrationError("TERRAIN_RETRY_UNAVAILABLE",
                               "No unavailable terrain assessment has usable retained weather to retry.", 409)
    now = time.time()
    job = {"id": retry_id, "owner": owner, "kind": KIND, "status": "queued", "created": now,
           "updated": now, "expires": min(original["expires"], expires_at(now)), "revision": 1,
           "cancel": False, "outbox_pending": True, "phase": "Queued explicit terrain retry",
           "progress": 0.0, "entries": entries, "assessment_ids": [], "weather_state": "completed",
           "charges": {}, "retry_of": str(job_id)}
    try:
        commit(store, [(job, None)])
    except IntegrationError as exc:
        if exc.code != "REVISION_CONFLICT":
            raise
    from backend.simulation_jobs import dispatch
    dispatch(store, retry_id)
    return read_job(store, retry_id, owner)


def usable_weather(weather):
    """Measured calm is usable without a direction; absent speed is never calm."""
    return (weather is not None and weather.availability == "available" and weather.observed_at is not None
            and weather.wind_speed_mps is not None
            and (weather.wind_speed_mps == 0 or weather.wind_from_degrees is not None))


def fresh_weather(weather):
    """Evaluate current freshness when first freezing; later execution keeps that immutable measured timestamp."""
    if not usable_weather(weather):
        return False
    age = (datetime.now(UTC)-datetime.fromisoformat(weather.observed_at)).total_seconds()
    return -300 <= age <= broker().api_settings().fire_risk_limit("weather_max_age_seconds")


def spend_once(store, job_id, token, marker, budget, limit):
    """Atomically charge the shared daily counter and mark this leased intent, including across worker restarts."""
    day, next_day = budget_day()
    counter_id = f"budget:{budget}:{day}"
    if (store.get(job_id) or {}).get("charges", {}).get(marker):
        return
    if limit <= 0:
        raise IntegrationError("DAILY_LIMIT_REACHED", "The daily fire risk budget has been reached.", 429)
    if store.cloud:
        try:
            store.ddb.transact_write_items(TransactItems=[
                {"Update": {"TableName": store.table.name, "Key": {"id": {"S": job_id}},
                    "UpdateExpression": "SET #charges.#marker=:yes",
                    "ConditionExpression": "leaseToken=:token AND leaseUntil>=:now "
                                           "AND attribute_not_exists(#charges.#marker)",
                    "ExpressionAttributeNames": {"#charges": "charges", "#marker": marker},
                    "ExpressionAttributeValues": {":yes": {"BOOL": True}, ":token": {"S": token},
                                                  ":now": {"N": str(int(time.time()))}}}},
                {"Update": {"TableName": store.table.name, "Key": {"id": {"S": counter_id}},
                    "UpdateExpression": "SET #kind=:kind, expires=:expires ADD #count :one",
                    "ConditionExpression": "attribute_not_exists(#count) OR #count<:limit",
                    "ExpressionAttributeNames": {"#kind": "kind", "#count": "count"},
                    "ExpressionAttributeValues": {":kind": {"S": "budget"}, ":expires": {
                        "N": str(next_day+DAY_SECONDS)}, ":one": {"N": "1"}, ":limit": {"N": str(limit)}}}},
            ])
            return
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            if (store.get(job_id) or {}).get("charges", {}).get(marker):
                return
            if (store.get(counter_id) or {}).get("count", 0) < limit:
                raise RuntimeError("Worker lease lost") from None
    else:
        with store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM jobs WHERE id=?", (job_id,)).fetchone()
            job = json.loads(row[0]) if row else {}
            if job.get("leaseToken") != token or job.get("leaseUntil", 0) < time.time():
                raise RuntimeError("Worker lease lost")
            if job.get("charges", {}).get(marker):
                return
            row = db.execute("SELECT body FROM jobs WHERE id=?", (counter_id,)).fetchone()
            counter = json.loads(row[0]) if row else {"id": counter_id, "kind": "budget", "count": 0,
                                                     "expires": next_day+DAY_SECONDS}
            if counter["count"] < limit:
                counter["count"] += 1
                job.setdefault("charges", {})[marker] = True
                db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?)", (counter_id, None, json.dumps(counter)))
                db.execute("UPDATE jobs SET body=? WHERE id=?", (json.dumps(job), job_id))
                return
    raise IntegrationError("DAILY_LIMIT_REACHED", "The daily fire risk budget has been reached.", 429)


def frozen_weather(store, job, token, check_cancel=None):
    """Freeze one exact-coordinate DTN batch. An ambiguous earlier invocation cannot trigger another billing call."""
    entries = job["entries"]
    if job.get("weather_state") in ("completed", "failed"):
        return entries
    missing = [index for index, entry in enumerate(entries)
               if not fresh_weather(WeatherObservation.model_validate(entry["weather"])
                                    if entry["weather"] else None)]
    if not missing:
        if not store.fenced_update(job["id"], token, weather_state="completed"):
            raise RuntimeError("Worker lease lost")
        return entries
    if job.get("weather_state") == "invoking":
        # A crashed client cannot know whether Lambda returned. Do not repeat that invocation.
        raise IntegrationError("WEATHER_INVOCATION_UNCERTAIN",
                               "The saved weather invocation was interrupted; start a new investigation.", 503)
    operation_id = str(uuid5(NAMESPACE_URL, digest([job["id"], "weather"])))
    if not store.fenced_update(job["id"], token, weather_state="invoking", weather_operation_id=operation_id,
                               phase="Reading DTN weather at the reported fires"):
        raise RuntimeError("Worker lease lost")
    spend_once(store, job["id"], token, "weather", "fire_risk_weather",
               broker().api_settings().fire_risk_limit("weather_calls_per_day"))
    fires = [FireObservation.model_validate(entries[index]["fire"]) for index in missing]
    check_cancel = check_cancel or cancellation_check(store, job["id"], token)
    try:
        values = run_transport("backend.fire_risk_transport", "weather", {
            "fires": [fire.model_dump(mode="json") for fire in fires],
            "deadline": time.time()+broker().api_settings().fire_risk_limit("weather_timeout_seconds"),
            "operation_id": operation_id,
        }, int(broker().api_settings().fire_risk_limit("response_max_kib"))*1024+1024, check_cancel)
    except PreparationInterrupted:
        raise
    except Exception as exc:  # noqa: BLE001 — isolate the failed batch from already measured sibling observations
        logger.warning("Fire risk weather invocation failed: operation=%s reason=%s",
                       operation_id, type(exc).__name__)
        for index in missing:
            entries[index]["weather"] = None
            entries[index]["weather_error"] = "DTN request did not return usable measurements."
        if not store.fenced_update(job["id"], token, entries=entries, weather_state="failed"):
            raise RuntimeError("Worker lease lost") from None
        return entries
    for index in missing:
        fire = FireObservation.model_validate(entries[index]["fire"])
        matches = [item for item in values if isinstance(item, dict)
                   and (item.get("latitude"), item.get("longitude")) == (fire.latitude, fire.longitude)]
        # Identical duplicates are harmless; conflicting duplicates cannot establish authoritative wind.
        if not matches or any(item.get("currentWeather") != matches[0].get("currentWeather") for item in matches):
            entries[index]["weather"] = None
            continue
        raw = matches[0].get("currentWeather")
        raw = raw if isinstance(raw, dict) else {}
        normalized = broker().normalize_weather({**raw, "queryLocation": {
            "latitude": fire.latitude, "longitude": fire.longitude}}, fire)
        entries[index]["weather"] = normalized.model_dump(mode="json") if normalized else None
    if not store.fenced_update(job["id"], token, entries=entries, weather_state="completed"):
        raise RuntimeError("Worker lease lost")
    return entries


def input_artifact(store, job_id, assessment_id, input_hash, value=None):
    """Keep the sealed numerical request outside DynamoDB; the fixed server identifiers own its path."""
    from traffic_simulator import settings
    UUID(job_id)
    UUID(assessment_id)
    if len(input_hash) != 64 or any(character not in "0123456789abcdef" for character in input_hash):
        raise ValueError("Invalid landscape input hash")
    key = f"fire-risk/inputs/{job_id}/{assessment_id}/{input_hash}.json.gz"
    path = store.root / key
    if value is not None:
        raw = gzip.compress(json.dumps(value, separators=(",", ":"), allow_nan=False).encode())
        if store.cloud:
            store.s3.put_object(Bucket=settings.BUCKET, Key=key, Body=raw,
                               ContentType="application/json", ContentEncoding="gzip")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix("."+uuid4().hex+".pending")
            try:
                temporary.write_bytes(raw)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return
    if store.cloud:
        try:
            response = store.s3.get_object(Bucket=settings.BUCKET, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                return None
            raise
        handle = response["Body"]
        try:
            compressed = handle.read(int(broker().api_settings().fire_risk_limit("landscape_request_max_kib"))*1024)
        finally:
            handle.close()
    elif path.exists():
        compressed = path.read_bytes()
    else:
        return None
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
        bound = int(broker().api_settings().fire_risk_limit("landscape_request_max_kib"))*1024
        raw = stream.read(bound+1)
    if len(raw) > bound:
        raise ValueError("Saved landscape request exceeded its bound")
    return json.loads(raw)


def scenario():
    return LandscapeScenario(
        moisture_profile=broker().api_settings().fire_risk_moisture_profile(),
        wind_height_assumption="DTN wind treated as 10 m above ground; open unsheltered fuel assumed",
        initialization="reported point treated as demo ignition",
        horizons_minutes=[float(broker().api_settings().fire_risk_limit(name)) for name in (
            "surface_first_minutes", "surface_second_minutes", "surface_third_minutes")],
        directional_radius=broker().api_settings().fire_risk_limit("landscape_directional_radius"))


def failed_result(entry, weather, code, message):
    """A typed failure has no substitute geometry, and retains the original fire and weather evidence."""
    frozen_scenario = LandscapeScenario.model_validate(entry["scenario"])
    source = {"fire": entry["fire"], "weather": weather.model_dump(mode="json") if weather else None,
              "scenario": frozen_scenario.model_dump(mode="json"), "failure_code": code}
    return AssessmentResult(
        schema_version="2", assessment_id=entry["assessment_id"], fire_ref=entry["fire_ref"],
        input_hash=digest(source), model_version=model_version("landscape"), availability="unavailable",
        mode="landscape", calculated_at=datetime.now(UTC).isoformat(),
        fire=FireObservation.model_validate(entry["fire"]), weather=weather, geojson={"features": []},
        assumptions=[frozen_scenario.initialization, frozen_scenario.wind_height_assumption,
                     "Fuel moisture is an explicit scenario assumption."],
        scenario=frozen_scenario, failure=AssessmentFailure(code=code, message=message))


def transport_failure(entry, weather, exc, default_code):
    """Map infrastructure errors into the canonical per-fire failure contract without losing source identity."""
    try:
        failure = AssessmentFailure(code=exc.code, message=exc.message[:300])
    except ValueError:
        failure = AssessmentFailure(code=default_code, message=exc.message[:300])
    return failed_result(entry, weather, failure.code, failure.message)


def publish(store, job, token, result):
    """Publish immutable evidence only while the worker owns the lease; local and AWS checks are equivalent."""
    document = record("fire_exposure", result.assessment_id, job["owner"], input_hash=result.input_hash,
                      mode="landscape", result=result.model_dump(mode="json"))
    document["revision"] = 2
    if broker().retained_size(document) > broker().MAX_RETAINED_BYTES:
        raise ValueError("Landscape result exceeds retained evidence bound")
    if store.cloud:
        serializer = TypeSerializer()
        try:
            store.ddb.transact_write_items(TransactItems=[
                {"ConditionCheck": {"TableName": store.table.name, "Key": {"id": {"S": job["id"]}},
                    "ConditionExpression": "leaseToken=:token AND leaseUntil>=:now AND #s=:running",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {":token": {"S": token}, ":now": {"N": str(int(time.time()))},
                                                  ":running": {"S": "running"}}}},
                {"Put": {"TableName": store.table.name,
                         "Item": {key: serializer.serialize(value) for key, value in dynamo_value(document).items()},
                         "ConditionExpression": "attribute_not_exists(id)"}}])
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            prior = store.get(document["id"])
            if not prior or prior.get("input_hash") != result.input_hash:
                raise RuntimeError("Worker lease or assessment identity lost") from None
    else:
        with store.db() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("SELECT body FROM jobs WHERE id=?", (job["id"],)).fetchone()
            current = json.loads(saved[0]) if saved else {}
            if (current.get("leaseToken") != token or current.get("leaseUntil", 0) < time.time()
                    or current.get("status") != "running"):
                raise RuntimeError("Worker lease lost")
            prior = db.execute("SELECT body FROM jobs WHERE id=?", (document["id"],)).fetchone()
            if prior:
                if json.loads(prior[0]).get("input_hash") != result.input_hash:
                    raise RuntimeError("Assessment identity changed")
            else:
                db.execute("INSERT INTO jobs VALUES (?,?,?)", (document["id"], None, json.dumps(document)))


def calculate_entry(store, job, entry, token, check_cancel=None):
    """Acquire canonical aligned inputs, seal once, then invoke the separate numerical service."""
    check_cancel = check_cancel or cancellation_check(store, job["id"], token)
    existing = store.get("fire_exposure:"+entry["assessment_id"])
    if existing:
        return AssessmentResult.model_validate(existing["result"])
    weather = WeatherObservation.model_validate(entry["weather"]) if entry["weather"] else None
    if not usable_weather(weather):
        return failed_result(entry, weather, "DTN_WIND_UNUSABLE",
                             "DTN did not provide fresh measured wind at this fire.")
    fire = FireObservation.model_validate(entry["fire"])
    limits = broker().api_settings()
    saved = None
    if entry.get("landscape_input_hash"):
        try:
            saved = input_artifact(store, job["id"], entry["assessment_id"], entry["landscape_input_hash"])
            if saved is None:
                raise ValueError("Missing saved input")
            request = LandscapeAssessmentRequest.model_validate(saved)
            if (request.assessment_id != entry["assessment_id"] or request.fire_ref != entry["fire_ref"]
                    or request.fire != fire or request.weather != weather
                    or request.scenario != LandscapeScenario.model_validate(entry["scenario"])
                    or request.input_hash != entry["landscape_input_hash"]
                    or request.input_hash != model_input_hash(request)):
                raise ValueError("Saved input identity changed")
        except (ValueError, KeyError, gzip.BadGzipFile, EOFError, zlib.error):
            return failed_result(entry, weather, "LANDSCAPE_INPUT_INVALID",
                                 "The saved landscape input is missing or does not match this fire request.")
    else:
        request = None
    radius = float(limits.fire_risk_limit("landscape_initial_radius_m"))
    # Acquisition adds a two-cell halo to ceil(2*radius/30). Account for that halo before selecting an extent.
    maximum = (int(limits.fire_risk_limit("landscape_max_cells") ** 0.5)-2)*30/2
    acquisition = {
        "timeout_seconds": limits.fire_risk_limit("landscape_acquisition_timeout_seconds"),
        "max_response_bytes": limits.fire_risk_limit("landscape_source_max_mib")*1024*1024,
        "max_cells": limits.fire_risk_limit("landscape_max_cells"),
        "idle_timeout_seconds": limits.fire_risk_limit("landscape_idle_timeout_seconds"),
    }
    transport_bound = int(limits.fire_risk_limit("landscape_request_max_kib"))*1024
    while True:
        check_cancel()
        if request is None:
            if not store.fenced_update(job["id"], token, phase="Preparing LANDFIRE fuel and elevation"):
                raise RuntimeError("Worker lease lost")
            maximum_attempts = limits.fire_risk_limit("landscape_max_attempts")
            attempts = entry.setdefault("landscape_attempts", {})
            window = str(radius)
            while attempts.get(window, 0) < maximum_attempts:
                attempts[window] = attempts.get(window, 0)+1
                if not store.fenced_update(job["id"], token, entries=job["entries"],
                                          phase=f"Preparing canonical LANDFIRE sources, attempt {attempts[window]}"):
                    raise RuntimeError("Worker lease lost")
                try:
                    value = run_transport("backend.fire_risk_transport", "landscape", {
                        "fire": fire.model_dump(mode="json"), "radius_m": radius, "limits": acquisition,
                    }, transport_bound, check_cancel)
                    landscape = LandscapeInput.model_validate(value)
                    break
                except TransportError as exc:
                    if not exc.retryable or attempts[window] >= maximum_attempts:
                        entry["source_failure_stage"] = exc.reason
                        return transport_failure(entry, weather, exc, "LANDSCAPE_REQUEST_FAILED")
                    delay = min(limits.fire_risk_limit("retry_max_seconds"),
                                exc.retry_after or random.uniform(0.5, 2 ** attempts[window]))
                    if not store.fenced_update(job["id"], token, phase="Retrying canonical LANDFIRE source"):
                        raise RuntimeError("Worker lease lost") from None
                    cancellable_wait(delay, check_cancel)
            else:
                return failed_result(entry, weather, "LANDSCAPE_REQUEST_FAILED",
                                     "Canonical LANDFIRE source recovery attempts were exhausted.")
            request = LandscapeAssessmentRequest(
                assessment_id=entry["assessment_id"], fire_ref=entry["fire_ref"], input_hash="0"*64,
                fire=fire, weather=weather, scenario=LandscapeScenario.model_validate(entry["scenario"]),
                landscape=landscape)
            request = request.model_copy(update={"input_hash": model_input_hash(request)})
            # A hash-specific checkpoint preserves each expansion instead of relabelling its original input.
            input_artifact(store, job["id"], entry["assessment_id"], request.input_hash,
                           request.model_dump(mode="json"))
            entry["landscape_input_hash"] = request.input_hash
            if not store.fenced_update(job["id"], token, entries=job["entries"]):
                raise RuntimeError("Worker lease lost")
        if not store.fenced_update(job["id"], token, phase="Calculating terrain-aware fire spread"):
            raise RuntimeError("Worker lease lost")
        ceiling = limits.fire_risk_limit("landscape_calculation_timeout_seconds")
        admission = entry.setdefault("model_admission", {})
        admission_key = request.input_hash
        admission_until = admission.setdefault(
            admission_key, time.time()+limits.fire_risk_limit("model_admission_seconds"))
        if not store.fenced_update(job["id"], token, entries=job["entries"]):
            raise RuntimeError("Worker lease lost")
        while True:
            try:
                value = run_transport("backend.fire_risk_transport", "model", {
                    "request": request.model_dump(mode="json"),
                    "deadline": time.time()+ceiling if ceiling else None,
                }, transport_bound, check_cancel)
                result = AssessmentResult.model_validate(value)
                break
            except TransportError as exc:
                # Retry only explicit calculator admission, using its protocol Retry-After. The input is sealed
                # and a stateless model invocation does not charge a provider again.
                if exc.code != "MODEL_BUSY" or not exc.retry_after:
                    return transport_failure(entry, weather, exc, "CALCULATION_FAILED")
                remaining = admission_until-time.time()
                if remaining <= 0:
                    return failed_result(entry, weather, "CALCULATION_LIMIT",
                                         "Fire model capacity remained busy beyond its admission budget.")
                if not store.fenced_update(job["id"], token, phase="Waiting for fire model capacity"):
                    raise RuntimeError("Worker lease lost") from None
                cancellable_wait(min(remaining, exc.retry_after, limits.fire_risk_limit("retry_max_seconds")),
                                 check_cancel)
                if time.time() >= admission_until:
                    return failed_result(entry, weather, "CALCULATION_LIMIT",
                                         "Fire model capacity remained busy beyond its admission budget.")
        if not result.failure or result.failure.code != "DOMAIN_TRUNCATED":
            return result
        radius = max(radius, (request.landscape.manifest.width-2)*30/2)
        if radius >= maximum:
            return result
        radius = min(maximum, radius*2)
        request = None


def cancellable_wait(seconds, check_cancel):
    until = time.monotonic()+seconds
    while time.monotonic() < until:
        check_cancel()
        time.sleep(min(0.1, max(0, until-time.monotonic())))


def execute(store, job_id, stop, receipt=None):
    """One renewable fenced lease; slow source and model calls outlive chat without exposing partial success."""
    from traffic_simulator.store import LEASE_SECONDS
    job = store.get(job_id)
    if not job or job.get("kind") != KIND or job.get("expires", 0) <= time.time():
        return True
    token = str(uuid4())
    if job.get("cancel"):
        return store.acknowledge_cancel(job_id) or job.get("status") == "cancelled"
    if not store.claim(job_id, token):
        return job.get("status") in ("completed", "failed", "cancelled")
    done, lost = threading.Event(), threading.Event()

    def heartbeat():
        while not done.wait(LEASE_SECONDS / 6):
            try:
                store.heartbeat(job_id, token)
                if (store.get(job_id) or {}).get("cancel"):
                    lost.set()
                    return
                if receipt:
                    store.sqs.change_message_visibility(QueueUrl=risk_queue(),
                        ReceiptHandle=receipt, VisibilityTimeout=LEASE_SECONDS)
            except Exception:  # noqa: BLE001 — a worker that cannot renew must lose publication rights
                lost.set()
                return
    thread = threading.Thread(target=heartbeat, daemon=True, name="fire-risk-lease")
    thread.start()
    try:
        job = store.get(job_id)
        check_cancel = cancellation_check(store, job_id, token, stop, lost)
        entries = frozen_weather(store, job, token, check_cancel)
        completed, available = [], 0
        for index, entry in enumerate(entries):
            if stop.is_set() or lost.is_set():
                return False
            if (store.get(job_id) or {}).get("cancel"):
                return store.acknowledge_cancel(job_id)
            existing = store.get("fire_exposure:"+entry["assessment_id"])
            if not existing:
                spend_once(store, job_id, token, entry["assessment_id"], "fire_risk_assessments",
                           broker().api_settings().fire_risk_limit("assessments_per_day"))
            started = time.monotonic()
            result = calculate_entry(store, job, entry, token, check_cancel)
            if result.availability == "unavailable":
                logger.warning("Terrain assessment unavailable: job=%s assessment=%s code=%s "
                               "source_stage=%s attempts=%s duration_seconds=%.3f", job_id, entry["assessment_id"],
                               result.failure.code if result.failure else "UNAVAILABLE",
                               entry.get("source_failure_stage"),
                               sum(entry.get("landscape_attempts", {}).values()), time.monotonic()-started)
            check_cancel()
            publish(store, job, token, result)
            completed.append(result.assessment_id)
            available += result.availability == "available"
            if not store.fenced_update(job_id, token, assessment_ids=completed,
                                       progress=(index+1)/len(entries)):
                return False
        return store.finish(job_id, token, [],
                            phase=f"Assessed {len(completed)} fires: {available} available, "
                                  f"{len(completed)-available} unavailable")
    except Exception as exc:  # noqa: BLE001 — never retain SDK responses, endpoint URLs or credentials
        if (store.get(job_id) or {}).get("cancel"):
            return store.acknowledge_cancel(job_id)
        if isinstance(exc, PreparationInterrupted) or stop.is_set() or lost.is_set():
            return False
        logger.warning("Fire risk job failed: job=%s reason=%s", job_id, type(exc).__name__)
        message = exc.message if isinstance(exc, IntegrationError) else "Fire risk preparation failed."
        return store.fenced_update(job_id, token, status="failed", phase="Preparation failed", error=message)
    finally:
        done.set()
        thread.join()


def risk_queue():
    import os

    from traffic_simulator import settings
    value = os.getenv("FIRE_RISK_QUEUE_URL", "")
    if settings.CLOUD and not value:
        raise RuntimeError("AWS fire risk preparation requires FIRE_RISK_QUEUE_URL")
    return value


def loop(store, stop):
    """One consumer per API process; task restart resumes leased jobs from the existing durable queue."""
    from traffic_simulator.store import LEASE_SECONDS
    queue_url = risk_queue()
    while not stop.is_set():
        try:
            if store.cloud:
                messages = store.sqs.receive_message(QueueUrl=queue_url, WaitTimeSeconds=20,
                                                    MaxNumberOfMessages=1).get("Messages", [])
                for message in messages:
                    identifier = json.loads(message["Body"])["id"]
                    if execute(store, identifier, stop, message["ReceiptHandle"]):
                        store.sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
            else:
                for job in store.pending():
                    if job.get("kind") == KIND:
                        execute(store, job["id"], stop)
                stop.wait(1)
        except Exception as exc:  # noqa: BLE001 — durable queue remains authoritative after a transport interruption
            logger.warning("Fire risk queue iteration interrupted: reason=%s", type(exc).__name__)
            stop.wait(LEASE_SECONDS / 6)
