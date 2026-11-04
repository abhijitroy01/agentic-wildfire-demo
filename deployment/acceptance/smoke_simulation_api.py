"""Post-deployment smoke test through the public demo session. A fresh job starts only with --submit.

There is no user sign-in: the script opens a demo session on the integrated workspace, exactly as a
visitor's browser does, and stops when the workspace is not open to visitors.
"""
import argparse
import importlib.util
import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from deployment.acceptance.background_jobs import observed_fires, wait_terrain_jobs
from deployment.acceptance.visitor import AcceptanceError, checked, error_code, visitor_client, write_report

ROOT = Path(__file__).resolve().parents[2]
# A feed incident name the fallback may put in a prompt: 1 to 120 plain characters, nothing else.
INCIDENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .,'()&/#-]{0,119}")


def verify_fire_search_center(content, latitude, longitude):
    """A matching incident name cannot hide a fire lookup shifted to a nearby town."""
    center = ((content.get("mapData") or {}).get("summary") or {}).get("location")
    return isinstance(center, dict) and all(
        type(center.get(key)) in (int, float) and math.isfinite(center[key])
        and abs(center[key] - expected) <= 0.000001
        for key, expected in (("latitude", latitude), ("longitude", longitude)))


def execution_status(event, calls):
    """Known prerequisite barriers may recover; provider and unknown execution errors may not."""
    result = json.loads(event["content"])
    if result.get("status") == "success":
        return "success"
    if (result.get("status") == "error"
            and calls.get(event["toolCallId"]) == "PublicAnswerResponse"
            and isinstance(result.get("result"), str)
            and (result["result"].startswith("SOURCE_RESULTS_REQUIRED: ")
                 or result["result"] == (
                     "SELECTED_TASKS_REQUIRED: Task selection resolved this turn's scope. Run the selected "
                     "specialists and use their current receipts; location_required is only for an empty task scope."))):
        return "prerequisite_rejected"
    barriers = {
        "public_response_agent_tool": "Complete the requested observations before guidance.",
        "communication_agent_tool": "Complete requested observations and guidance before communication.",
    }
    barrier = barriers.get(calls.get(event["toolCallId"]))
    if (result.get("status") == "error" and barrier
            and result.get("result") == "Error: RuntimeError - OBSERVATIONS_REQUIRED: " + barrier):
        return "prerequisite_rejected"
    raise RuntimeError("missing or failed SDK execution receipt")


def investigation_turn(client, thread, question, *, name, wants_detection, wants_guidance,
                       wants_communication=False, expected_build=None):
    """One real AG-UI turn; shared by fresh-conversation and deployment-continuity acceptance."""
    tools, snapshot, finished, executions = [], None, False, []
    calls, rejections, pending, identities = {}, [], set(), {}
    with client.stream("POST", "investigation/turns", json={
            "thread_id": thread, "question": question, "context_envelope": {
                "mapContext": {"centerLocation": {"lat": 34, "lng": -118}, "zoomLevel": 10,
                               "visibleIncidentIds": [], "selectedIncidentId": None},
                "incidentResponse": {"activeIncidents": [], "completedIncidents": []},
                "radioMonitor": {"isActive": False, "activeChannels": [], "recentKeywords": []},
                "cameraSensor": None, "fireInventory": {"fires": []}, "userQuery": question}}) as response:
        checked(response)
        for line in response.iter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if event["type"] == "RUN_ERROR":
                raise RuntimeError(f"{name}: investigation failed")
            if event["type"] == "TOOL_CALL_START":
                tools.append(event.get("toolCallName"))
                calls[event.get("toolCallId", event.get("toolCallName"))] = event.get("toolCallName")
            elif event["type"] == "TOOL_CALL_RESULT":
                try:
                    status = execution_status(event, calls)
                except RuntimeError as exc:
                    raise RuntimeError(f"{name}: {exc}") from exc
                if status == "prerequisite_rejected":
                    rejections.append(event["toolCallId"])
                    pending.add(calls[event["toolCallId"]])
                else:
                    executions.append(event["toolCallId"])
                    pending.discard(calls.get(event["toolCallId"]))
            elif event["type"] == "STATE_SNAPSHOT":
                snapshot = event["snapshot"]
            elif event["type"] == "RUN_FINISHED":
                finished = True
            elif event["type"] == "CUSTOM" and event.get("name") in {"runtime_binding", "runtime_execution"}:
                identities[event["name"]] = event["value"]
    receipt = {"name": name, "tools": tools, "snapshot": snapshot, "identities": identities,
               "execution_receipts": executions, "prerequisite_rejections": rejections, "status": "received"}
    if not finished or not snapshot or not executions:
        raise RuntimeError(f"{name}: no completed source response")
    content = snapshot["content"]
    if (("detection_agent_tool" in tools) != wants_detection or
            ("public_response_agent_tool" in tools) != wants_guidance or
            ("communication_agent_tool" in tools) != wants_communication or
            bool(content.get("knowledgeBaseCitations")) != wants_guidance or
            (not wants_communication and content.get("audioData"))):
        raise RuntimeError(f"{name}: incorrect topic routing {tools}")
    successful = {calls.get(identifier) for identifier in executions}
    if (wants_detection and "detection_agent_tool" not in successful
            or wants_guidance and "public_response_agent_tool" not in successful
            or wants_communication and "communication_agent_tool" not in successful or pending):
        raise RuntimeError(f"{name}: rejected prerequisite did not recover with successful requested tools")
    if expected_build is not None:
        execution = identities.get("runtime_execution", {})
        if execution.get("build_id") != expected_build:
            raise RuntimeError(f"{name}: executing Runtime build does not match this release")
        if execution.get("conversation_id") != identities.get("runtime_binding", {}).get("conversation_id"):
            raise RuntimeError(f"{name}: execution and broker conversation identities differ")
    receipt["status"] = "passed"
    return receipt


def investigation_conversation(client, *, checkpoint=None):
    """Require memory-aware fire/current/forecast/guidance and explicit combined orchestration."""
    thread = str(uuid4())
    receipts = []
    for name, question, wants_detection, wants_guidance in (
        ("fire-only", "Is there a fire going near sacramento ca", True, False),
        ("weather-follow-up", "What is the current weather there?", True, False),
        ("forecast-follow-up", "What about tomorrow?", True, False),
        ("current-versus-forecast", "Compare the weather now with tomorrow's forecast there.", True, False),
        ("guidance-follow-up", "Give me response guidance.", False, True),
        ("combined", "Check the fire and current weather near Sacramento CA and build response guidance.",
         True, True),
        ("fire-after-guidance", "Is there a fire going near sacramento ca", True, False),
    ):
        receipt = investigation_turn(client, thread, question, name=name,
                                     wants_detection=wants_detection, wants_guidance=wants_guidance)
        content = receipt["snapshot"]["content"]
        receipts.append(receipt)
        if checkpoint:
            checkpoint(receipts)
        locations = (content.get("mapData") or {}).get("locations", [])
        if name in {"fire-only", "fire-after-guidance"} and (
                any(loc.get("currentWeather") for loc in locations) or content.get("forecastData")):
            raise RuntimeError("Fire-only search fetched unrequested weather")
        if name == "weather-follow-up" and (
                not any(loc.get("currentWeather") for loc in locations) or any(loc.get("fire") for loc in locations)):
            raise RuntimeError("Weather follow-up did not return weather alone")
        if name == "weather-follow-up":
            first = receipts[0]["snapshot"]["content"]
            reference = first.get("location") or (first.get("mapData") or {}).get("summary", {}).get("location")
            actual = content.get("location")
            if not reference or not actual or any(
                    abs(reference[key]-actual[key]) > 0.000001 for key in ("latitude", "longitude")):
                raise RuntimeError("Weather follow-up lost the previous search location")
            if any(abs(reference[key]-loc[key]) > 0.000001 for loc in locations
                   if loc.get("currentWeather") for key in ("latitude", "longitude")):
                raise RuntimeError("Weather observations do not belong to the remembered search point")
        if name in {"forecast-follow-up", "current-versus-forecast"}:
            if not content.get("forecastData") or any(loc.get("fire") for loc in locations):
                raise RuntimeError(f"{name}: forecast missing or unrequested fire search")
            if any(loc.get("currentWeather") for loc in locations) != (name == "current-versus-forecast"):
                raise RuntimeError(f"{name}: incorrect current/forecast provider selection")
            first = receipts[0]["snapshot"]["content"].get("location")
            actual = content.get("location")
            if not first or not actual or any(
                    abs(first[key]-actual[key]) > 0.000001 for key in ("latitude", "longitude")):
                raise RuntimeError(f"{name}: forecast lost the remembered location")
        if name == "combined" and (
                not content.get("mapData") or not any(loc.get("currentWeather") for loc in locations)):
            raise RuntimeError("Combined request lost source weather observations")
        receipt["status"] = "passed"
        print("Investigation conversation:", name, "passed", flush=True)
    fires = [loc for loc in (receipts[-1]["snapshot"]["content"].get("mapData") or {}).get("locations", [])
             if loc.get("fire")]
    if fires:
        incident = {"path": "nearby fire", "name": fires[0]["fire"]["name"]}
    else:
        # No fire is reported near Sacramento: look up the nearest reported incident at its own
        # coordinates, so the observations name it. A small radius keeps it among the fire tool's top results.
        incident = nearest_reported_incident(receipts[0]["snapshot"]["content"]["location"])
        latitude, longitude = round(incident["latitude"], 4), round(incident["longitude"], 4)
        receipt = investigation_turn(
            client, thread, f"Is there a fire going within 5 miles of {latitude:.4f}, "
            f"{longitude:.4f}.", name="fire-near-reported-incident",
            wants_detection=True, wants_guidance=False)
        reported = {(loc.get("fire") or {}).get("name")
                    for loc in (receipt["snapshot"]["content"].get("mapData") or {}).get("locations", [])}
        matched_center = verify_fire_search_center(receipt["snapshot"]["content"], latitude, longitude)
        receipt.update(incident=incident, requested_search_center_matches=matched_center,
                       status="passed" if incident["name"] in reported and matched_center else "failed")
        receipts.append(receipt)
        if checkpoint:
            checkpoint(receipts)
        if receipt["status"] == "failed":
            raise RuntimeError(
                f"fire-near-reported-incident: source incident or requested search center does not match "
                f"the {incident['name']} lookup")
        print("Investigation conversation: fire-near-reported-incident passed", flush=True)
    chosen = incident["name"]
    receipt = investigation_turn(
        client, thread, f"Prepare English, Spanish and Hindi voice drafts for the reported {chosen} fire. "
        "Use the existing conversation observations without a new fire or weather lookup.",
        name="selected-fire-communication", wants_detection=False, wants_guidance=False, wants_communication=True)
    audio = receipt["snapshot"]["content"].get("audioData") or {}
    if audio.get("summary") != {"totalLanguages": 3, "successful": 3, "failed": 0}:
        raise RuntimeError("Communication did not generate all three requested recordings")
    playback = []
    with httpx.Client(timeout=30, follow_redirects=False) as media:
        for code in ("en", "es", "hi"):
            recording = (audio.get("languages") or {}).get(code) or {}
            address = urlsplit(recording.get("audioUrl") or "")
            if (address.scheme != "https" or not address.hostname or address.username or address.password
                    or not address.hostname.endswith(".s3.us-west-2.amazonaws.com")):
                raise RuntimeError("Recording is not a private regional S3 playback URL")
            response = media.get(recording["audioUrl"])
            if (response.status_code != 200 or response.headers.get("content-type") != "audio/mpeg"
                    or not 1024 < len(response.content) < 2*1024*1024):
                raise RuntimeError(f"{code}: actual recording could not be played")
            if not recording.get("translatedText") or not time.time() < recording.get("expires", 0) <= time.time()+3600:
                raise RuntimeError(f"{code}: reviewable translated text or bounded playback expiry missing")
            playback.append({"language": code, "bytes": len(response.content), "content_type": "audio/mpeg",
                             "expires": recording["expires"], "status": "passed"})
    # Playback links are presigned: before the receipt is saved, keep only scheme, host and path.
    for recording in audio["languages"].values():
        if isinstance(recording, dict) and isinstance(recording.get("audioUrl"), str):
            address = urlsplit(recording["audioUrl"])
            recording["audioUrl"] = f"{address.scheme}://{address.hostname}{address.path}"
    receipt.update(playback=playback, incident=incident)
    receipts.append(receipt)
    if checkpoint:
        checkpoint(receipts)
    print("Investigation conversation: selected incident and real multilingual playback passed", flush=True)
    receipt = investigation_turn(
        client, thread, "Is there a fire going near sacramento ca", name="fire-after-communication",
        wants_detection=True, wants_guidance=False)
    receipts.append(receipt)
    if checkpoint:
        checkpoint(receipts)
    return receipts


def use_backend_api():
    """Load the backend and its shared simulator/modeler contracts outside the pytest environment."""
    for folder in (".", "traffic-simulator", "fire-risk-modeler"):
        path = str(ROOT / folder)
        if path not in sys.path:
            sys.path.insert(0, path)


def nearest_reported_incident(point):
    """Return the named incident nearest point among the deployed wildfire tool's widest-radius results.

    It runs the tool's own query (backend/gateway/wildfire/handler.py: its feed, maximum radius and record
    cap), so a capped list says truncated. A failed source or no usable named incident raises.
    """
    spec = importlib.util.spec_from_file_location("wildfire_tool",
                                                  ROOT / "backend/gateway/wildfire/handler.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    result = tool.handler({"geometry": f"{point['longitude']},{point['latitude']}",
                           "distance": tool.MAX_DISTANCE}, None)
    if result.get("error"):
        raise RuntimeError("No fire is reported near the search point, and the wildfire source failed "
                           f"({result['error']['code']}); communication acceptance needs a reported incident")
    use_backend_api()
    from backend.evacuation.locations import distance_m
    origin, incidents = {"lat": point["latitude"], "lon": point["longitude"]}, []
    for feature in result["features"]:
        name, where = (feature.get("attributes") or {}).get("IncidentName"), feature.get("geometry") or {}
        lat, lon = where.get("y"), where.get("x")
        # Feed text goes into a prompt and the receipt: only a plain name and real coordinates qualify.
        # The range comparisons are False for NaN and infinities, so they also keep the point finite.
        if (isinstance(name, str) and INCIDENT_NAME.fullmatch(name)
                and all(type(value) in (int, float) for value in (lat, lon))
                and -90 <= lat <= 90 and -180 <= lon <= 180):
            incidents.append((distance_m(origin, {"lat": lat, "lon": lon}), name, lat, lon))
    if not incidents:
        raise RuntimeError(f"The wildfire source lists no usable named incident within {tool.MAX_DISTANCE} "
                           "miles of the search point; communication acceptance needs a reported incident")
    meters, name, lat, lon = min(incidents, key=lambda item: item[0])
    return {"path": "nearest reported incident", "name": name, "distance_miles": round(meters/1609.344, 1),
            "latitude": round(lat, 4), "longitude": round(lon, 4),
            "radius_miles": tool.MAX_DISTANCE, "truncated": bool(result.get("truncated"))}


def verify_plan_summary(content, pages):
    """Compare saved text to the same deterministic renderer used by the broker."""
    use_backend_api()
    from backend.evacuation.evidence import planning_summary
    expected = planning_summary(pages)
    return bool(expected) and content.get("response", "").startswith(expected)


def radio_check(client, wait_seconds=180):
    """Require a real located detection and the broker's successful terminal recording receipt."""
    deadline, accepted_detection = time.monotonic() + wait_seconds, None
    with client.stream("POST", "radio/stream", json={"playback": False}) as response:
        checked(response)
        for line in response.iter_lines():
            if time.monotonic() >= deadline:
                break
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if event.get("type") == "error":
                raise RuntimeError("Radio acceptance returned an error")
            detection = event.get("data") or {}
            if event.get("type") == "complete":
                if detection.get("success") is not True:
                    raise RuntimeError("Radio acceptance did not report successful completion")
                if accepted_detection is None:
                    raise RuntimeError("Radio acceptance completed without a location and transcript")
                return accepted_detection
            if (event.get("type") == "detection" and detection.get("location")
                    and detection.get("transcript_excerpt")):
                accepted_detection = accepted_detection or detection
    raise RuntimeError("Radio acceptance ended without successful completion, location and transcript")


# The camera check's bounded rounds (D42): each API process keeps its own latest records, so
# an image ID one process returned can be unknown to the process that answers next, and Delphire can fail once.
CAMERA_ROUNDS = 5
CAMERA_ROUND_SECONDS = 3
# An image ID that no latest records name; the broker must refuse it.
CAMERA_PROBE = "acceptance-probe"
CAMERA_IMAGE_TYPES = ("image/webp", "image/png")
CAMERA_IMAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def camera_aliases():
    """The [camera] camera_alias values the deployer accepts (deployment.installation, from the repository root)."""
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)
    from deployment.installation import CAMERA_ALIASES
    return CAMERA_ALIASES


def camera_answer(response):
    """A camera route's answer for the receipt: status and error code (null without one), never a body."""
    return {"status": response.status_code, "code": error_code(response.content) or None}


def retry_round(response):
    """True for the answers another round may change: an image another API process holds, or a Delphire outage."""
    return (response.status_code, error_code(response.content)) in ((404, "CAMERA_IMAGE_NOT_FOUND"),
                                                                     (503, "CAMERA_UNAVAILABLE"))


def round_pause(response):
    """Seconds before the next round: the broker's Retry-After (1-10), else CAMERA_ROUND_SECONDS."""
    try:
        return min(max(int(response.headers.get("retry-after", "")), 1), 10)
    except ValueError:
        return CAMERA_ROUND_SECONDS


def camera_check(client, origin, expected, receipt):
    """Check the camera routes against [camera] inside this run's demo session, filling receipt as it goes.

    origin is the workspace URL client visits. expected is "configured" or "not_configured". The routes must refuse
    a caller without a demo session, and workspace-config must report the expectation. Not configured, both routes
    answer 503 CAMERA_NOT_CONFIGURED.
    Configured, up to CAMERA_ROUNDS rounds of latest records and then their image must serve a WebP or PNG image
    (or find no image record), and an ID outside the latest records is refused. The receipt holds statuses, codes,
    content types and sizes, never image bytes.
    """
    receipt.update(expected=expected, status="running")
    # A second visitor client, without the demo cookie, gets the gate's own answer and opens no session.
    with visitor_client(origin) as anonymous:
        gate = anonymous.get("camera/latest")
    receipt["anonymous"] = camera_answer(gate)
    if (gate.status_code, error_code(gate.content)) != (401, "DEMO_SESSION_REQUIRED"):
        raise RuntimeError("The camera route did not require a demo session")
    configured = client.get("workspace-config").json().get("camera_configured")
    receipt["camera_configured"] = configured
    if bool(configured) is not (expected == "configured"):
        raise RuntimeError(f"workspace-config reports camera_configured {configured!r}, expected {expected}")
    if expected == "not_configured":
        for name, path in (("latest", "camera/latest"), ("probe", f"camera/images/{CAMERA_PROBE}")):
            response = client.get(path)
            receipt[name] = camera_answer(response)
            if (response.status_code, error_code(response.content)) != (503, "CAMERA_NOT_CONFIGURED"):
                raise RuntimeError(f"Camera {name} did not answer 503 CAMERA_NOT_CONFIGURED")
        receipt["status"] = "passed"
        return receipt
    aliases, rounds = camera_aliases(), receipt.setdefault("rounds", [])
    for number in range(1, CAMERA_ROUNDS + 1):
        latest = client.get("camera/latest")
        entry = {"latest": camera_answer(latest)}
        rounds.append(entry)
        pending = latest
        if latest.status_code == 200:
            view = latest.json()
            groups = view.get("groups") if isinstance(view, dict) else None
            if (not isinstance(view.get("device_id"), str) or view.get("camera_alias") not in aliases
                    or not isinstance(groups, list) or not groups or not isinstance(groups[0], dict)
                    or groups[0].get("alias") != view["camera_alias"]
                    or not isinstance(groups[0].get("records"), list)):
                raise RuntimeError("Camera latest records do not have the broker's shape")
            records = groups[0]["records"]
            if not records:
                receipt["image"] = "none in the latest records"
                break
            image_id = records[0].get("id") if isinstance(records[0], dict) else None
            if not isinstance(image_id, str) or not CAMERA_IMAGE_ID.fullmatch(image_id):
                raise RuntimeError("Camera latest records name an invalid image ID")
            image = client.get(f"camera/images/{image_id}")
            entry["image"] = {**camera_answer(image), "content_type": image.headers.get("content-type"),
                              "bytes": len(image.content)}
            if (image.status_code == 200 and image.headers.get("content-type") in CAMERA_IMAGE_TYPES
                    and image.content):
                receipt["image"] = "served"
                break
            pending = image
        if not retry_round(pending):
            raise RuntimeError(f"The camera answered {pending.status_code} {error_code(pending.content)}")
        if number < CAMERA_ROUNDS:
            time.sleep(round_pause(pending))
    else:
        raise RuntimeError(f"The camera served no image of its latest records in {CAMERA_ROUNDS} rounds")
    probe = client.get(f"camera/images/{CAMERA_PROBE}")
    receipt["probe"] = camera_answer(probe)
    if (probe.status_code, error_code(probe.content)) != (404, "CAMERA_IMAGE_NOT_FOUND"):
        raise RuntimeError("The camera did not refuse an image outside its latest records")
    receipt["status"] = "passed"
    return receipt


def parallel_runs(call, wait_seconds, *, checkpoint=None):
    """Prove two owned research scenarios simulate concurrently and retain complete evidence."""
    example = call("GET", "research/examples/paradise")
    seed = int(time.time()) % 2147483645
    jobs = [call("POST", "research/jobs", {
        "request_id": str(uuid4()),
        **example["area"], "highway": "State 191", "vehicles": 1500, "crews": 4,
        "duration_minutes": 60, "departure_minutes": 45, "traffic_mode": "synthetic",
        "seed": seed+index,
    }) for index in range(2)]
    deadline, snapshots, overlapping = time.monotonic()+wait_seconds, [], False
    report = {"runs": jobs, "snapshots": snapshots, "simultaneous_sumo": False,
              "checks": [], "policy_summaries": []}
    if checkpoint:
        checkpoint(report)
    while time.monotonic() < deadline:
        jobs = [call("GET", f"research/jobs/{job['id']}") for job in jobs]
        snapshot = {"at": time.time(), "jobs": [
            {key: job[key] for key in ("id", "status", "phase", "progress")} for job in jobs]}
        snapshots.append(snapshot)
        print("Parallel runs:", [(job["status"], job["phase"]) for job in jobs], flush=True)
        overlapping |= all(job["status"] == "running" and job["phase"].startswith("Simulating") for job in jobs)
        report.update(runs=jobs, simultaneous_sumo=overlapping)
        if checkpoint:
            checkpoint(report)
        if any(job["status"] in ("failed", "cancelled") for job in jobs):
            break
        if all(job["status"] == "completed" for job in jobs):
            break
        time.sleep(3)
    if not all(job["status"] == "completed" for job in jobs):
        return report
    for job in jobs:
        result = call("GET", f"research/jobs/{job['id']}/artifacts/result")
        report["policy_summaries"].append({"run_id": job["id"], "policies": [
            {"policy": p["policy"], "summary": p["summary"]} for p in result["policies"]]})
        if checkpoint:
            checkpoint(report)
        assert [policy["policy"] for policy in result["policies"]] == ["baseline", "coordinated", "phased"]
        for policy in result["policies"]:
            summary = policy["summary"]
            assert summary["valid_engine_run"] and summary["valid_accounting"]
            assert sum(summary[key] for key in ("exited_vehicles", "active_vehicles", "not_departed_vehicles")) == 1500
            assert policy["frames"] and policy["series"]
        assert result["crew_plan"] and result["input_bundle_sha256"]
    report["checks"] = ["two owned scenarios completed", "three policies and valid civilian accounting in each",
                        "crew plans, replay and frozen-input provenance retained"]
    if overlapping:
        report["checks"].append("both jobs observed inside SUMO simultaneously")
    return report


def main():
    """Check the deployed workspace's owner-scoped simulation API as an anonymous demo visitor."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Integrated workspace URL")
    parser.add_argument("--preset", choices=["paradise-2018", "palisades-2025"], default="paradise-2018")
    parser.add_argument("--submit", action="store_true")
    parser.add_argument("--parallel", action="store_true",
                        help="With --submit, verify two simultaneous research runs instead of one preset run")
    parser.add_argument("--investigate", action="store_true",
                        help="With --submit, also verify parallel fire/weather/guidance and the frozen post-run plan")
    parser.add_argument("--radio", action="store_true", help="Also verify a live fixed-recording radio detection")
    parser.add_argument("--camera", choices=["configured", "not_configured"],
                        help="Also check the camera routes against [camera] device_id, in the same demo session")
    parser.add_argument("--vehicles", type=int, help="Optional bounded preset-run vehicle count")
    parser.add_argument("--radius-km", type=float, help="Optional preset-run network radius")
    parser.add_argument("--wait-seconds", type=int, default=1800,
                        help="Seconds to wait for the road network to be ready and the run to finish")
    parser.add_argument("--output", type=Path, default=Path(".runtime/verification/deployed-smoke.json"))
    args = parser.parse_args()
    if args.parallel and not args.submit:
        parser.error("--parallel requires --submit")
    if args.investigate and (not args.submit or args.parallel):
        parser.error("--investigate requires --submit and cannot be combined with --parallel")
    report = {"status": "running"}

    def save():
        write_report(args.output, report)

    with visitor_client(args.base_url) as client:
        def call(method, path, body=None):
            try:
                return checked(client.request(method, path, json=body)).json()
            except AcceptanceError as exc:
                if args.investigate:
                    report.update(status="failed", error=str(exc))
                    save()
                raise SystemExit(str(exc)) from None
        config = call("GET", "workspace-config")
        if config.get("access_mode") != "public_demo":
            raise SystemExit(f"The workspace is not open to visitors (access_mode {config.get('access_mode')!r}); "
                             'deploy it with [access] mode = "public" first.')
        call("POST", "demo/session")
        caps = call("GET", "capabilities")
        presets = call("GET", "presets")
        if {p["preset_id"] for p in presets} != {"paradise-2018", "palisades-2025"}:
            raise SystemExit("Preset contract does not match this release")
        report = {"capability_version": caps["capability_version"], "presets": presets,
                  "checks": ["owner-scoped API"], "access_mode": config.get("access_mode")}
        if args.camera:
            report["camera"] = {}
            try:
                camera_check(client, args.base_url, args.camera, report["camera"])
                report["checks"].append(f"camera routes ({args.camera.replace('_', ' ')})")
                save()
            except Exception as exc:
                report["camera"]["status"] = "failed"
                report.update(status="failed", error=str(exc))
                save()
                raise
        if args.investigate:
            def save_conversation(receipts):
                report["conversation"] = receipts
                save()
            try:
                report["conversation"] = investigation_conversation(client, checkpoint=save_conversation)
                report["checks"].append("topic routing, retained fire/weather follow-ups and combined guidance")
                save()
                # Reuse one existing source answer. Chat has already finished; only saved owner-scoped GETs
                # await its durable terrain work, independently of the later SUMO/preparation wait.
                source = next(receipt["snapshot"]["content"] for receipt in reversed(report["conversation"])
                              if observed_fires(receipt["snapshot"]["content"]))

                def save_terrain(value):
                    report["terrain"] = value
                    save()
                report["terrain"] = wait_terrain_jobs(client, source, checkpoint=save_terrain, require_current=True)
                report["checks"].append("background terrain terminal receipts and exact retained source fire")
                save()
            except Exception as exc:
                report.update(status="failed", error=str(exc))
                save()
                raise
        if args.radio:
            report["radio_detection"] = radio_check(client)
            report["checks"].append("live radio transcription and location detection")

        def require(condition, message):
            if not condition:
                report.update(status="failed", error=message)
                save()
                raise SystemExit(message)

        def await_plan(path, task, stage):
            # The service's 420-second task lease is independent of simulation time.
            deadline = time.monotonic() + min(args.wait_seconds, 450)
            while task["status"] == "running" and time.monotonic() < deadline:
                report[stage] = task
                save()
                print(stage, task["status"], flush=True)
                time.sleep(3)
                task = call("GET", path)
            report[stage] = task
            require(task["status"] == "completed", f"{stage}: {task.get('error') or task['status']}")
            require(bool(task["snapshot"]["content"].get("knowledgeBaseCitations")),
                    f"{stage}: validated official citations are missing")
            save()
            return task

        if args.parallel:
            def save_parallel(value):
                report["parallel"] = value
                save()
            report["parallel"] = parallel_runs(call, args.wait_seconds, checkpoint=save_parallel)
            report["checks"].extend(report["parallel"]["checks"])
        elif args.submit:
            end = time.monotonic()+args.wait_seconds
            session = call("POST", "sessions", {"request_id": str(uuid4()), "entry_mode": "lab"})
            incident = call("POST", "incidents", {"request_id": str(uuid4()), "session_id": session["session_id"],
                                                 "preset_id": args.preset})
            draft_body = {"request_id": str(uuid4()), "session_id": session["session_id"],
                          "incident_id": incident["incident_id"], "location_revision": incident["location_revision"]}
            draft_body.update({key: value for key, value in (
                ("vehicles", args.vehicles), ("radius_km", args.radius_km)) if value is not None})
            path = f"incidents/{incident['incident_id']}/investigation"
            initial = None
            if args.investigate:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    investigating = pool.submit(call, "POST", path, {})
                    preparing = pool.submit(call, "POST", "scenario-drafts", draft_body)
                    initial, draft = investigating.result(), preparing.result()
                report.update(initial=initial, parallel_investigation_preparation=False, status="running")
            else:
                draft = call("POST", "scenario-drafts", draft_body)
            # Road-network preparation runs in the background, as in the browser: a new draft is submitted only
            # once it is ready, at the revision the ready draft reports.
            while draft["status"] == "preparing" and time.monotonic() < end:
                if args.investigate:
                    initial = call("GET", path)
                    report.update(initial=initial, draft=draft, incident=incident)
                    report["parallel_investigation_preparation"] |= initial["status"] == "running"
                    save()
                print(draft["status"], draft.get("phase"), flush=True)
                time.sleep(5)
                draft = call("GET", f"scenario-drafts/{draft['draft_id']}")
            if draft["status"] != "ready":
                detail = draft.get("error") or f"still {draft['status']} after {args.wait_seconds} s"
                if args.investigate:
                    require(False, f"The scenario draft did not become ready: {detail}")
                raise SystemExit(f"The scenario draft did not become ready: {detail}")
            if args.investigate:
                initial = await_plan(path, initial, "initial")
                require(report["parallel_investigation_preparation"],
                        "Initial investigation and road preparation were not observed concurrently")
                content, point = initial["snapshot"]["content"], incident["location"]
                require(any(item.get("currentWeather")
                            and abs(item["latitude"]-point["lat"]) < 0.000001
                            and abs(item["longitude"]-point["lon"]) < 0.000001
                            for item in (content.get("mapData") or {}).get("locations", [])),
                        "Saved weather at the verified scenario point is missing")
                require(bool(content.get("forecastData")), "Saved forecast is missing")
                require(bool(initial["activity"]), "No actual tool lifecycle was saved")
                require(call("POST", path, {})["completed_at"] == initial["completed_at"],
                        "Completed initial investigation did not rejoin its saved receipt")
                report["checks"].extend(["parallel investigation and road preparation",
                                          "point weather, forecast, cited initial plan and tool lifecycle",
                                          "completed initial plan rejoins without recomputation"])
            submission = {"request_id": str(uuid4()), "draft_id": draft["draft_id"],
                          "draft_revision": draft["draft_revision"], "capability_version": caps["capability_version"]}
            job = call("POST", "runs", submission)
            again = call("POST", "runs", submission)
            if job["run_id"] != again["run_id"]:
                raise SystemExit("Idempotency verification failed")
            report.update(incident=incident, draft=draft, run_id=job["run_id"])
            report["checks"].append("duplicate submission returned the same run")
            while job["status"] in ("queued", "running", "cancelling") and time.monotonic() < end:
                print(job["status"], job["phase"], flush=True)
                time.sleep(5)
                job = call("GET", f"runs/{job['run_id']}")
                if args.investigate:
                    report["run"] = job
                    save()
            report["run"] = job
            if job["status"] == "completed":
                for view in ("outcomes", "crew", "comparison", "sources", "assumptions"):
                    query = {"view": view, "run_ids": [job["run_id"]]}
                    pages = []
                    while True:
                        page = call("POST", "evidence", query)
                        pages.append(page)
                        if not page["pagination"]["has_more"]:
                            break
                        query["cursor"] = page["pagination"]["next_cursor"]
                    report[view] = pages
                report["checks"].append("completed run and paginated evidence")
                if args.investigate:
                    result = call("GET", f"runs/{job['run_id']}/artifacts/result")
                    report["policy_summaries"] = [{"policy": p["policy"], "summary": p["summary"]}
                                                  for p in result["policies"]]
                    save()
                    require([p["policy"] for p in result["policies"]] == ["baseline", "coordinated", "phased"],
                            "Not every policy was completed")
                    for policy in result["policies"]:
                        summary = policy["summary"]
                        require(summary["valid_engine_run"] and summary["valid_accounting"]
                                and sum(summary[k] for k in ("exited_vehicles", "active_vehicles", "not_departed_vehicles"))
                                == result["scenario"]["vehicles"] and bool(policy["frames"]) and bool(policy["series"]),
                                f"{policy['policy']}: invalid accounting, engine or replay")
                    require(bool(result["crew_plan"]), "Crew plan is missing")
                    body = {"run_id": job["run_id"]}
                    updated = await_plan(path+"?run_id="+job["run_id"], call("POST", path, body), "updated")
                    require(bool(updated["input_bundle_id"])
                            and updated["input_bundle_id"] == result["input_bundle_id"]
                            and updated["input_bundle_sha256"] == result["input_bundle_sha256"],
                            "Updated plan does not carry the completed run's frozen input identity")
                    require(call("POST", path, body)["completed_at"] == updated["completed_at"],
                            "Completed updated plan did not rejoin its saved receipt")
                    require(verify_plan_summary(updated["snapshot"]["content"], [
                        page for view in ("outcomes", "crew", "comparison", "sources", "assumptions")
                        for page in report[view]]),
                        "Updated plan does not preserve the deterministic facts and paired comparisons")
                    report["checks"].extend(["all three SUMO policies, accounting, crew plans and replay",
                                              "cited updated plan uses the exact frozen input identity",
                                              "completed updated plan rejoins without recomputation"])
                    report["status"] = "passed"
            elif args.investigate:
                require(False, f"Run did not complete: {job.get('error') or job['status']}")
        if args.camera and not args.submit:
            # The camera check alone (one demo session) passed; a --submit run sets its own status.
            report["status"] = "passed"
        save()
        print("Smoke-test receipt:", args.output)
        if args.parallel:
            if (not report["parallel"]["simultaneous_sumo"]
                    or not all(job["status"] == "completed" for job in report["parallel"]["runs"])):
                raise SystemExit("Parallel SUMO execution was not verified; inspect the retained snapshots.")
        elif args.submit and report["run"]["status"] != "completed":
            raise SystemExit("Run did not complete; inspect the retained status. It was not reported as successful.")


if __name__ == "__main__":
    main()
