import json
import multiprocessing
import queue
import socket
import threading
from itertools import pairwise
from unittest.mock import Mock

import pytest
import traci
from shapely.geometry import Point
from traffic_simulator import errors, simulation
from traffic_simulator.models import Scenario
from traffic_simulator.simulation import demand_for, hold_closed_origins, radius_at, run_policy, simulate
from traffic_simulator.timings import Timings


def artifact_bytes(result):
    """Encode a result as the job store writes the result artifact (Store.artifact), for byte comparison."""
    return json.dumps(result, separators=(",", ":"), allow_nan=False).encode()


def grid_route(*nodes):
    """Name the grid fixture's edges along a path of its node IDs; an edge's ID is "<from node>-<to node>"."""
    return [f"{start}-{end}" for start, end in pairwise(nodes)]


def grid_record(grid, vehicle_id, route, depart, **fields):
    """Build one civilian or crew demand record that starts on the first edge of a grid route."""
    return {"id": vehicle_id, "origin": route[0], "route": route, "depart": depart, "people": 2,
            "origin_xy": list(grid[1].getEdge(route[0]).getShape()[0]), **fields}


# The grid's fire point is its center node, n2_2. A 50 m hazard closes exactly the eight edges that meet there
# (7.4 m away); every other edge is at least 512 m away. Vehicles bound through n2_2 then stop where they stand.
THROUGH_THE_FIRE = grid_route("n4_2", "n3_2", "n2_2", "n1_2", "n0_2")
CLOSURE_AT_ONE_MINUTE = {"radius_km": 3, "departure_minutes": 1, "hazard_radius_m": 50, "hazard_growth_m_min": 0,
                         "hazard_start_minutes": 1}


@pytest.mark.integration
def test_parallel_policies_give_byte_identical_results_to_sequential_runs(grid):
    """Simulation results must not change: policies in parallel processes equal the in-process run, byte for byte."""
    scenario = Scenario(vehicles=60, crews=2, radius_km=3, duration_minutes=6, departure_minutes=2,
                        hazard_radius_m=100, hazard_growth_m_min=40, hazard_start_minutes=1)
    demand = demand_for(scenario, grid[1], grid[2], grid[3], grid[4], grid[5])
    reports, timings = [], Timings()
    sequential = simulate(scenario, grid, lambda *_: None, lambda: None, demand=demand, parallelism=1)
    parallel = simulate(scenario, grid, lambda message, fraction: reports.append((message, fraction)), lambda: None,
                        demand=demand, parallelism=3, timings=timings)
    assert artifact_bytes(parallel) == artifact_bytes(sequential)
    assert [p["policy"] for p in parallel["policies"]] == ["baseline", "coordinated", "phased"]
    assert sequential["policies"][0]["summary"]["exited_vehicles"] > 0
    # Without explicit demand, both modes also derive the same demand from the seed.
    assert artifact_bytes(simulate(scenario, grid, lambda *_: None, lambda: None, parallelism=3)) == artifact_bytes(
        sequential)
    assert {"policy_baseline", "policy_coordinated", "policy_phased"} == timings.public().keys()
    # One combined progress line: monotonic, inside the simulation band, ending when every policy has finished.
    combined = reports[1:]
    assert combined and all(message.startswith("Simulating 3 policies, 3 at a time") for message, _ in combined)
    fractions = [fraction for _, fraction in combined]
    assert fractions == sorted(fractions) and 0.2 <= fractions[0] and fractions[-1] == pytest.approx(0.95)
    assert multiprocessing.active_children() == []


@pytest.mark.integration
def test_one_cancel_stops_every_parallel_policy(grid):
    """A cancellation the parent sees stops every child process before the exception reaches the worker."""
    class Cancelled(Exception):
        pass

    def cancel():
        raise Cancelled()

    scenario = Scenario(vehicles=20, crews=0, radius_km=3, duration_minutes=60, departure_minutes=1,
                        hazard_radius_m=0, hazard_growth_m_min=0)
    with pytest.raises(Cancelled):
        simulate(scenario, grid, lambda *_: None, cancel, parallelism=3)
    assert multiprocessing.active_children() == []


@pytest.mark.integration
def test_a_failed_parallel_policy_raises_what_a_sequential_run_raises(grid):
    """A child's failure stops the others and reaches the worker unchanged, so it maps to the same public text."""
    scenario = Scenario(vehicles=20, crews=0, radius_km=3, duration_minutes=5, departure_minutes=1,
                        hazard_radius_m=0, hazard_growth_m_min=0, traffic_mode="provider_speed_limits")
    broken = {"matches": [{"edge_id": "not-a-road-edge", "speed_limit_m_s": 4}]}
    failures = []
    for parallelism in (1, 3):
        with pytest.raises(KeyError) as failure:
            simulate(scenario, grid, lambda *_: None, lambda: None, traffic=broken, parallelism=parallelism)
        failures.append(failure.value)
    assert [type(f) for f in failures] == [KeyError, KeyError] and str(failures[0]) == str(failures[1])
    assert errors.public_message(failures[0]) == errors.public_message(failures[1]) == errors.UNEXPECTED_FAILURE
    assert multiprocessing.active_children() == []


def test_a_stopped_policy_child_ends_before_loading_the_network(tmp_path, monkeypatch):
    """Once the parent sets the stop event, a child raises PolicyStopped before it reads the network file."""
    monkeypatch.setattr(simulation, "POLICY_CHILD", {})
    stop = threading.Event()
    stop.set()
    simulation.start_policy_child(queue.Queue(), stop)
    with pytest.raises(simulation.PolicyStopped):
        simulation.run_policy_in_child(Scenario(), "baseline", tmp_path / "absent.net.xml", [], (0.0, 0.0),
                                       ([], []), None, tmp_path)


@pytest.mark.integration
def test_each_policy_deletes_its_engine_log_and_keeps_a_failure_tail_in_the_server_log(grid, tmp_path, caplog):
    """SUMO logs never accumulate on the worker's disk; an engine failure's log tail reaches the server log first."""
    scenario = Scenario(vehicles=20, crews=0, radius_km=3, duration_minutes=5, departure_minutes=1,
                        hazard_radius_m=0, hazard_growth_m_min=0)
    demand = demand_for(scenario, grid[1], grid[2], grid[3], grid[4], grid[5])
    logs = tmp_path / "engine-logs"
    logs.mkdir()
    run_policy(scenario, "baseline", grid, demand, lambda *_: None, lambda: None, logfolder=logs)
    assert list(logs.iterdir()) == []
    unknown_road = ([{**demand[0][0], "route": ["not-a-road-edge"]}], [])
    with pytest.raises(traci.TraCIException):
        run_policy(scenario, "baseline", grid, unknown_road, lambda *_: None, lambda: None, logfolder=logs)
    assert list(logs.iterdir()) == []
    assert any(r.name == "traffic_simulator.simulation" and "SUMO log tail for the baseline policy" in r.getMessage()
               for r in caplog.records)


@pytest.mark.integration
@pytest.mark.parametrize("collisions", [1, 3])
def test_only_startup_port_collisions_retry_before_any_simulation(grid, tmp_path, monkeypatch, collisions):
    """Force a real SUMO bind failure; recover once or fail after three attempts."""
    scenario = Scenario(vehicles=20, crews=0, radius_km=3, duration_minutes=5, departure_minutes=1,
                        hazard_radius_m=0, hazard_growth_m_min=0)
    demand = demand_for(scenario, grid[1], grid[2], grid[3], grid[4], grid[5])
    launch = simulation.subprocess.Popen
    processes = []
    with socket.socket() as occupied:
        # The socket is bound but never listens, so nothing can connect to it: the kernel accepts no
        # connection to this port, from any host. The wildcard address is required: only a wildcard bind
        # collides with SUMO's own wildcard TraCI bind on every platform (on macOS, SUMO still binds a port
        # that a loopback-only socket holds).
        occupied.bind(("", 0))
        busy_port = occupied.getsockname()[1]
        def force_collision(args, **kwargs):
            args = list(args)
            if len(processes) < collisions:
                args[args.index("--remote-port")+1] = str(busy_port)
            child = launch(args, **kwargs)
            processes.append(child)
            return child
        monkeypatch.setattr(simulation.subprocess, "Popen", force_collision)
        logs = tmp_path / "logs"
        logs.mkdir()
        if collisions == 3:
            with pytest.raises(traci.TraCIException):
                run_policy(scenario, "baseline", grid, demand, lambda *_: None, lambda: None, logfolder=logs)
            assert len(processes) == 3
        else:
            result = run_policy(scenario, "baseline", grid, demand, lambda *_: None, lambda: None, logfolder=logs)
            assert len(processes) == 2 and result["summary"]["valid_accounting"]
            assert result["summary"]["valid_engine_run"]
        assert all(child.poll() is not None for child in processes)
        assert not list(logs.iterdir())


@pytest.mark.integration
def test_only_phased_departures_send_people_near_the_fire_first(grid):
    """TSIM-2: phased moves near-fire departures earlier and far ones later; the other policies keep the plan."""
    scenario = Scenario(vehicles=20, crews=0, radius_km=3, duration_minutes=5, departure_minutes=5,
                        hazard_radius_m=0, hazard_growth_m_min=0)
    origin = grid[1].getEdge("n3_2-n2_2")
    path, _ = grid[1].getFastestPath(origin, grid[4][0], vClass="passenger")
    # Everyone is planned at 200 s. Phased starts half at the fire point at 200 s x 0.45 = 90 s, and half at the
    # demand circle's edge at 90 s + 0.55 of the 300 s departure window = 255 s.
    civilians = [grid_record(grid, f"evac-{i}", [e.getID() for e in path], 200,
                             distance_m=0 if i < 10 else scenario.radius_km * 850) for i in range(20)]
    waiting = {}
    for policy in ("baseline", "coordinated", "phased"):
        series = run_policy(scenario, policy, grid, (civilians, []), lambda *_: None, lambda: None)["series"]
        waiting[policy] = next(point["not_departed"] for point in series if point["t"] == 150)
    assert waiting == {"baseline": 20, "coordinated": 20, "phased": 10}


@pytest.mark.integration
def test_coordinated_and_phased_crews_wait_out_congestion_that_baseline_crews_drive_into(grid):
    """TSIM-2: a crew whose first roads hold more than 8 halted vehicles waits up to 120 s, except under baseline."""
    scenario = Scenario(vehicles=20, crews=13, duration_minutes=5, **CLOSURE_AT_ONE_MINUTE)
    # Twelve crews bound through the fire point stand at the end of n4_2-n3_2 once it closes; a thirteenth crew,
    # due at 150 s, would turn off that road before the closure.
    stopped = [grid_record(grid, f"crew-{i}", THROUGH_THE_FIRE, 15 + 2 * i, stage=THROUGH_THE_FIRE[2],
                           stage_index=2, people=0) for i in range(12)]
    detour = grid_route("n4_3", "n4_2", "n3_2", "n3_1", "n3_0")
    due = grid_record(grid, "crew-12", detour, 150, stage=detour[3], stage_index=3, people=0)
    civilians = [{"id": f"evac-{i}", "route": None, "depart": 0, "people": 2} for i in range(20)]
    departed = {}
    for policy in ("baseline", "coordinated", "phased"):
        result = run_policy(scenario, policy, grid, (civilians, [*stopped, due]), lambda *_: None, lambda: None)
        departed[policy] = next(crew["departed_s"] for crew in result["frames"][-1]["crews"]
                                if crew["id"] == "crew-12")
    assert departed == {"baseline": 151, "coordinated": 271, "phased": 271}


@pytest.mark.integration
def test_coordinated_and_phased_civilians_reroute_around_a_closure_that_holds_baseline_civilians(grid):
    """TSIM-2: past a closure only the rerouting policies get civilians out; baseline keeps the planned route."""
    scenario = Scenario(vehicles=20, crews=0, duration_minutes=10, **CLOSURE_AT_ONE_MINUTE)
    civilians = [grid_record(grid, f"evac-{i}", THROUGH_THE_FIRE, 10 + 2 * i, distance_m=0) for i in range(20)]
    exited = {policy: run_policy(scenario, policy, grid, (civilians, []), lambda *_: None, lambda: None)
              ["summary"]["exited_vehicles"] for policy in ("baseline", "coordinated", "phased")}
    assert exited["baseline"] == 0, exited
    assert exited["coordinated"] >= 10 and exited["phased"] >= 10, exited


def test_hazard_onset_is_explicit():
    scenario = Scenario(hazard_start_minutes=5, hazard_radius_m=150, hazard_growth_m_min=10)
    assert radius_at(scenario, 299) == 0
    assert radius_at(scenario, 300) == 150
    assert radius_at(scenario, 360) == 160


@pytest.mark.integration
def test_real_sumo_uses_traffic_speed_limits_without_changing_demand(grid):
    scenario = Scenario(vehicles=20, crews=0, radius_km=3, duration_minutes=5,
                        departure_minutes=1, hazard_radius_m=0, hazard_growth_m_min=0,
                        traffic_mode="provider_speed_limits")
    origin = grid[1].getEdge("n3_2-n2_2")
    route, _ = grid[1].getFastestPath(origin, grid[4][0], vClass="passenger")
    civilians = [{"id": f"evac-{i}", "origin": origin.getID(), "route": [e.getID() for e in route],
                  "depart": i * 2, "distance_m": 0, "people": 2,
                  "origin_xy": list(origin.getShape()[0])} for i in range(20)]
    # Explicitly synthetic 4 m/s fixture; the engine test makes no live-data claim.
    fixture = {"matches": [{"edge_id": e.getID(), "speed_limit_m_s": 4} for e in route]}
    limited = run_policy(scenario, "baseline", grid, (civilians, []), lambda *_: None, lambda: None, fixture)
    control = run_policy(scenario, "baseline", grid, (civilians, []), lambda *_: None, lambda: None)
    def interior_speeds(result):
        speeds = []
        for frame in result["frames"]:
            for vehicle in frame["vehicles"]:
                p = Point(grid[1].convertLonLat2XY(vehicle[1], vehicle[2]))
                # SUMO internal junction connectors are not the matched road lanes.
                if any(line.distance(p) < 5 and 30 < line.project(p) < line.length - 30
                       for line in (grid[3][edge.getID()] for edge in route)):
                    speeds.append(vehicle[3])
        return speeds
    # A lane speed limit influences desired speed; SUMO's existing driver
    # speed-factor variation remains enabled, so this is not a hard clamp.
    assert 3.5 <= max(interior_speeds(limited)) <= 5
    assert max(interior_speeds(control)) > 8
    for result in (limited, control):
        assert all(s["exited"] + s["active"] + s["not_departed"] == 20 for s in result["series"])
        assert result["summary"]["valid_engine_run"]


@pytest.mark.integration
def test_traffic_unavailable_after_the_daily_budget_keeps_osm_limits_and_says_so(grid):
    """A run whose TomTom budget was used up continues; its policies equal the assumed-traffic run's results."""
    values = {"vehicles": 20, "crews": 0, "radius_km": 3, "duration_minutes": 5, "departure_minutes": 1,
              "hazard_radius_m": 0, "hazard_growth_m_min": 0, "policies": ["baseline"]}
    unavailable = {"mode": "provider_speed_limits", "provider": "TomTom", "status": "unavailable",
                   "reason": "daily_budget_reached", "matches": [], "matched_edges": 0}
    limited = simulate(Scenario(**values, traffic_mode="provider_speed_limits"), grid, lambda *_: None,
                       lambda: None, traffic=unavailable)
    assumed = simulate(Scenario(**values), grid, lambda *_: None, lambda: None)
    assert limited["policies"] == assumed["policies"]
    assert limited["traffic"] == unavailable and assumed["traffic"]["status"] == "not_requested"
    assert limited["assumptions"] == assumed["assumptions"], "No TomTom speed limit is claimed."
    with pytest.raises(RuntimeError, match="requires a matched snapshot"):
        simulate(Scenario(**values, traffic_mode="provider_speed_limits"), grid, lambda *_: None, lambda: None,
                 traffic={**unavailable, "status": "applied"})


def test_closure_cancels_only_pending_insertions():
    conn = Mock()
    records = {vid: {"origin": origin, "route": [origin]} for vid, origin in [
        ("active", "closed"), ("future", "closed"), ("waiting", "closed"),
        ("crew-not-submitted", "closed"), ("open-origin", "open"),
    ]}
    records["unroutable"] = {"route": None}
    submitted = {"active", "future", "waiting", "open-origin"}
    held = hold_closed_origins(conn, {"closed"}, records, submitted, {"active"})
    assert held == {"future", "waiting", "crew-not-submitted"}
    assert {c.args[0] for c in conn.vehicle.remove.call_args_list} == {"future", "waiting"}
    assert submitted == {"active", "open-origin"}
    assert len(records) == 6  # Cancellation does not erase original demand.


@pytest.mark.integration
def test_real_sumo_conserves_demand_and_is_reproducible(grid):
    scenario = Scenario(vehicles=20, crews=0, radius_km=3, duration_minutes=5,
                        departure_minutes=1, hazard_radius_m=0, hazard_growth_m_min=0)
    demand = demand_for(scenario, grid[1], grid[2], grid[3], grid[4], grid[5])
    again = demand_for(scenario, grid[1], grid[2], grid[3], grid[4], grid[5])
    assert demand == again
    result = run_policy(scenario, "baseline", grid, demand, lambda *_: None, lambda: None)
    summary = result["summary"]
    assert summary["exited_vehicles"] + summary["active_vehicles"] + summary["not_departed_vehicles"] == 20
    assert summary["exited_vehicles"] > 0
    assert summary["exposed_vehicles"] == 0
    assert summary["teleports"] == 0
    assert any(f["vehicles"] for f in result["frames"])


@pytest.mark.integration
@pytest.mark.parametrize("policy", ["baseline", "coordinated", "phased"])
def test_hard_exclusion_retains_blocked_non_departed_people(grid, policy):
    scenario = Scenario(vehicles=20, crews=0, radius_km=3, duration_minutes=5,
                        departure_minutes=1, hazard_radius_m=2000, hazard_growth_m_min=0,
                        hazard_start_minutes=0)
    demand = demand_for(scenario, grid[1], grid[2], grid[3], grid[4], grid[5])
    result = run_policy(scenario, policy, grid, demand, lambda *_: None, lambda: None)
    summary = result["summary"]
    assert summary["exited_vehicles"] == 0
    assert summary["unresolved_vehicles"] == 20
    assert summary["not_departed_vehicles"] == 20
    assert summary["active_vehicles"] == 0
    assert summary["origin_blocked_vehicles"] == 20
    assert summary["insertion_errors"] == 0
    assert summary["exposed_vehicles"] == 20
    assert summary["exposed_people"] == 40
    assert summary["teleports"] == 0
    assert summary["closed_edges"] == len(grid[2])


@pytest.mark.integration
def test_closure_cancels_future_civilian_and_crew_departures(grid):
    scenario = Scenario(vehicles=20, crews=1, radius_km=3, duration_minutes=5,
                        departure_minutes=3, hazard_radius_m=2000, hazard_growth_m_min=0,
                        hazard_start_minutes=1)
    origin = grid[1].getEdge("n3_2-n2_2")
    path, _ = grid[1].getFastestPath(origin, grid[4][0], vClass="passenger")
    route = [e.getID() for e in path]
    civilians = [{"id": f"evac-{i}", "origin": origin.getID(), "route": route, "depart": 120,
                  "distance_m": 0, "people": 2, "origin_xy": list(origin.getShape()[0])}
                 for i in range(20)]
    crews = [{"id": "crew-0", "origin": origin.getID(), "route": route, "depart": 120,
              "stage": route[1], "people": 0, "origin_xy": list(origin.getShape()[0])}]
    result = run_policy(scenario, "baseline", grid, (civilians, crews), lambda *_: None, lambda: None)
    summary = result["summary"]
    assert summary["origin_blocked_vehicles"] == summary["not_departed_vehicles"] == 20
    assert summary["crews_origin_blocked"] == summary["crews_not_departed"] == 1
    assert summary["exited_vehicles"] == summary["active_vehicles"] == 0
    assert summary["crews_staged"] == summary["crews_returned"] == 0
    assert summary["exposed_vehicles"] == 20
    assert summary["exposed_crews"] == 1
    assert summary["insertion_errors"] == summary["teleports"] == 0
    assert all(s["exited"] + s["active"] + s["not_departed"] == 20 for s in result["series"])


@pytest.mark.integration
def test_closure_preserves_departed_traffic_and_cancels_insertion_queue(grid):
    scenario = Scenario(vehicles=100, crews=1, radius_km=3, duration_minutes=5,
                        departure_minutes=1, hazard_radius_m=2000, hazard_growth_m_min=0,
                        hazard_start_minutes=1)
    origin = grid[1].getEdge("n3_2-n2_2")
    path, _ = grid[1].getFastestPath(origin, grid[4][0], vClass="passenger")
    route = [e.getID() for e in path]
    civilians = [{"id": f"evac-{i}", "origin": origin.getID(), "route": route, "depart": 0,
                  "distance_m": 0, "people": 2, "origin_xy": list(origin.getShape()[0])}
                 for i in range(100)]
    # Queued behind the civilians when the closure arrives.
    crews = [{"id": "crew-0", "origin": origin.getID(), "route": route, "depart": 50,
              "stage": route[1], "people": 0, "origin_xy": list(origin.getShape()[0])}]
    result = run_policy(scenario, "baseline", grid, (civilians, crews), lambda *_: None, lambda: None)
    before = next(s for s in result["series"] if s["t"] == 60)
    after = next(s for s in result["series"] if s["t"] == 65)
    assert before["active"] > 0
    assert before["not_departed"] > 0
    assert after["exited"] + after["active"] == before["exited"] + before["active"]
    summary = result["summary"]
    assert summary["origin_blocked_vehicles"] == before["not_departed"]
    assert summary["not_departed_vehicles"] == before["not_departed"]
    assert summary["crews_origin_blocked"] == summary["crews_not_departed"] == 1
    assert summary["insertion_errors"] == summary["teleports"] == 0
    assert all(s["exited"] + s["active"] + s["not_departed"] == 100 for s in result["series"])


@pytest.mark.integration
def test_crew_timeline_records_dispatch_staging_and_return(grid):
    scenario = Scenario(vehicles=20, crews=1, duration_minutes=5, departure_minutes=5,
                        hazard_radius_m=0, hazard_growth_m_min=0)
    origin = grid[1].getEdge("n3_2-n2_2")
    path, _ = grid[1].getFastestPath(origin, grid[4][0], vClass="passenger")
    route = [e.getID() for e in path]
    civilians = [{"id": f"evac-{i}", "origin": origin.getID(), "route": route, "depart": 270,
                  "distance_m": 0, "people": 2, "origin_xy": list(origin.getShape()[0])}
                 for i in range(20)]
    crews = [{"id": "crew-0", "origin": origin.getID(), "route": route, "depart": 0,
              "stage": route[1], "stage_index": 1, "people": 0,
              "origin_xy": list(origin.getShape()[0])}]
    result = run_policy(scenario, "baseline", grid, (civilians, crews), lambda *_: None, lambda: None)
    states = [frame["crews"][0] for frame in result["frames"]]
    assert states[0]["state"] == "waiting_to_depart"
    assert states[0]["departed_s"] is None
    assert {"to_staging", "staging_stop", "returning", "returned"} <= {s["state"] for s in states}
    final = states[-1]
    assert final["state"] == "returned"
    assert 0 < final["departed_s"] < final["reached_staging_s"] < final["returned_s"] <= 300
    assert final["returned_s"] - final["reached_staging_s"] >= 120
    assert result["summary"]["crews_staged"] == result["summary"]["crews_returned"] == 1
