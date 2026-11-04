"""IAM-only forward geocoding shared by Gateway, radio, and the simulation broker."""
import math
from datetime import UTC, datetime
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


@lru_cache(maxsize=1)
def places():
    return boto3.client("geo-places", config=Config(
        connect_timeout=2, read_timeout=5, retries={"total_max_attempts": 1}))


def normalize(response):
    candidates = []
    for item in response.get("ResultItems", []):
        point = item.get("Position", [])
        address = item.get("Address", {})
        score = item.get("MatchScores", {}).get("Overall")
        if (len(point) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) for v in point)
                or not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90
                or type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1
                or address.get("Country", {}).get("Code3") != "USA"
                or not address.get("Label")):
            continue
        candidates.append({
            "matched_address": address["Label"], "lat": point[1], "lon": point[0],
            "address_type": item.get("PlaceType", ""), "region": address.get("Region", {}).get("Code"),
            "country": "USA", "score": score * 100,
            "match_scores": item.get("MatchScores", {}),
        })
    return {"provider": "Amazon Location", "resolver_version": "amazon-location-v1",
            "resolved_at": datetime.now(UTC).isoformat(), "candidates": candidates,
            "score_meaning": "Amazon Location MatchScores.Overall scaled to 0–100; not a probability.",
            "intended_use": "Storage"}


def handler(event, context):
    # Both direct IAM Lambda invocations and Gateway use this same bounded input.
    address = event.get("address") if isinstance(event, dict) else None
    if (not isinstance(address, str) or not 3 <= len(address.strip()) <= 500
            or set(event) != {"address"}):
        return {"error": {"code": "INVALID_ADDRESS", "message": "Supply one address or place name (3–500 characters)."}}
    try:
        return normalize(places().geocode(
            QueryText=address.strip(), MaxResults=6, Filter={"IncludeCountries": ["USA"]},
            IntendedUse="Storage"))
    except (BotoCoreError, ClientError, ValueError, TypeError):
        # Never turn a provider failure into a successful empty search.
        return {"error": {"code": "GEOCODER_UNAVAILABLE", "message": "Amazon Location could not resolve this request."}}
