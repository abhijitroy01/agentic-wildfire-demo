"""Live private firetruck/report acceptance; creates and stops one owned test run."""
import argparse
import asyncio
import json
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from deployment.acceptance.visitor import checked, visitor_client, write_report


async def stream_check(url, headers, foreign_headers, origin):
    """Acceptance check: the owner's firetruck stream sends truck updates and another browser is refused."""
    async with connect(url, additional_headers=headers, origin=origin, open_timeout=30) as socket:
        kinds = set()
        for _ in range(6):
            event = json.loads(await asyncio.wait_for(socket.recv(), timeout=30))
            kinds.add(event.get("event_type"))
            if "TRUCK_UPDATE" in kinds:
                break
        if "TRUCK_UPDATE" not in kinds:
            raise RuntimeError("Owned stream did not deliver truck evidence")
    try:
        async with connect(url, additional_headers=foreign_headers, origin=origin, open_timeout=30):
            raise RuntimeError("Another browser could open the run's stream")
    except InvalidStatus as exc:
        if exc.response.status_code not in (401, 403, 404):
            raise RuntimeError("Foreign stream failed with an unexpected status") from None
    return sorted(k for k in kinds if k)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--assumed-latitude", type=float,
                        help="Explicit synthetic point for firetruck-only acceptance when geocoding is unavailable")
    parser.add_argument("--assumed-longitude", type=float)
    args = parser.parse_args()
    origin = args.base_url.rstrip("/")
    if urlsplit(origin).scheme != "https" or urlsplit(origin).path:
        raise SystemExit("Use the deployed HTTPS origin")
    if (args.assumed_latitude is None) != (args.assumed_longitude is None):
        raise SystemExit("Supply both assumed coordinates, or neither")
    report = {"origin": origin, "started_at": time.time(), "checks": [], "status": "running"}
    run_id = None
    # The report HTML is saved beside the receipt before the receipt is written.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with visitor_client(origin) as owner, visitor_client(origin) as other:
        def call(client, method, path, body=None):
            return checked(client.request(method, path, json=body)).json()

        def denied(method, path, body=None):
            response = other.request(method, path, json=body)
            if response.status_code not in (403, 404):
                raise RuntimeError(f"Foreign {method} {path}: HTTP {response.status_code}")

        try:
            for client in (owner, other):
                call(client, "POST", "demo/session")
            call(owner, "GET", "firetruck/health")
            if args.assumed_latitude is None:
                session = call(owner, "POST", "sessions", {"request_id": str(uuid4()), "entry_mode": "lab"})
                incident = call(owner, "POST", "incidents", {
                    "request_id": str(uuid4()), "session_id": session["session_id"], "preset_id": "palisades-2025"})
                location = incident["location"]
                report["checks"].append("Amazon Location preset resolution")
            else:
                location = {"lat": args.assumed_latitude, "lon": args.assumed_longitude,
                            "meaning": "Explicit synthetic test point; geocoding was not tested"}
            report["location"] = location
            body = {"request_id": str(uuid4()), "fire_location": {
                "latitude": location["lat"], "longitude": location["lon"], "name": "Deployment acceptance"},
                "initial_truck_count": 4, "max_truck_count": 12}
            run_id = call(owner, "POST", "firetruck/runs", body)["session_id"]
            report["run_id"] = run_id
            if call(owner, "POST", "firetruck/runs", body)["session_id"] != run_id:
                raise RuntimeError("Repeated start created another run")
            path = f"firetruck/runs/{run_id}"
            for method in ("GET", "DELETE"):
                denied(method, path)
            if any(x["session_id"] == run_id for x in call(other, "GET", "firetruck/runs")):
                raise RuntimeError("Foreign run leaked through listing")
            socket_url = origin.replace("https://", "wss://")+f"/simulation-api/v1/{path}/stream"
            cookie = lambda client: {"Cookie": "; ".join(f"{k}={v}" for k, v in client.cookies.items())}
            report["stream_events"] = asyncio.run(stream_check(
                socket_url, cookie(owner), cookie(other), origin))
            report["checks"].extend(["idempotent start",
                                     "owned stream", "foreign read/stop/list/stream denied"])
            call(owner, "DELETE", path)
            deadline = time.monotonic()+60
            while True:
                evidence = call(owner, "GET", path)
                if evidence["status"] == "stopped":
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError("Test run did not stop")
                time.sleep(2)
            report["evidence"] = evidence
            question = {"request_id": str(uuid4()),
                        "question": "Explain the recorded stages and resource activity. Identify stages not reached "
                                    "and missing data. State that this is synthetic telemetry."}
            denied("POST", path+"/reports", question)
            invalid = owner.post(path+"/reports", json={**question, "telemetry": {"invented": True}})
            if invalid.status_code != 422:
                raise RuntimeError("Client telemetry was not rejected")
            result = call(owner, "POST", path+"/reports", question)
            report_id = result["report_id"]
            report["report_id"] = report_id
            deadline = time.monotonic()+240
            while result["status"] == "running" and time.monotonic() < deadline:
                time.sleep(3)
                result = call(owner, "GET", f"firetruck/reports/{report_id}")
            if result["status"] != "completed":
                raise RuntimeError(f"Report did not complete: {result['status']}")
            denied("GET", f"firetruck/reports/{report_id}")
            denied("GET", f"firetruck/reports/{report_id}/html")
            response = owner.get(f"firetruck/reports/{report_id}/html")
            if response.status_code != 200 or "<script" in response.text.lower():
                raise RuntimeError("Report HTML is unavailable or contains scripts")
            if "sandbox" not in response.headers.get("content-security-policy", ""):
                raise RuntimeError("Report sandbox policy is absent")
            if run_id not in response.text or "Synthetic" not in response.text:
                raise RuntimeError("Report lacks source run or synthetic-data labeling")
            args.output.with_suffix(".html").write_text(response.text)
            report["checks"].extend(["stop retains evidence", "client telemetry rejected",
                                     "live AgentCore report", "foreign report access denied",
                                     "sandboxed escaped report HTML"])
            report["status"] = "passed"
        except Exception as exc:
            report.update(status="failed", error=str(exc))
            raise
        finally:
            if run_id:
                try:
                    owner.delete(f"firetruck/runs/{run_id}")
                except httpx.HTTPError:
                    pass
            report["finished_at"] = time.time()
            write_report(args.output, report)
    print("Firetruck/report acceptance passed:", args.output)


if __name__ == "__main__":
    main()
