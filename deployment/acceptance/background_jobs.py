"""Retained terrain and road-discovery checks; status reads never create or retry source/model work."""
import hashlib
import json
import math
import sys
import time
from datetime import datetime
from uuid import UUID, uuid4

from deployment import ROOT
from deployment.acceptance.visitor import TIMEOUT_SECONDS, checked

ACTIVE = {"queued", "running", "cancelling"}
TERMINAL = {"completed", "failed", "cancelled"}
POLL_SECONDS = 2.5


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def contracts():
    """Load only canonical, I/O-free DTOs outside the pytest path configuration."""
    for folder in (".", "fire-risk-modeler"):
        path = str(ROOT / folder)
        if path not in sys.path:
            sys.path.insert(0, path)
    from fire_risk_modeler.models import AssessmentResult

    from backend.fire_risk_contracts import FireRiskJobReference, FireRiskJobResponse
    return FireRiskJobReference, FireRiskJobResponse, AssessmentResult


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def observed_fires(content):
    """Keep source points only: the search area's city is never an assessment origin."""
    locations = (content.get("mapData") or {}).get("locations", [])
    return [item for item in locations if isinstance(item, dict) and isinstance(item.get("fire"), dict)
            and all(type(item.get(key)) in (float, int) and math.isfinite(item[key])
                    and abs(item[key]) <= limit for key, limit in (("latitude", 90), ("longitude", 180)))]


def source_matches(assessment, location):
    fire = location["fire"]
    if (assessment.fire.latitude != location["latitude"] or assessment.fire.longitude != location["longitude"]
            or assessment.fire.source_id != fire.get("sourceId")
            or assessment.fire.name != fire.get("name")):
        return False
    observed = fire.get("lastUpdated")
    try:
        timestamp = datetime.fromisoformat(observed) if isinstance(observed, str) else None
    except ValueError:
        timestamp = None
    if timestamp is None or timestamp.utcoffset() is None:
        return assessment.fire.source_timestamp is None
    return (assessment.fire.source_timestamp is not None
            and datetime.fromisoformat(assessment.fire.source_timestamp) == timestamp)


def terrain_outcome(raw, locations):
    """Validate actual returned horizons, calm measurements and typed failures without inventing spread."""
    assessment = contracts()[2].model_validate(raw)
    require(assessment.schema_version == "2" and assessment.mode == "landscape",
            "A terrain job substituted a historical distance or uniform-fuel assessment.")
    require(any(source_matches(assessment, location) for location in locations),
            "Terrain assessment changed the answer's original source fire.")
    result = {"assessment_id": assessment.assessment_id, "fire_ref": assessment.fire_ref,
              "input_hash": assessment.input_hash, "model_version": assessment.model_version,
              "availability": assessment.availability, "fire": assessment.fire.model_dump(mode="json"),
              "result_sha256": digest(raw)}
    if assessment.availability == "unavailable":
        result.update(outcome="unavailable", failure=assessment.failure.model_dump(mode="json"), horizons=[])
        return result
    weather, grid, scenario = assessment.weather, assessment.landscape, assessment.scenario
    require(weather is not None and weather.availability == "available" and weather.observed_at
            and weather.wind_speed_mps is not None
            and (weather.wind_speed_mps == 0 or weather.wind_from_degrees is not None),
            "Available terrain has no usable measured wind; missing wind cannot be calm.")
    features = assessment.geojson.features
    horizons = [feature.properties.elapsed_minutes for feature in features]
    require(horizons == scenario.horizons_minutes, "Terrain omitted or changed its saved time horizons.")
    cells = grid.manifest.width * grid.manifest.height
    require(grid.reached_cells >= 1 and grid.spread_cells == grid.reached_cells - 1
            and grid.reached_cells <= grid.modeled_cells
            and grid.modeled_cells + grid.nonburnable_cells + grid.nodata_cells == cells,
            "Terrain cell accounting does not match its saved landscape.")
    if grid.spread_cells == 0:
        require(all(feature.geometry == features[0].geometry for feature in features),
                "A no-growth terrain receipt changed geometry between time horizons.")
    result.update(outcome="no_growth" if grid.spread_cells == 0 else "spread", horizons=horizons,
                  weather=weather.model_dump(mode="json"), landscape=grid.model_dump(mode="json"),
                  geometry_sha256=[digest(feature.geometry.model_dump(mode="json")) for feature in features])
    return result


def terrain_receipt(raw, reference, locations):
    response = contracts()[1].model_validate(raw)
    require(response.job_id == reference.job_id and response.fire_refs == reference.fire_refs,
            "Terrain status belongs to a different saved job or fire set.")
    require(len(set(response.fire_refs)) == len(response.fire_refs), "Terrain job repeats a source reference.")
    if response.status != "completed":
        require(not response.assessments, "An unfinished terrain job exposed provisional success.")
        return response, []
    references = [item.fire_ref for item in response.assessments]
    identifiers = [item.assessment_id for item in response.assessments]
    require(len(set(identifiers)) == len(identifiers) and len(set(references)) == len(references)
            and set(references) == set(reference.fire_refs),
            "Completed terrain results do not cover exactly the saved fire references.")
    return response, [terrain_outcome(item, locations) for item in raw["assessments"]]


def wait_terrain_jobs(client, content, *, checkpoint=None, require_current=False):
    """Read until authoritative terminal states, with bounded GETs and no invented job deadline.

    A historical answer is explicitly labeled and never recalculated. New source answers carry job references;
    a transport or contract error stops acceptance without creating an unavailable model result.
    """
    locations, references = observed_fires(content), content.get("fireExposureJobs")
    if not references:
        if require_current and locations:
            raise RuntimeError("A new source answer has no background terrain job references.")
        historical = content.get("fireExposureData") or []
        if historical:
            models = [contracts()[2].model_validate(item) for item in historical]
            require(all(any(source_matches(item, location) for location in locations) for item in models),
                    "Historical exposure changed its saved source fire.")
        return {"status": "historical" if historical else "not_requested", "jobs": [],
                "historical_assessment_count": len(historical),
                "scope": "Saved answer only; no terrain, weather or provider work started."}
    require(isinstance(references, list) and not content.get("fireExposureData"),
            "A new background terrain answer contains provisional or historical success geometry.")
    reference_type = contracts()[0]
    references = [reference_type.model_validate(item) for item in references]
    require(len({item.job_id for item in references}) == len(references), "Answer repeats a terrain job.")
    report = {"status": "running", "jobs": [], "scope": "Same-visitor saved GETs only; no source/model retries."}
    for reference in references:
        entry = {"job_id": reference.job_id, "fire_refs": reference.fire_refs, "status": "queued"}
        report["jobs"].append(entry)
        while True:
            raw = checked(client.get(f"fire-exposure/jobs/{reference.job_id}", timeout=TIMEOUT_SECONDS)).json()
            receipt, outcomes = terrain_receipt(raw, reference, locations)
            entry.update(status=receipt.status, phase=receipt.phase, progress=receipt.progress, error=receipt.error)
            if checkpoint:
                checkpoint(report)
            if receipt.status not in ACTIVE:
                break
            time.sleep(POLL_SECONDS)
        if receipt.status != "completed":
            report["status"] = receipt.status
            if checkpoint:
                checkpoint(report)
            raise RuntimeError(f"Terrain job reached {receipt.status}; no successful geometry was substituted.")
        for result in raw["assessments"]:
            saved = checked(client.get(
                f"fire-exposure/assessments/{result['assessment_id']}", timeout=TIMEOUT_SECONDS)).json()
            require(saved == result, "Completed terrain job differs from its retained assessment GET.")
        entry["assessments"] = outcomes
        if checkpoint:
            checkpoint(report)
    report.update(status="completed", available=sum(item["availability"] == "available"
                  for job in report["jobs"] for item in job["assessments"]),
                  unavailable=sum(item["availability"] == "unavailable"
                  for job in report["jobs"] for item in job["assessments"]))
    if checkpoint:
        checkpoint(report)
    return report


def discovery_receipt(raw, area, identifier=None):
    """The discovery receipt stores an area, never a SUMO scenario's default highway."""
    require(isinstance(raw, dict), "Road discovery returned an unusable receipt.")
    try:
        valid_id = str(UUID(raw.get("id"))) == raw.get("id")
    except (ValueError, TypeError, AttributeError):
        valid_id = False
    require(valid_id and (identifier is None or raw["id"] == identifier)
            and raw.get("kind") == "road_discovery" and raw.get("scenario") == area
            and raw.get("status") in ACTIVE | TERMINAL and isinstance(raw.get("phase"), str)
            and bool(raw["phase"]) and type(raw.get("progress")) in (float, int)
            and math.isfinite(raw["progress"]) and 0 <= raw["progress"] <= 1,
            "Road discovery changed its saved job or exact submitted area.")
    if raw["status"] != "completed":
        require("result" not in raw, "Unfinished road discovery exposed a success result.")
    else:
        result = raw.get("result")
        require(isinstance(result, dict) and isinstance(result.get("geometry"), dict)
                and result["geometry"].get("type") == "FeatureCollection"
                and isinstance(result["geometry"].get("features"), list)
                and isinstance(result.get("highways"), list)
                and ("area" not in result or result["area"] == area),
                "Completed road discovery has no usable saved area result.")
    return raw


def submit_road_discovery(client, area):
    """Only an explicit acceptance call submits work; a queued 202 carries no road or simulation result."""
    response = checked(client.post("research/discover", json={**area, "request_id": str(uuid4())}))
    require(response.status_code == 202, "Road discovery did not return its asynchronous 202 receipt.")
    return discovery_receipt(response.json(), area)


def wait_road_discovery(client, job, area, *, checkpoint=None):
    """Rejoin an existing discovery using saved GETs; no polling deadline or automatic retry POST."""
    current = discovery_receipt(job, area)
    while current["status"] in ACTIVE:
        if checkpoint:
            checkpoint(current)
        current = discovery_receipt(checked(client.get(
            f"research/discover/{job['id']}", timeout=TIMEOUT_SECONDS)).json(), area, job["id"])
        if current["status"] in ACTIVE:
            time.sleep(POLL_SECONDS)
    if checkpoint:
        checkpoint(current)
    return current
