"""SUMO experiments: fixed demand, conserved vehicles, no automatic teleportation; policies may run in parallel."""
import logging
import math
import multiprocessing
import queue
import socket
import subprocess  # nosec B404 - starts only the server-built SUMO argument list below, never a shell
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

import sumolib
import traci
from shapely.geometry import LineString, Point

from traffic_simulator import settings
from traffic_simulator.demand import demand_for
from traffic_simulator.network import binary
from traffic_simulator.timings import Timings

# How long stopped policy children may take to reach their next cancellation check before the parent terminates
# them. Shorter than the worker task's 120-second stop timeout.
STOP_GRACE_SECONDS = 60
POLICIES = {
    "baseline": "Uncoordinated departures and fixed crew dispatch",
    "coordinated": "Adaptive civilian rerouting and staged crew dispatch",
    "phased": "Near-fire departures first, adaptive rerouting and staged dispatch",
}
logger = logging.getLogger(__name__)


def engine_log_tail(path, characters=2000):
    """Return the last characters of a SUMO engine log (it names server paths and inputs), or "" when it is absent."""
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - characters * 4))
            return handle.read().decode(errors="replace")[-characters:]
    except OSError:
        return ""


def stop_engine(process):
    """Reap only the SUMO child this policy started."""
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)



def radius_at(scenario, seconds):
    """Hazard radius in meters at a simulated time: zero before start, then linear growth."""
    if seconds < scenario.hazard_start_minutes * 60:
        return 0.0
    return scenario.hazard_radius_m + (
        seconds - scenario.hazard_start_minutes * 60) / 60 * scenario.hazard_growth_m_min


def hold_closed_origins(conn, newly_closed, records, submitted, inserted):
    """Cancel only not-yet-departed insertions; keep their records in the demand ledger.

    This includes future departures and vehicles waiting for an insertion gap.
    Already-departed vehicles must remain in SUMO, even on a newly closed edge.
    """
    held = {vid for vid, record in records.items()
            if record["route"] and record["origin"] in newly_closed and vid not in inserted}
    for vid in sorted(held & submitted):
        conn.vehicle.remove(vid)
        submitted.remove(vid)
    return held


def run_policy(scenario, policy, prepared, demand, progress, check_cancel, traffic=None, logfolder=None):
    """Simulate one policy in its own SUMO process (own port, the scenario seed, own log) and return its result.

    The engine log is deleted when the policy ends; after an engine (TraCI) failure its tail goes to the server log
    first, so the disk never accumulates logs.
    """
    netfile, net, _edges, lines, _gates, fire_xy, _, _ = prepared
    civilians, crews = demand
    label = uuid.uuid4().hex
    logfolder = logfolder or settings.data_dir("engine-logs")
    logfile = logfolder / f"{label}.log"
    args = [
        binary("sumo"), "-n", str(netfile), "--step-length", "1", "--seed", str(scenario.seed),
        "--time-to-teleport", "-1", "--time-to-teleport.disconnected", "-1",
        "--collision.action", "warn", "--collision.check-junctions", "true",
        "--no-step-log", "true", "--duration-log.disable", "true",
        "--ignore-route-errors", "false", "--max-depart-delay", "-1",
        "--log", str(logfile),
    ]
    conn = None
    process = None
    engine_failed = False
    frames, series = [], []
    ended, inserted, exposed, staged, failed_insert = set(), set(), set(), {}, set()
    submitted, blocked_origins = set(), set()
    pending_crews = {c["id"]: dict(c) for c in crews if c["route"]}
    all_records = {d["id"]: d for d in civilians + crews}
    civilian_ids = {d["id"] for d in civilians}
    crew_ids = {d["id"] for d in crews}
    routed_ids = {d["id"] for d in civilians + crews if d["route"]}
    closed = set()
    min_distances = {eid: line.distance(Point(fire_xy)) for eid, line in lines.items()}
    teleports, collisions = 0, 0
    reroute_failures = 0
    horizon = scenario.duration_minutes * 60
    frame_every = max(5, math.ceil(horizon / 360))
    departed_at, arrival_times = {}, {}
    try:
        # Fire crews use passenger access rules intentionally: no blue-light device,
        # opposite-direction driving, red-light exceptions, or silent teleporting.
        for attempt in range(3):
            check_cancel()
            try:
                with socket.socket() as probe:
                    # SUMO binds all interfaces. A loopback-only probe can select a
                    # port already used on another interface (including an outbound client).
                    probe.bind(("", 0))
                    port = probe.getsockname()[1]
            except OSError as exc:
                raise RuntimeError(
                    "SUMO requires a local socket. This execution environment does not permit one."
                ) from exc
            # The SUMO binary and every switch and file name are server built; no shell.
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            process = subprocess.Popen([*args, "--remote-port", str(port)],  # nosec B603 - fixed arguments
                                       stdout=subprocess.DEVNULL)
            try:
                conn = traci.connect(port=port, host="127.0.0.1", numRetries=30, proc=process,
                                     waitBetweenRetries=1, label=label)
                break
            except (traci.TraCIException, traci.FatalTraCIError):
                # Only retry a failed bind before any demand or simulation steps.
                # Never restart an engine failure and present it as a valid result.
                if attempt == 2 or "Address already in use" not in engine_log_tail(logfile):
                    raise
                stop_engine(process)
                logfile.unlink(missing_ok=True)
        conn.vehicletype.copy("DEFAULT_VEHTYPE", "civilian")
        conn.vehicletype.setSpeedFactor("civilian", scenario.background_speed_factor)
        conn.vehicletype.copy("DEFAULT_VEHTYPE", "crew")
        conn.vehicletype.setLength("crew", 9)
        conn.vehicletype.setAccel("crew", 1.1)
        conn.vehicletype.setDecel("crew", 3.5)
        conn.vehicletype.setMaxSpeed("crew", 22)
        conn.vehicletype.setMinGap("crew", 3)
        for match in (traffic or {}).get("matches", []):
            for lane in net.getEdge(match["edge_id"]).getLanes():
                conn.lane.setMaxSpeed(lane.getID(), min(lane.getSpeed(), match["speed_limit_m_s"]))
        for record in civilians:
            if not record["route"]:
                continue
            depart = record["depart"]
            if policy == "phased":
                # Fixed, documented departure shift; deferred people remain in the ledger.
                fraction = min(1, record["distance_m"] / (scenario.radius_km * 850))
                depart = depart * 0.45 + fraction * scenario.departure_minutes * 60 * 0.55
            conn.route.add(record["id"], record["route"])
            try:
                conn.vehicle.add(record["id"], record["id"], typeID="civilian",
                                 depart=str(round(depart, 1)), departLane="best", departSpeed="0")
                submitted.add(record["id"])
            except traci.TraCIException:
                failed_insert.add(record["id"])

        def crew_timeline():
            states = []
            for record in crews:
                vid = record["id"]
                if not record["route"]:
                    state = "no_route"
                elif vid in ended:
                    state = "returned"
                elif vid not in inserted:
                    state = "origin_closed" if vid in blocked_origins else "waiting_to_depart"
                else:
                    stage_index = record.get("stage_index", record["route"].index(record["stage"]))
                    route_index = conn.vehicle.getRouteIndex(vid)
                    if route_index > stage_index:
                        state = "returning"
                    elif route_index == stage_index:
                        state = "staging_stop" if conn.vehicle.isStopped(vid) else "on_staging_segment"
                    else:
                        state = "to_staging"
                states.append({
                    "id": vid, "state": state,
                    "scheduled_departure_s": record["depart"],
                    "departed_s": departed_at.get(vid),
                    "reached_staging_s": staged.get(vid),
                    "returned_s": arrival_times.get(vid),
                })
            return states

        frames.append({"t": 0, "vehicles": [], "hazard_radius_m": round(radius_at(scenario, 0)),
                       "closed_edges": [], "crews": crew_timeline()})
        series.append({"t": 0, "exited": 0, "active": 0, "not_departed": scenario.vehicles,
                       "queue": 0, "exposed": 0, "crews_staged": 0, "closed_edges": 0})
        for second in range(horizon):
            if second % 5 == 0:
                check_cancel()
            hazard = radius_at(scenario, second)
            if second % 5 == 0 and hazard > 0:
                newly_closed = {eid for eid, dist in min_distances.items() if dist <= hazard} - closed
                # SUMO treats departure onto a fully prohibited edge as fatal.
                # Withdraw pending insertions before changing lane permissions.
                held = hold_closed_origins(conn, newly_closed, all_records, submitted, inserted)
                blocked_origins.update(held)
                for vid in held:
                    pending_crews.pop(vid, None)
                for eid in sorted(newly_closed):
                    for lane in net.getEdge(eid).getLanes():
                        conn.lane.setDisallowed(lane.getID(), ["passenger"])
                closed.update(newly_closed)
            for crew_id, record in list(pending_crews.items()):
                if second < record["depart"]:
                    continue
                route = record["route"]
                if policy != "baseline" and second < record["depart"] + 120:
                    congested = any(conn.edge.getLastStepHaltingNumber(eid) > 8 for eid in route[:5])
                    if congested:
                        continue
                try:
                    conn.route.add(crew_id, route)
                    conn.vehicle.add(crew_id, crew_id, typeID="crew", depart="now",
                                     departLane="best", departSpeed="0")
                    submitted.add(crew_id)
                    # Keep an explicit service stop. This is an assumed 120-second task.
                    stage = net.getEdge(record["stage"])
                    conn.vehicle.setStop(crew_id, stage.getID(), pos=max(1, stage.getLength() - 5),
                                         duration=120)
                except traci.TraCIException:
                    if crew_id in submitted:
                        conn.vehicle.remove(crew_id)
                        submitted.remove(crew_id)
                    failed_insert.add(crew_id)
                del pending_crews[crew_id]
            conn.simulationStep()
            for vid in conn.simulation.getDepartedIDList():
                inserted.add(vid)
                departed_at[vid] = second + 1
            for vid in conn.simulation.getArrivedIDList():
                ended.add(vid)
                arrival_times[vid] = second + 1
            teleports += conn.simulation.getStartingTeleportNumber()
            collisions += conn.simulation.getCollidingVehiclesNumber()
            active = conn.vehicle.getIDList()
            if policy != "baseline" and second > 0 and second % 30 == 0:
                # Only a quarter of evacuees reroute per interval to reduce synchronous route flipping.
                for vid in active:
                    if vid in civilian_ids and (int(vid.split("-")[1]) + second // 30) % 4 == 0:
                        try:
                            conn.vehicle.rerouteTraveltime(vid, currentTravelTimes=True)
                        except traci.TraCIException:
                            reroute_failures += 1
            positions = []
            queue = 0
            for vid in active:
                x, y = conn.vehicle.getPosition(vid)
                speed = conn.vehicle.getSpeed(vid)
                if math.hypot(x - fire_xy[0], y - fire_xy[1]) <= hazard and hazard > 0:
                    exposed.add(vid)
                if speed < 0.5:
                    queue += 1
                if (vid in crew_ids and vid not in staged
                        and conn.vehicle.getRoadID(vid) == all_records[vid].get("stage")):
                    staged[vid] = second + 1
                if (second + 1) % frame_every == 0 or second + 1 == horizon:
                    lon, lat = net.convertXY2LonLat(x, y)
                    positions.append([vid, round(lon, 6), round(lat, 6), round(speed, 1),
                                      1 if vid in crew_ids else 0])
            for vid in set(all_records) - inserted:
                if "origin_xy" not in all_records[vid]:
                    continue
                x, y = all_records[vid]["origin_xy"]
                if hazard > 0 and math.hypot(x - fire_xy[0], y - fire_xy[1]) <= hazard:
                    exposed.add(vid)
            if (second + 1) % frame_every == 0 or second + 1 == horizon:
                metrics = {
                    "t": second + 1, "exited": len(ended & civilian_ids),
                    "active": len((inserted - ended) & civilian_ids),
                    "not_departed": len(civilian_ids - inserted), "queue": queue,
                    "exposed": len(exposed & civilian_ids), "crews_staged": len(staged),
                    "closed_edges": len(closed),
                }
                series.append(metrics)
                frames.append({"t": second + 1, "vehicles": positions, "hazard_radius_m": round(hazard),
                               "closed_edges": sorted(closed), "crews": crew_timeline()})
            if (second + 1) % 60 == 0:
                progress(f"{POLICIES[policy]} · {(second + 1) // 60}/{scenario.duration_minutes} min",
                         (second + 1) / horizon)
        exited = len(ended & civilian_ids)
        active_civilians = len((inserted - ended) & civilian_ids)
        not_departed = len(civilian_ids - inserted)
        if exited + active_civilians + not_departed != scenario.vehicles:
            raise RuntimeError("Vehicle conservation invariant failed")
        times = sorted(arrival_times[v] for v in ended & civilian_ids)
        target = math.ceil(scenario.vehicles * 0.95)
        t95 = times[target - 1] if len(times) >= target else None
        crew_reached = list(staged.values())
        summary = {
            "demand_vehicles": scenario.vehicles, "demand_people": scenario.vehicles * scenario.people_per_vehicle,
            "exited_vehicles": exited, "exited_people": exited * scenario.people_per_vehicle,
            "active_vehicles": active_civilians, "not_departed_vehicles": not_departed,
            "unresolved_vehicles": scenario.vehicles - exited,
            "unroutable_vehicles": len(civilian_ids - routed_ids),
            "origin_blocked_vehicles": len(blocked_origins & civilian_ids),
            "insertion_errors": len(failed_insert), "exposed_vehicles": len(exposed & civilian_ids),
            "exposed_people": len(exposed & civilian_ids) * scenario.people_per_vehicle,
            "exposed_crews": len(exposed & crew_ids),
            "t95_seconds": t95, "crews_requested": scenario.crews, "crews_staged": len(staged),
            "crews_returned": len(ended & crew_ids),
            "crews_not_departed": len(crew_ids - inserted),
            "crews_without_route": len(crew_ids - routed_ids),
            "crews_origin_blocked": len(blocked_origins & crew_ids),
            "crew_mean_staging_seconds": round(sum(crew_reached) / len(crew_reached)) if crew_reached else None,
            "teleports": teleports, "collision_events": collisions,
            "reroute_failures": reroute_failures, "closed_edges": len(closed),
            "valid_accounting": True, "valid_engine_run": teleports == 0 and collisions == 0,
            "simulated_seconds": horizon,
        }
        return {"policy": policy, "description": POLICIES[policy], "summary": summary,
                "series": series, "frames": frames}
    except (traci.TraCIException, traci.FatalTraCIError):
        engine_failed = True
        raise
    finally:
        if conn is not None:
            try:
                conn.close(wait=False)
            except (traci.TraCIException, traci.FatalTraCIError, OSError):
                # A crashed SUMO may already have closed the connection. Always
                # reap its process and preserve the original simulation error.
                pass
        stop_engine(process)
        if engine_failed:
            # SUMO explains engine failures in its log, which names server paths; the tail stays server-side.
            logger.warning("SUMO log tail for the %s policy: %s", policy, engine_log_tail(logfile))
        logfile.unlink(missing_ok=True)


class PolicyStopped(Exception):
    """Raised in a policy child process once the parent has asked every child to stop."""


# One policy child process's channels to its parent, set by start_policy_child: progress queue and stop event.
POLICY_CHILD = {}


def start_policy_child(updates, stop):
    """Give a policy child process the parent's progress queue and stop event (the process pool initializer)."""
    POLICY_CHILD.update(updates=updates, stop=stop)


def run_policy_in_child(scenario, policy, netfile, line_ids, fire_xy, demand, traffic, logfolder):
    """Simulate one policy in a child process that loads the network from its file; return the result and seconds.

    Progress goes to the parent's queue. Once the parent sets the stop event, the next cancellation check raises
    PolicyStopped, and run_policy still closes its SUMO process.
    """
    started = time.monotonic()
    updates, stop = POLICY_CHILD["updates"], POLICY_CHILD["stop"]

    def check_cancel():
        """Raise PolicyStopped once the parent has set the stop event."""
        if stop.is_set():
            raise PolicyStopped()

    check_cancel()
    net = sumolib.net.readNet(str(netfile), withInternal=False)
    lines = {edge_id: LineString(net.getEdge(edge_id).getShape()) for edge_id in line_ids}
    prepared = (netfile, net, None, lines, None, fire_xy, None, None)
    result = run_policy(scenario, policy, prepared, demand, lambda _, fraction: updates.put((policy, fraction)),
                        check_cancel, traffic, logfolder)
    return result, time.monotonic() - started


def take_progress(updates, fractions):
    """Apply every queued child progress report, (policy, fraction of its horizon), without waiting."""
    while True:
        try:
            policy, fraction = updates.get_nowait()
        except queue.Empty:
            return
        fractions[policy] = max(fractions[policy], fraction)


def run_policies_in_parallel(scenario, prepared, demand, progress, check_cancel, traffic, workers, timings):
    """Simulate each policy in its own spawned process, `workers` at a time, and return results in policy order.

    A child receives only plain inputs (scenario, demand, traffic, the network file path and edge IDs), loads the
    network from the file and runs its own SUMO. The parent reports combined progress and checks for
    cancellation about once a second. When a check raises or a child fails, it stops every child and then raises
    that same exception, so the worker maps it to the same public message as a sequential run.
    """
    policies = scenario.policies
    context = multiprocessing.get_context("spawn")
    updates, stop = context.Queue(), context.Event()
    fractions, results, shown = dict.fromkeys(policies, 0.0), {}, None
    logfolder = settings.data_dir("engine-logs")
    with ProcessPoolExecutor(workers, mp_context=context, initializer=start_policy_child,
                             initargs=(updates, stop)) as pool:
        futures = {pool.submit(run_policy_in_child, scenario, policy, prepared[0], list(prepared[3]), prepared[5],
                               demand, traffic, logfolder): policy for policy in policies}
        pending = set(futures)
        try:
            while pending:
                done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
                take_progress(updates, fractions)
                for future in sorted(done, key=lambda finished: policies.index(futures[finished])):
                    policy = futures[future]
                    results[policy], seconds = future.result()
                    timings.add(f"policy_{policy}", seconds)
                    fractions[policy] = 1.0
                check_cancel()
                minutes = round(sum(fractions.values()) / len(policies) * scenario.duration_minutes)
                if (minutes, len(results)) != shown:
                    shown = (minutes, len(results))
                    progress(f"Simulating {len(policies)} policies, {workers} at a time · "
                             f"{minutes}/{scenario.duration_minutes} min on average",
                             0.2 + 0.75 * sum(fractions.values()) / len(policies))
        except BaseException:
            stop.set()
            for future in pending:
                future.cancel()
            # Children stop at their next check, within seconds; one stuck in SUMO is terminated, and its SUMO
            # exits when the TraCI connection closes.
            deadline = time.monotonic() + STOP_GRACE_SECONDS
            while pending and time.monotonic() < deadline:
                _, pending = wait(pending, timeout=1)
                take_progress(updates, fractions)
            if pending:
                pool.terminate_workers()
            raise
    return [results[policy] for policy in policies]


def simulate(scenario, prepared, progress, check_cancel, traffic=None, demand=None, *, parallelism=None,
             timings=None):
    """Run every scenario policy on the prepared network and return the comparison with its assumptions.

    Policies run in separate processes, up to `parallelism` at once (POLICY_PARALLELISM by default); 1 runs them
    one after another in this process. The result is identical either way. A traffic-speed run needs a matched
    snapshot, or one marked unavailable because the daily TomTom budget was used up: that run keeps OSM speed
    limits and its result says the speeds were unavailable. timings, when given, receives one policy_<name>
    duration per policy; durations never enter the result.
    """
    timings = timings if timings is not None else Timings()
    unavailable = (traffic or {}).get("status") == "unavailable"
    if scenario.traffic_mode == "provider_speed_limits" and not (traffic or {}).get("matches") and not unavailable:
        raise RuntimeError("Traffic-speed mode requires a matched snapshot; synthetic fallback is not allowed.")
    if scenario.traffic_mode == "synthetic":
        traffic = None
    progress("Generating matched civilian demand and assumed crew staging routes", 0.15)
    if demand is None:
        demand = demand_for(scenario, prepared[1], prepared[2], prepared[3], prepared[4], prepared[5])
    workers = min(settings.policy_parallelism() if parallelism is None else parallelism, len(scenario.policies))
    if workers > 1:
        results = run_policies_in_parallel(scenario, prepared, demand, progress, check_cancel, traffic, workers,
                                           timings)
    else:
        results = []
        for index, policy in enumerate(scenario.policies):
            def policy_progress(message, fraction, index=index):
                progress(message, 0.2 + 0.75 * (index + fraction) / len(scenario.policies))
            with timings.measure(f"policy_{policy}"):
                results.append(run_policy(scenario, policy, prepared, demand, policy_progress, check_cancel,
                                          traffic))
    return {
        "schema_version": 3, "engine": "Eclipse SUMO", "engine_version": sumolib.__version__,
        "traffic": traffic or {"mode": "synthetic", "status": "not_requested", "matched_edges": 0},
        "scenario": scenario.model_dump(), "network": prepared[7], "policies": results,
        "assumptions": [
            "All evacuation demand is synthetic, sampled from road length; not census- or dispatch-derived.",
            ("A frozen TomTom snapshot sets experimental speed limits on matched highway edges; "
             "demand and initial queues are not calibrated."
             if (traffic or {}).get("matches")
             else "Traffic feed observations are context only. Speed factor is a manual assumption."),
            "The hazard is an analyst-defined expanding circle; it is not a wildfire forecast.",
            "Hard exclusions block new entry; already-entered vehicles can attempt to leave and count as exposed.",
            "Pending departures at closed origins are canceled in SUMO and retained as nondeparted demand.",
            "Already-departed vehicles are never removed to clear queues; unresolved demand stays in the ledger.",
            "Crew origins and service tasks are synthetic. Staging arrival is not completion of firefighting.",
            "Crew vehicles follow normal access and signal rules; no priority signal preemption is modeled.",
            "Policies are heuristic experiments, not a proof of globally optimal traffic control.",
            "A single random seed is a demo comparison, not a statistically validated operational result.",
        ],
        "demand": [{"id": d["id"], "origin": d.get("origin"), "depart": d["depart"],
                    "routable": bool(d["route"]), "people": d["people"]} for d in demand[0] + demand[1]],
        "crew_plan": [{
            "id": c["id"], "origin": c.get("origin"), "stage": c.get("stage"),
            "route": c["route"], "stage_index": c.get("stage_index"),
            "scheduled_departure_s": c["depart"], "service_seconds": 120,
        } for c in demand[1]],
    }
