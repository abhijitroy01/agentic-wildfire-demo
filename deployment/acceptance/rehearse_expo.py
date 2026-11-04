"""Bounded shared-network rehearsal with real visitor sessions and explicit work submission.

Run separately from a release: this spends session/provider/model budgets. It
checks admission, polling, ownership, cancellation and recovery, not venue capacity.
"""
import argparse
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from deployment.acceptance.background_jobs import wait_terrain_jobs
from deployment.acceptance.visitor import checked, visitor_client, write_report


def admission(response):
    """A refused start must be an explicit busy response with a usable retry hint."""
    data = response.json()
    if response.status_code == 200 or response.status_code == 201:
        return data
    if response.status_code == 202:
        return data
    error = data.get("error", {})
    if (response.status_code != 503 or error.get("code") != "SIMULATOR_BUSY"
            or error.get("retryable") is not True
            or not response.headers.get("Retry-After", "").isdigit()
            or int(response.headers["Retry-After"]) < 1):
        raise RuntimeError(f"Unexpected admission response: {response.status_code} {error.get('code')}")
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--submit", action="store_true", help="Required acknowledgement of real work and budget use")
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--ai-users", type=int, default=2)
    parser.add_argument("--wait-seconds", type=int, default=1800)
    parser.add_argument("--output", type=Path, default=Path(".runtime/verification/expo-rehearsal.json"))
    args = parser.parse_args()
    if not args.submit or not 6 <= args.users <= 50 or not 0 <= args.ai_users <= min(8, args.users):
        parser.error("Use --submit, 6–50 users and 0–8 AI users (no more than the visitor count)")
    report = {"status": "running", "users": args.users, "ai_users": args.ai_users,
              "started_at": time.time(), "checks": [], "jobs": [], "investigations": [], "snapshots": []}
    samples, lock = [], threading.Lock()

    def save():
        write_report(args.output, report)

    def request(client, method, path, body=None, *, raw=False):
        started = time.monotonic()
        response = client.request(method, path, json=body)
        with lock:
            samples.append({"path": path.split("?", 1)[0], "status": response.status_code,
                            "seconds": round(time.monotonic() - started, 3)})
        return response if raw else checked(response).json()

    try:
        with ExitStack() as stack, ThreadPoolExecutor(max_workers=args.users) as pool:
            clients = [stack.enter_context(visitor_client(args.base_url)) for _ in range(args.users)]
            opened = list(pool.map(lambda c: request(c, "POST", "demo/session"), clients))
            assert all(item["session_created"] for item in opened), "Fresh visitor sessions were not created"
            reused = list(pool.map(lambda c: request(c, "POST", "demo/session"), clients))
            assert all(not item["session_created"] for item in reused), "Reload consumed new-session allowances"
            report["checks"].append("fresh visitors on one network; session cookies reused without new allowances")
            example = request(clients[0], "GET", "research/examples/paradise")
            scenario = {**example["area"], "radius_km": 2, "highway": "State 191", "vehicles": 200,
                        "crews": 4, "duration_minutes": 30, "departure_minutes": 10,
                        "traffic_mode": "synthetic", "seed": 42}
            starts = list(pool.map(lambda c: admission(
                request(c, "POST", "research/jobs", {**scenario, "request_id": str(uuid4())}, raw=True)), clients))
            jobs = {index: job for index, job in enumerate(starts) if job}
            refused = [index for index, job in enumerate(starts) if job is None]
            report.update(jobs=list(jobs.values()), refused_users=len(refused))
            save()
            assert jobs and refused, "The work burst did not exercise both admitted and busy visitors"
            assert len(jobs) <= 5, "Shared job admission exceeded this installation's five-job limit"
            report["checks"].append("work burst preserves shared admission and returns explicit busy/retry responses")
            victim = next(iter(jobs))
            foreign = (victim + 1) % args.users
            denied = request(clients[foreign], "GET", f"research/jobs/{jobs[victim]['id']}", raw=True)
            assert denied.status_code == 404, "Another visitor could read the run"
            report["checks"].append("foreign visitor cannot read another owner's run")
            cancel_index = next(reversed(jobs))
            cancel_id = jobs[cancel_index]["id"]
            request(clients[cancel_index], "POST", f"research/jobs/{cancel_id}/cancel", {})
            plans = {}
            for index in range(args.ai_users):
                c = clients[index]
                session = request(c, "POST", "sessions", {"request_id": str(uuid4()), "entry_mode": "lab"})
                incident = request(c, "POST", "incidents", {
                    "request_id": str(uuid4()), "session_id": session["session_id"], "preset_id": "paradise-2018"})
                path = f"incidents/{incident['incident_id']}/investigation"
                plans[index] = (path, request(c, "POST", path, {}))
            deadline, simultaneous, last_states = time.monotonic() + args.wait_seconds, False, None
            while time.monotonic() < deadline:
                # All visitors poll their own history, including those shown a busy message.
                histories = list(pool.map(lambda c: request(c, "GET", "research/jobs"), clients))
                for index, job in jobs.items():
                    job = next(item for item in histories[index] if item["id"] == job["id"])
                    jobs[index] = job
                for index, (path, plan) in list(plans.items()):
                    if plan["status"] == "running":
                        plans[index] = (path, request(clients[index], "GET", path))
                states = [{key: job[key] for key in ("id", "status", "phase")} for job in jobs.values()]
                simultaneous |= sum(item["status"] == "running" and item["phase"].startswith("Simulating")
                                    for item in states) >= 2
                report.update(jobs=list(jobs.values()), investigations=[p for _, p in plans.values()],
                              simultaneous_sumo=simultaneous)
                report["snapshots"].append({"at": time.time(), "jobs": states})
                save()
                current_states = ([j["status"] for j in jobs.values()], [p["status"] for _, p in plans.values()])
                if current_states != last_states:
                    print("Visitors:", args.users, "jobs:", current_states[0], "plans:", current_states[1], flush=True)
                    last_states = current_states
                if (all(j["status"] in ("completed", "failed", "cancelled") for j in jobs.values())
                        and all(p["status"] != "running" for _, p in plans.values())):
                    break
                time.sleep(2.5)
            assert jobs[cancel_index]["status"] == "cancelled", "Cancellation did not reach an authoritative terminal state"
            assert all(j["status"] == "completed" for i, j in jobs.items() if i != cancel_index), "An admitted run failed"
            summaries = []
            for index, job in jobs.items():
                if index == cancel_index:
                    continue
                result = request(clients[index], "GET", f"research/jobs/{job['id']}/artifacts/result")
                summaries.append({"id": job["id"], "policies": [
                    {"policy": p["policy"], "summary": p["summary"]} for p in result["policies"]]})
                report["policy_summaries"] = summaries
                save()
                assert [p["policy"] for p in result["policies"]] == ["baseline", "coordinated", "phased"]
                for policy in result["policies"]:
                    s = policy["summary"]
                    assert s["valid_accounting"] and s["valid_engine_run"], "Invalid policy result"
                    assert sum(s[k] for k in ("exited_vehicles", "active_vehicles", "not_departed_vehicles")) == 200
                    assert policy["frames"] and policy["series"]
                assert result["crew_plan"] and result["input_bundle_sha256"]
            # Deliberate visitor retry only after all previous jobs actually ended.
            retry_index = refused[0]
            retried = admission(request(clients[retry_index], "POST", "research/jobs",
                                        {**scenario, "request_id": str(uuid4())}, raw=True))
            assert retried is not None, "Admission did not recover after completed/cancelled work"
            report["recovery_run"] = retried
            request(clients[retry_index], "POST", f"research/jobs/{retried['id']}/cancel", {})
            deadline = time.monotonic() + args.wait_seconds
            while time.monotonic() < deadline:
                report["recovery_run"] = request(
                    clients[retry_index], "GET", f"research/jobs/{retried['id']}")
                save()
                if report["recovery_run"]["status"] in ("cancelled", "failed", "completed"):
                    break
                time.sleep(2.5)
            assert report["recovery_run"]["status"] == "cancelled", "Recovery cancellation did not finish"
            assert all(p["status"] == "completed" and p["snapshot"]["content"].get("knowledgeBaseCitations")
                       for _, p in plans.values()), "An admitted investigation did not complete with citations"
            # Existing plan snapshots may carry background terrain references. Rejoin those saved jobs after
            # chat completion; the rehearsal's SUMO wait never invents a terrain deadline or unavailable result.
            report["terrain"] = {}
            for index, (_, plan) in plans.items():
                def save_terrain(value, index=index):
                    report["terrain"][str(index)] = value
                    save()
                report["terrain"][str(index)] = wait_terrain_jobs(
                    clients[index], plan["snapshot"]["content"], checkpoint=save_terrain)
            save()
            report["checks"].extend(["cancellation retains capacity until terminal state; subsequent start admitted",
                                     "admitted runs retain all policy ledgers, replay, crew plans and frozen inputs",
                                     "real cited investigations complete while visitor polling and SUMO execute"])
            if simultaneous:
                report["checks"].append("two jobs observed executing SUMO together")
            report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        durations = [s["seconds"] for s in samples]
        report.update(finished_at=time.time(), http_requests=samples,
                      latency_seconds={"median": statistics.median(durations) if durations else None,
                                       "p95": sorted(durations)[int((len(durations)-1)*0.95)] if durations else None})
        save()
    print("Expo rehearsal receipt:", args.output)


if __name__ == "__main__":
    main()
