"""Optional live source proof: one actual Esri fire and/or Seattle road completion and cancellation.

After publication:
  python -m deployment.acceptance.check_background_sources --submit --scope all --output REPORT.json

Status polling has bounded requests and no independent job deadline. Native replay uses the saved numerical
input offline; it never fetches replacement fuel, elevation or weather. Visitor cookies stay in memory.
"""
import argparse
import gzip
import importlib.util
import io
import json
import math
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from deployment import ROOT
from deployment.acceptance.background_jobs import (
    contracts,
    digest,
    discovery_receipt,
    observed_fires,
    require,
    submit_road_discovery,
    wait_road_discovery,
    wait_terrain_jobs,
)
from deployment.acceptance.smoke_simulation_api import investigation_turn, verify_fire_search_center
from deployment.acceptance.visitor import (
    TIMEOUT_SECONDS,
    AcceptanceError,
    checked,
    error_code,
    visitor_client,
    write_report,
)
from deployment.deployment_target import safe_outputs
from deployment.installation import load_installation

HEX = re.compile(r"[a-f0-9]{64}")
BUDGETS = ("agent_requests", "fire_risk_weather", "fire_risk_assessments")
ROAD_AREA = {"lat": 47.60395, "lon": -122.3305, "radius_km": 2}
RECORD_FIELDS = ("revision", "mode", "input_hash", "result")
JOB_FIELDS = ("id", "kind", "status", "entries", "assessment_ids", "revision", "artifact_attempt", "scenario")
REPLAY_ANGLES = {
    ("landscape", "ignition_aspect_degrees"): (360, 1e-6),
    ("landscape", "ignition_slope_degrees"): (90, 1e-7),
}


def plain(value):
    """Normalize DynamoDB numbers without including an SDK object or response metadata."""
    def number(item):
        if isinstance(item, Decimal):
            return int(item) if item == item.to_integral_value() else float(item)
        raise TypeError()
    return json.loads(json.dumps(value, default=number, allow_nan=False))


def record(table, key, fields):
    aliases = {f"#f{index}": name for index, name in enumerate(fields)}
    response = table.get_item(Key={"id": key}, ConsistentRead=True,
                              ProjectionExpression=",".join(aliases), ExpressionAttributeNames=aliases)
    require(isinstance(response.get("Item"), dict), "Saved source evidence is missing.")
    return plain(response["Item"])


def counters(table):
    day = datetime.now(UTC).date().isoformat()
    values = {}
    for name in BUDGETS:
        item = table.get_item(Key={"id": f"budget:{name}:{day}"}, ConsistentRead=True,
                              ProjectionExpression="#count", ExpressionAttributeNames={"#count": "count"})
        values[name] = int((item.get("Item") or {}).get("count", 0))
    return {"utc_day": day, "counts": values}


def selected_incident(anchor, index=0):
    """One fixed official feed query; choose a real, eligible source point, never a fixture or city."""
    spec = importlib.util.spec_from_file_location("acceptance_source_fire", ROOT / "backend/gateway/wildfire/handler.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    response = tool.handler({"geometry": f"{anchor[1]},{anchor[0]}", "distance": tool.MAX_DISTANCE}, None)
    require(not response.get("error"), "Official Esri source discovery is unavailable.")
    contracts()
    from fire_risk_modeler.landscape import LandscapeError, source_region
    from fire_risk_modeler.models import FireObservation

    candidates = []
    for feature in response.get("features", []):
        if not isinstance(feature, dict):
            continue
        attributes, point = feature.get("attributes") or {}, feature.get("geometry") or {}
        if not isinstance(attributes, dict) or not isinstance(point, dict):
            continue
        lat, lon = point.get("y"), point.get("x")
        contained = attributes.get("PercentContained")
        if (not all(type(value) in (int, float) and math.isfinite(value) for value in (lat, lon))
                or not -90 <= lat <= 90 or not -180 <= lon <= 180
                or attributes.get("IncidentTypeCategory") != "WF"
                or type(contained) in (int, float) and contained >= 100):
            continue
        identity = next((f"esri:{key}:{attributes[key]}" for key in (
            "IrwinID", "IRWINID", "UniqueFireIdentifier", "GlobalID", "OBJECTID")
            if attributes.get(key) is not None), None)
        if not identity:
            continue
        name = attributes.get("IncidentName")
        try:
            fire = FireObservation(latitude=lat, longitude=lon, source_id=identity,
                                   name=name if isinstance(name, str) else None)
            region = source_region(fire)
        except (ValueError, LandscapeError):
            continue
        candidates.append((fire, region))
    require(0 <= index < len(candidates), "The bounded official Esri query has no eligible U.S. fire at that index.")
    fire, region = candidates[index]
    return {"fire": fire.model_dump(mode="json"), "region": region,
            "source": "fixed official Esri wildfire feed", "query_radius_miles": tool.MAX_DISTANCE,
            "truncated": bool(response.get("truncated")), "candidate_index": index,
            "eligible_count": len(candidates)}


def equivalent(first: object, second: object, path: tuple[str | int, ...] = ()) -> bool:
    """Bound derived angles only; other numbers match exactly across DynamoDB JSON representations."""
    if path in REPLAY_ANGLES:
        maximum, tolerance = REPLAY_ANGLES[path]
        if (type(first) not in (int, float) or type(second) not in (int, float)
                or not 0 <= first <= maximum or not 0 <= second <= maximum
                or not math.isfinite(first) or not math.isfinite(second)):
            return False
        difference = abs(first - second)
        if maximum == 360:
            difference = min(difference, 360 - difference)
        return difference <= tolerance
    if type(first) in (int, float) and type(second) in (int, float):
        # DynamoDB retained reads emit integral Decimals as ints; the model emits schema floats.
        return (first == second and (type(first) is int or math.isfinite(first))
                and (type(second) is int or math.isfinite(second)))
    if type(first) is not type(second):
        return False
    if isinstance(first, list) and isinstance(second, list):
        return len(first) == len(second) and all(
            equivalent(a, b, (*path, index)) for index, (a, b) in enumerate(zip(first, second)))
    if isinstance(first, dict) and isinstance(second, dict):
        return first.keys() == second.keys() and all(
            equivalent(first[key], second[key], (*path, key)) for key in first)
    return first == second


def sealed_replay(s3, bucket, job, result, maximum_bytes):
    """Read the fixed input path, verify its seal and replay only that effective numerical input."""
    contracts()
    from fire_risk_modeler.landscape import LandscapeError, unpack_landscape
    from fire_risk_modeler.landscape_spread import assess_landscape
    from fire_risk_modeler.models import LandscapeAssessmentRequest, model_input_hash

    entry = next((entry for entry in job["entries"] if entry["assessment_id"] == result["assessment_id"]), None)
    require(entry is not None and entry["fire_ref"] == result["fire_ref"]
            and entry["fire"] == result["fire"], "Saved terrain entry changed its original fire identity.")
    identity = entry.get("landscape_input_hash")
    if not identity:
        require(result["availability"] == "unavailable", "Available terrain has no sealed landscape input.")
        require(result["input_hash"] == digest({
            "fire": entry["fire"], "weather": entry["weather"], "scenario": entry["scenario"],
            "failure_code": result["failure"]["code"]}),
            "Pre-input failure does not match its frozen source evidence.")
        return {"status": "not_applicable", "reason": "Source preparation failed before numerical input sealing.",
                "failure": result["failure"], "numerical_replay": False}
    require(HEX.fullmatch(identity) is not None, "Saved numerical input hash is invalid.")
    UUID(job["id"])
    UUID(result["assessment_id"])
    key = f"fire-risk/inputs/{job['id']}/{result['assessment_id']}/{identity}.json.gz"
    response = s3.get_object(Bucket=bucket, Key=key)
    handle = response["Body"]
    try:
        compressed = handle.read(maximum_bytes + 1)
    finally:
        handle.close()
    require(len(compressed) <= maximum_bytes, "Saved numerical input exceeds its declared size.")
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
        encoded = stream.read(maximum_bytes + 1)
    require(len(encoded) <= maximum_bytes, "Saved numerical input expands beyond its declared size.")
    request = LandscapeAssessmentRequest.model_validate_json(encoded)
    require(request.assessment_id == result["assessment_id"] and request.fire_ref == result["fire_ref"]
            and request.fire.model_dump(mode="json") == entry["fire"]
            and request.weather.model_dump(mode="json") == entry["weather"]
            and request.scenario.model_dump(mode="json") == entry["scenario"]
            and request.input_hash == identity == model_input_hash(request),
            "Sealed numerical request changed its effective source, weather, scenario or input hash.")
    try:
        unpack_landscape(request.landscape)
    except LandscapeError as exc:
        require(result["availability"] == "unavailable" and exc.failure.code == "FUEL_CODE_UNSUPPORTED"
                and result["failure"]["code"] == exc.failure.code,
                "Saved numerical raster has an invalid seal or unexplained unsupported fuel.")
    if result["input_hash"] != identity:
        require(result["availability"] == "unavailable" and result["input_hash"] == digest({
            "fire": entry["fire"], "weather": entry["weather"], "scenario": entry["scenario"],
            "failure_code": result["failure"]["code"]}),
            "Preparation failure cannot replace a successful sealed-input result.")
        return {"status": "input_verified", "input_hash": identity, "numerical_replay": False,
                "failure": result["failure"], "reason": "Infrastructure failed after input sealing."}
    replay = assess_landscape(request).model_dump(mode="json")
    require(equivalent({key: value for key, value in replay.items() if key != "calculated_at"},
                       {key: value for key, value in result.items() if key != "calculated_at"}),
            "Saved terrain result does not match the pinned offline native replay.")
    return {"status": "passed", "input_hash": identity, "numerical_replay": True,
            "manifest": request.landscape.manifest.model_dump(mode="json")}


def hidden(other, path):
    response = other.get(path)
    require(response.status_code == 404 and error_code(response.content) == "NOT_FOUND",
            "A different visitor can read another visitor's saved source evidence.")


def retained_reads(owner, other, table, terrain):
    """GET replay proves immutable records; shared counters have an explicit concurrency attribution limit."""
    before, evidence = counters(table), []
    for job in terrain["jobs"]:
        path = f"fire-exposure/jobs/{job['job_id']}"
        original = checked(owner.get(path)).json()
        expected = {item["assessment_id"]: item["result_sha256"] for item in job["assessments"]}
        require(original.get("status") == "completed"
                and {item["assessment_id"]: digest(item) for item in original["assessments"]} == expected,
                "Saved terrain changed from the original completed receipt.")
        stored = record(table, job["job_id"], JOB_FIELDS)
        hidden(other, path)
        require(checked(owner.get(path)).json() == original, "Retained terrain job changed on repeated GET.")
        require(record(table, job["job_id"], JOB_FIELDS) == stored, "GET changed the private terminal job.")
        for raw in original["assessments"]:
            identifier = raw["assessment_id"]
            path = f"fire-exposure/assessments/{identifier}"
            assessment = record(table, "fire_exposure:" + identifier, RECORD_FIELDS)
            require(assessment["revision"] == 2 and assessment["mode"] == "landscape"
                    and assessment["result"] == raw and assessment["input_hash"] == raw["input_hash"],
                    "Retained schema-2 assessment is not its immutable terminal record.")
            for _ in range(2):
                response = checked(owner.get(path))
                require(response.json() == raw and response.headers.get("cache-control") == "no-store",
                        "Saved assessment replay changed its result or lacks no-store.")
            hidden(other, path)
            require(record(table, "fire_exposure:" + identifier, RECORD_FIELDS) == assessment,
                    "GET changed the retained assessment or its terminal revision.")
            evidence.append({"assessment_id": identifier, "result_sha256": digest(raw), "revision": 2,
                             "owner_status": 200, "foreign_status": 404, "unchanged": True})
    after = counters(table)
    return {"assessments": evidence, "before": before, "after": after, "counters_unchanged": before == after,
            "claim_limit": "Shared counters may change from concurrent visitors or UTC rollover; GET hashes and "
                           "terminal records prove immutability, not absence of all unmetered calls."}


def fire_handoff(owner, table, result):
    """One incident uses the retained reported point; this check never prepares roads or runs SUMO."""
    identifier, fire = result["assessment_id"], result["fire"]
    before = record(table, "fire_exposure:" + identifier, RECORD_FIELDS)
    session = checked(owner.post("sessions", json={"request_id": str(uuid4()), "entry_mode": "radio"})).json()
    incident = checked(owner.post("incidents", json={"request_id": str(uuid4()), "session_id": session["session_id"],
                                                   "fire_assessment_id": identifier})).json()
    location = incident["location"]
    expected = {"lat": fire["latitude"], "lon": fire["longitude"], "source_id": fire["source_id"],
                "source_timestamp": fire["source_timestamp"], "source_assessment_id": identifier}
    require(location["origin"] == "reported_fire" and location["provider"] == "Esri"
            and location["score"] is None and location["resolver_version"] == "fire-observation-v1"
            and all(location.get(key) == value for key, value in expected.items())
            and "not a verified ignition" in location["meaning"],
            "Reported-fire handoff changed its immutable observed point or provenance.")
    require(checked(owner.get(f"incidents/{incident['incident_id']}")).json() == incident
            and record(table, "fire_exposure:" + identifier, RECORD_FIELDS) == before,
            "Saved incident read or handoff changed retained source evidence.")
    return {"incident_id": incident["incident_id"], "source_point": expected, "origin": location["origin"],
            "meaning": location["meaning"], "status": "passed", "scope": "No geocoding, preparation or SUMO."}


def fire_check(owner, other, table, s3, bucket, installation, anchor, index, expected_build, save):
    selected = selected_incident(anchor, index)
    fire = selected["fire"]
    center = {"latitude": round(fire["latitude"], 6), "longitude": round(fire["longitude"], 6)}
    question = (f"Check currently reported wildfires within 5 miles of "
                f"{center['latitude']:.6f}, {center['longitude']:.6f}. Include current weather.")
    receipt = investigation_turn(owner, str(uuid4()), question, name="live-terrain-source",
                                 wants_detection=True, wants_guidance=False, expected_build=expected_build)
    content = receipt["snapshot"]["content"]
    require(verify_fire_search_center(content, **center), "Investigation shifted the exact requested source center.")
    require(any(item["fire"].get("sourceId") == fire["source_id"]
                and item["latitude"] == fire["latitude"] and item["longitude"] == fire["longitude"]
                for item in observed_fires(content)), "Investigation changed the actual Esri-selected fire point.")
    report = {"selected": selected, "source_observations": [
        {"latitude": item["latitude"], "longitude": item["longitude"], "fire": {
            key: item["fire"].get(key) for key in ("name", "sourceId", "lastUpdated")}}
        for item in observed_fires(content)],
              "investigation": {key: receipt[key] for key in ("tools", "identities", "execution_receipts", "status")},
              "requested_center": center, "terrain": {}}

    def progress(value):
        report["terrain"] = value
        save(report)
    report["terrain"] = wait_terrain_jobs(owner, content, checkpoint=progress, require_current=True)
    report["saved_reads"] = retained_reads(owner, other, table, report["terrain"])
    report["replays"] = []
    chosen = None
    for job in report["terrain"]["jobs"]:
        stored = record(table, job["job_id"], JOB_FIELDS)
        response = checked(owner.get(f"fire-exposure/jobs/{job['job_id']}")).json()
        for result in response["assessments"]:
            proof = sealed_replay(s3, bucket, stored, result,
                                  installation.settings["fire_risk_landscape_request_max_kib"] * 1024)
            report["replays"].append({"assessment_id": result["assessment_id"], **proof})
            if result["fire"]["source_id"] == fire["source_id"]:
                chosen = result
    require(chosen is not None, "Selected source fire has no terminal terrain assessment.")
    report["handoff"] = fire_handoff(owner, table, chosen)
    report["status"] = "passed" if report["terrain"]["available"] else "failed"
    report["outcomes"] = [result["outcome"] for job in report["terrain"]["jobs"] for result in job["assessments"]]
    save(report)
    require(report["terrain"]["available"] > 0,
            "No actual available terrain assessment demonstrated capacity; typed unavailable outcomes are retained.")
    return report


def road_provenance(result):
    """Saved OSM candidates need actual regional source hashes and way identity, not a closure claim."""
    require(result.get("source") == "OpenStreetMap via Geofabrik"
            and isinstance(result.get("osm_sha256"), str) and HEX.fullmatch(result["osm_sha256"])
            and result.get("meaning") == "Candidate road corridors; no inference of road safety, closures or "
                                       "emergency access", "Road discovery lacks its declared source and meaning.")
    manifest = result.get("source_manifest")
    require(isinstance(manifest, dict) and manifest.get("publisher") == "Geofabrik"
            and bool(manifest.get("snapshot_timestamp")) and bool(manifest.get("recipe"))
            and bool(manifest.get("osmium_version")) and isinstance(manifest.get("sources"), list)
            and bool(manifest["sources"]), "Road discovery has no frozen publisher snapshot.")
    try:
        timestamp = datetime.fromisoformat(manifest["snapshot_timestamp"])
    except (ValueError, TypeError):
        timestamp = None
    require(timestamp is not None and timestamp.utcoffset() is not None,
            "Road publisher snapshot timestamp is invalid.")
    for source in manifest["sources"]:
        address = urlsplit(source.get("url", "")) if isinstance(source, dict) else urlsplit("")
        require(isinstance(source, dict) and isinstance(source.get("sha256"), str)
                and HEX.fullmatch(source["sha256"]) and isinstance(source.get("md5"), str)
                and re.fullmatch(r"[a-f0-9]{32}", source["md5"])
                and type(source.get("bytes")) is int and source["bytes"] > 0
                and isinstance(source.get("region_id"), str) and bool(source["region_id"])
                and address.scheme == "https" and address.hostname == "download.geofabrik.de"
                and not address.username and not address.password and not address.query and not address.fragment
                and isinstance(source.get("header"), dict) and not source["header"].get("with_history")
                and source["header"].get("option", {}).get("osmosis_replication_timestamp")
                == manifest["snapshot_timestamp"] == source.get("snapshot_timestamp"),
                "Road publisher snapshot has invalid identity, size or checksums.")
    corridors = result["highways"]
    require(bool(corridors), "Seattle discovery returned no candidate road corridors.")
    features = result["geometry"]["features"]
    for corridor in corridors:
        identifier = corridor.get("corridor_id")
        ways = corridor.get("source_way_ids")
        require(isinstance(identifier, str) and re.fullmatch(r"osm:(?:ref|name):[a-f0-9]{20}|osm:way:\d+", identifier)
                and corridor.get("source") == "OpenStreetMap"
                and isinstance(corridor.get("label"), str) and bool(corridor["label"])
                and isinstance(ways, list) and bool(ways) and all(type(way) is int and way > 0 for way in ways),
                "Candidate corridor lost its OSM namespace or source way identities.")
        require(any(feature.get("properties", {}).get("corridor_id") == identifier
                    and feature["properties"].get("source_way_id") in ways for feature in features),
                "Candidate corridor does not map to the saved source geometry.")
    return {"result_sha256": digest(result), "source": result["source"], "osm_sha256": result["osm_sha256"],
            "source_manifest": manifest, "corridor_count": len(corridors), "geometry_count": len(features),
            "meaning": result["meaning"]}


def road_reads(owner, other, job, area):
    path = f"research/discover/{job['id']}"
    original = discovery_receipt(checked(owner.get(path)).json(), area, job["id"])
    require(original == job, "Saved road job changed from its original terminal receipt.")
    for _ in range(2):
        require(checked(owner.get(path)).json() == original, "Saved road job changed on repeated GET.")
    hidden(other, path)
    return {"job_id": job["id"], "status": original["status"], "receipt_sha256": digest(original),
            "owner_status": 200, "foreign_status": 404, "unchanged": True}


def road_check(owner, other, area, save):
    report = {"area": area, "scope": "Two explicit area-only road jobs; no traffic-speed query or SUMO run."}
    created = submit_road_discovery(owner, area)
    report["completion"] = wait_road_discovery(owner, created, area,
                                              checkpoint=lambda value: save({**report, "completion": value}))
    require(report["completion"]["status"] == "completed", "Seattle road discovery did not complete.")
    report["provenance"] = road_provenance(report["completion"]["result"])
    report["saved_completion"] = road_reads(owner, other, report["completion"], area)
    cancelled = submit_road_discovery(owner, area)
    identifier = cancelled["id"]
    acknowledgement = checked(owner.post(f"research/discover/{identifier}/cancel", json={})).json()
    if acknowledgement.get("status") == "completed" and "result" not in acknowledgement:
        # A POST acknowledgement may omit the already published artifact. Read its authoritative GET;
        # do not invent a cancelled outcome or resubmit another provider job.
        require(acknowledgement.get("id") == identifier and acknowledgement.get("scenario") == area,
                "Cancel acknowledgement changed the submitted road job.")
        acknowledgement = checked(owner.get(f"research/discover/{identifier}")).json()
    report["cancel_requested"] = discovery_receipt(acknowledgement, area, identifier)
    report["cancellation"] = wait_road_discovery(owner, report["cancel_requested"], area,
                                                checkpoint=lambda value: save({**report, "cancellation": value}))
    terminal = report["cancellation"]
    require(terminal["status"] in {"cancelled", "completed"}, "Road cancellation failed before worker acknowledgement.")
    if terminal["status"] == "completed":
        road_provenance(terminal["result"])
    repeated = checked(owner.post(f"research/discover/{identifier}/cancel", json={})).json()
    require(repeated["status"] == terminal["status"], "Cancellation overwrote an already terminal road job.")
    report["saved_cancellation"] = road_reads(owner, other, terminal, area)
    report["cancel_outcome"] = "cancelled" if terminal["status"] == "cancelled" else "completion_won_cancel_race"
    report["status"] = "passed" if terminal["status"] == "cancelled" else "partial"
    save(report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--submit", action="store_true", help="Explicitly authorize the bounded real source workloads.")
    parser.add_argument("--scope", choices=("fire", "roads", "all"), required=True)
    parser.add_argument("--installation")
    parser.add_argument("--base-url", help="HTTPS origin; otherwise the application stack output.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--search-latitude", type=float, default=38.5816, help="Esri query anchor only, never an ignition.")
    parser.add_argument("--search-longitude", type=float, default=-121.4944)
    parser.add_argument("--incident-index", type=int, default=0, choices=range(100), metavar="N",
                        help="Explicit additional actual source case; default first eligible Esri fire, no retry loop.")
    parser.add_argument("--expected-runtime-build")
    args = parser.parse_args(argv)
    if not args.submit:
        parser.error("--submit is required before creating source or road workloads")
    anchor = (args.search_latitude, args.search_longitude)
    require(all(math.isfinite(value) for value in anchor) and -90 <= anchor[0] <= 90
            and -180 <= anchor[1] <= 180, "Source search anchor is invalid.")
    report = {"status": "running", "started_at": datetime.now(UTC).isoformat(), "scope": args.scope}
    try:
        installation = load_installation(args.installation)
        session = boto3.Session(profile_name=installation.profile, region_name=installation.target.region)
        config = Config(connect_timeout=TIMEOUT_SECONDS, read_timeout=TIMEOUT_SECONDS,
                        retries={"total_max_attempts": 1})
        stack = session.client("cloudformation", config=config).describe_stacks(
            StackName=installation.target.stack("application"))["Stacks"][0]
        require(stack["StackStatus"] in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}, "Application deployment is unsettled.")
        outputs = safe_outputs(stack)
        origin = (args.base_url or outputs["IntegratedWorkspaceUrl"]).rstrip("/")
        address = urlsplit(origin)
        require(address.scheme == "https" and address.hostname and not address.path and not address.query
                and not address.fragment and not address.username and not address.password,
                "Use a deployed HTTPS origin without credentials or a path.")
        report["origin"] = origin
        with visitor_client(origin) as owner, visitor_client(origin) as other:
            for client in (owner, other):
                checked(client.post("demo/session"))

            def save(section, value):
                report[section] = value
                write_report(args.output, report)
            if args.scope in {"fire", "all"}:
                table = session.resource("dynamodb", config=config).Table(outputs["JobsTable"])
                report["fire"] = fire_check(owner, other, table, session.client("s3", config=config),
                                           outputs["ArtifactsBucket"], installation, anchor,
                                           args.incident_index, args.expected_runtime_build,
                                           lambda value: save("fire", value))
            if args.scope in {"roads", "all"}:
                report["roads"] = road_check(owner, other, dict(ROAD_AREA), lambda value: save("roads", value))
        statuses = {report[section]["status"] for section in ("fire", "roads") if section in report}
        report["status"] = "partial" if "partial" in statuses else "passed"
    except Exception as exc:  # noqa: BLE001 — source/SDK responses and visitor credentials never enter receipts
        report.update(status="failed", error=str(exc) if isinstance(exc, (RuntimeError, AcceptanceError))
                      and not isinstance(exc, ClientError) else type(exc).__name__)
        if isinstance(exc, ClientError):
            report["aws_error_code"] = exc.response.get("Error", {}).get("Code", "")
    finally:
        report["finished_at"] = datetime.now(UTC).isoformat()
        write_report(args.output, report)
    print(json.dumps({"status": report["status"], "report": str(args.output)}, sort_keys=True))
    return 1 if report["status"] in {"failed", "partial"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
