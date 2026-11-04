"""Amazon Location receipts. Historical labels never select historical source data."""
import json
import math
import os
import time
from datetime import UTC, datetime

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fire_risk_modeler.models import FireObservation
from pydantic import ValidationError
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import digest, owned

PRESETS = {
    "paradise-2018": {
        "name": "Paradise — Camp Fire (2018)", "year": 2018,
        "address": "Paradise, California, USA", "radius_km": 8.0,
        "highway": "State 191", "exit_direction": "south",
    },
    "palisades-2025": {
        "name": "Pacific Palisades, Los Angeles — Palisades Fire (2025)", "year": 2025,
        "address": "Pacific Palisades, Los Angeles, California, USA", "radius_km": 6.0,
        "highway": "State 1", "exit_direction": "east",
    },
}


def distance_m(a, b):
    """Great-circle (haversine) distance in meters between two lat/lon points."""
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlat, dlon = lat2-lat1, math.radians(b["lon"]-a["lon"])
    return 6371000 * 2 * math.asin(min(1, math.sqrt(
        math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2)))


def resolve(address, *, preset=False, place=False):
    """Geocode an address through the Amazon Location geocoder Lambda (GEOCODER_FUNCTION_ARN)."""
    # Imported per call: the offline contract exporter imports this module without deployment settings.
    from backend import api_settings as settings
    function = settings.geocoder_function()
    if not function:
        raise IntegrationError("GEOCODER_NOT_CONFIGURED", "Configure the Amazon Location geocoder.", 503)
    try:
        client = boto3.client("lambda", region_name=os.getenv("AWS_REGION"), config=Config(
            connect_timeout=2, read_timeout=12, retries={"total_max_attempts": 1}))
        response = client.invoke(FunctionName=function, Payload=json.dumps({"address": address}).encode())
        stream = response["Payload"]
        try:
            payload = json.loads(stream.read())
        finally:
            stream.close()
        if response.get("FunctionError") or not isinstance(payload, dict) or payload.get("error"):
            raise ValueError("Provider rejected request")
    except (BotoCoreError, ClientError, ValueError, KeyError, TypeError):
        raise IntegrationError("GEOCODER_UNAVAILABLE", "Amazon Location could not resolve this address.", 503,
                               retryable=True) from None
    return select_receipt(address, payload, preset=preset, place=place)


def select_receipt(address, payload, *, preset=False, place=False):
    """Validate a geocoder reply and pick its single acceptable match as a location receipt."""
    allowed = {"PointAddress", "InterpolatedAddress", "Intersection"}
    if preset or place:
        allowed |= {"Locality", "District", "PointOfInterest"}
    if (not isinstance(payload, dict) or payload.get("provider") != "Amazon Location"
            or payload.get("resolver_version") != "amazon-location-v1"
            or not isinstance(payload.get("candidates"), list)):
        raise IntegrationError("GEOCODER_UNAVAILABLE", "Unexpected geocoder response.", 503)
    candidates = []
    for value in payload.get("candidates", []):
        if (not isinstance(value, dict) or not isinstance(value.get("matched_address"), str)
                or not value["matched_address"].strip()):
            continue
        lat, lon = value.get("lat"), value.get("lon")
        score = value.get("score")
        if (value.get("address_type") not in allowed
                or type(score) not in (int, float) or not math.isfinite(score) or not 90 <= score <= 100
                or value.get("country") != "USA"
                or type(lat) not in (int, float) or type(lon) not in (int, float)
                or not -90 <= lat <= 90 or not -180 <= lon <= 180):
            continue
        candidates.append({"status": "resolved", "reported_address": address,
                           "matched_address": value["matched_address"], "lat": lat, "lon": lon,
                           "address_type": value["address_type"], "score": float(score),
                           "provider": "Amazon Location", "resolver_version": "amazon-location-v1",
                           "score_meaning": payload.get("score_meaning"),
                           "resolved_at": datetime.now(UTC).isoformat(),
                           "source_id": f"amazon-location:{digest([value['matched_address'], lat, lon])}",
                           "origin": "geocoded_place" if preset or place else "geocoded_address",
                           **({"meaning": "Geocoded search-area center; a demo scenario assumption, not "
                               "confirmation of a fire or exact ignition."} if place else {})})
    if not candidates:
        message = ("Use a specific U.S. place with its city and state." if place else
                   "Use a specific U.S. street address or intersection.")
        raise IntegrationError("LOCATION_UNRESOLVED", message)
    first = candidates[0]
    if any(distance_m(first, candidate) > 250 for candidate in candidates[1:]):
        raise IntegrationError("LOCATION_AMBIGUOUS", "Amazon Location found separate locations. Include the city and street number.")
    return first


def coordinate_receipt(point):
    """Keep an explicitly supplied point separate from a provider-confirmed address or fire."""
    description = point.label or f"User-provided point ({point.lat:.5f}, {point.lon:.5f})"
    return {
        "status": "resolved", "reported_address": description, "matched_address": description,
        "lat": point.lat, "lon": point.lon, "address_type": "Coordinates",
        "provider": "User input", "score": None, "resolver_version": "coordinates-v1",
        "origin": "user_coordinates", "resolved_at": datetime.now(UTC).isoformat(),
        "source_id": f"coordinates:{digest([point.lat, point.lon])}",
        "meaning": "User-provided scenario point; not confirmation of an active fire or ignition.",
    }


def reported_fire_receipt(store, owner, assessment_id):
    """Use an immutable owned fire observation; model availability does not change its source authority."""
    receipt = owned(store, "fire_exposure", assessment_id, owner)
    if receipt.get("expires", 0) <= time.time():
        raise IntegrationError("NOT_FOUND", "Reported fire observation expired.", 404)
    if receipt.get("revision") != 2:
        raise IntegrationError("SOURCE_NOT_READY", "Wait for the reported fire observation to finish.", 409)
    result = receipt.get("result")
    try:
        if not isinstance(result, dict) or result.get("assessment_id") != str(assessment_id):
            raise ValueError()
        fire = FireObservation.model_validate(result["fire"])
    except (ValidationError, ValueError, KeyError, TypeError):
        raise IntegrationError("SOURCE_INVALID", "The saved fire observation cannot supply a scenario point.", 409) from None
    description = fire.name or "Selected reported fire"
    return {
        "status": "resolved", "reported_address": description, "matched_address": description,
        "lat": fire.latitude, "lon": fire.longitude, "address_type": "ReportedFire",
        "provider": "Esri", "score": None, "resolver_version": "fire-observation-v1",
        "origin": "reported_fire", "resolved_at": datetime.now(UTC).isoformat(),
        "source_id": fire.source_id or f"fire-assessment:{assessment_id}",
        "source_timestamp": fire.source_timestamp, "source_assessment_id": str(assessment_id),
        "meaning": "Reported fire source point used as a demo scenario origin; not a verified ignition "
                   "or an operational evacuation zone.",
    }


def reported_fire_observation_receipt(store, owner, observation_id):
    """Copy a saved source point into the incident without geocoding or requiring a model result."""
    receipt = owned(store, "fire_observation", observation_id, owner)
    if receipt.get("expires", 0) <= time.time():
        raise IntegrationError("NOT_FOUND", "Reported fire observation expired.", 404)
    try:
        fire = FireObservation.model_validate(receipt["fire"])
        if not fire.source_id:
            raise ValueError()
    except (ValidationError, ValueError, KeyError, TypeError):
        raise IntegrationError("SOURCE_INVALID", "The saved fire observation cannot supply a scenario point.", 409) from None
    description = fire.name or "Selected reported fire"
    return {
        "status": "resolved", "reported_address": description, "matched_address": description,
        "lat": fire.latitude, "lon": fire.longitude, "address_type": "ReportedFire",
        "provider": "Esri", "score": None, "resolver_version": "fire-observation-v1",
        "origin": "reported_fire", "resolved_at": datetime.now(UTC).isoformat(),
        "source_id": fire.source_id, "source_timestamp": fire.source_timestamp,
        "source_observation_id": str(observation_id),
        "meaning": "Reported fire source point used as a demo scenario origin; not a verified ignition "
                   "or an operational evacuation zone.",
    }
