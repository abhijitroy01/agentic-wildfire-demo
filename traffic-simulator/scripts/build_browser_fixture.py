"""Build the synthetic browser-harness fixture: one seeded SUMO run on the tests' 5x5 grid through the worker path.

The worker runs in-process the way test_worker.py's integration tests run it: a local job store in a temporary data
folder, the road network from traffic_simulator.testing.grid (built with the installed netconvert) and synthetic
public feeds marked not_requested, so nothing calls a provider or the network. The run's result and public road
network are written to ui/test-fixtures/browser-synthetic-grid/ with a fixture.json label. That folder is outside
every release bundle folder (deployment/tests/test_browser_fixture.py), so the fixture never ships.

Run from the repository root: .venv/bin/python traffic-simulator/scripts/build_browser_fixture.py
"""
import gzip
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "ui" / "test-fixtures" / "browser-synthetic-grid"
SEED = 42
LABEL = "Synthetic 5x5 grid near Paradise, CA, seeded; not real roads, traffic or incidents"
FIXTURE_LABEL = {"synthetic": True, "label": LABEL,
                 "generator": "traffic-simulator/scripts/build_browser_fixture.py", "seed": SEED}
# The harness drives every stage from one run; a fixture file above this size is too large to keep in git.
MAX_FILE_BYTES = 1024 * 1024
POLICIES = ["baseline", "coordinated", "phased"]
# Every scenario field not set here keeps the model's default (Paradise, CA, which the grid surrounds).
SCENARIO = {"radius_km": 2, "vehicles": 100, "crews": 4, "seed": SEED, "traffic_mode": "synthetic",
            "policies": POLICIES}
# The selected highway the grid stands in for; the grid's gate edge is its model exit.
ROUTE = {"route_type": "State", "route_number": 191, "approx_distance_km": 0.0, "districts": [3],
         "counties": ["BUT"], "segment_ids": []}


def harness_problems(result):
    """List what the browser harness needs from a result and this one lacks; an empty list means it can be used.

    The harness needs schema 4 with an input bundle ID and SHA-256, all three policies, a crew plan, and a pumping
    crew (roster positions 0-2 of every 5) that makes its staging stop in the coordinated replay.
    """
    problems = []
    if result.get("schema_version") != 4:
        problems.append("schema_version is not 4")
    if not result.get("input_bundle_id") or not result.get("input_bundle_sha256"):
        problems.append("no input bundle ID and SHA-256")
    policies = {policy["policy"]: policy for policy in result.get("policies", [])}
    if sorted(policies) != sorted(POLICIES):
        problems.append(f"policies are {sorted(policies)}, not {POLICIES}")
    plan = result.get("crew_plan") or []
    if not plan:
        problems.append("no crew_plan")
    pumping = {crew["id"] for position, crew in enumerate(plan) if position % 5 < 3}
    frames = policies.get("coordinated", {}).get("frames", [])
    if not any(crew["id"] in pumping and crew["state"] == "staging_stop" for frame in frames for crew in frame["crews"]):
        problems.append("no pumping crew makes its staging stop in the coordinated replay")
    return problems


def grid_network(folder, scenario):
    """Build the tests' 5x5 grid and return it as the prepared-network tuple, with public GeoJSON and metadata."""
    from traffic_simulator import testing

    nodes_and_edges = folder / "grid"
    nodes_and_edges.mkdir()
    # The fixture's function itself, unwrapped from its pytest registration.
    netfile, net, edges, lines, gates, center, _, _ = testing.grid.__wrapped__(nodes_and_edges)
    gate_ids = {gate.getID() for gate in gates}
    features = [{"type": "Feature",
                 "geometry": {"type": "LineString",
                              "coordinates": [list(net.convertXY2LonLat(x, y)) for x, y in edge.getShape()]},
                 "properties": {"id": edge.getID(), "highway": edge.getID() in gate_ids,
                                "lanes": edge.getLaneNumber(), "speed_m_s": edge.getSpeed()}} for edge in edges]
    inputs = b"".join((nodes_and_edges / name).read_bytes() for name in ("nodes.xml", "edges.xml"))
    metadata = {"source": "Synthetic 5x5 grid (traffic_simulator.testing.grid); not OpenStreetMap",
                "retrieved_at": time.time(), "osm_sha256": hashlib.sha256(inputs).hexdigest(),
                "net_sha256": hashlib.sha256(netfile.read_bytes()).hexdigest(),
                "highway": scenario.highway, "edge_count": len(edges), "highway_edge_count": len(gates),
                "selected_route": ROUTE,
                "gates": [{"edge": gate.getID(), "coordinate": list(net.convertXY2LonLat(*gate.getShape()[-1])),
                           "label": "Assumed model exit; safety unverified"} for gate in gates],
                "limitations": [f"{LABEL}.", "Exit gates model network departure, not arrival at a verified safe refuge."]}
    return netfile, net, edges, lines, gates, center, {"type": "FeatureCollection", "features": features}, metadata


def run_worker(data):
    """Run one seeded job through worker.execute on the grid with a local store under data; return its artifacts."""
    from traffic_simulator import worker
    from traffic_simulator.models import Scenario
    from traffic_simulator.store import Store
    from traffic_simulator.testing import create_job

    scenario = Scenario(**SCENARIO)
    prepared = grid_network(data, scenario)

    def prepare(scenario, progress, check, *, timings, route_selected=None):
        """Return the grid instead of discovering a highway and downloading roads."""
        return prepared

    async def sources(area, districts, readings=None):
        """Return synthetic readings instead of fetching the public feeds."""
        return {"retrieved_at": time.time(), "point": {"lat": area.lat, "lon": area.lon}, "radius_km": area.radius_km,
                "sources": [{"name": "Public feeds", "status": "not_requested", "records": [],
                             "use": f"{LABEL}; no public feed was requested"}]}

    worker.prepare_network, worker.fetch_sources = prepare, sources
    store = Store(data / "store")
    job = create_job(store, "browser-fixture", scenario.model_dump())
    worker.execute(store, job, threading.Event())
    record = store.get(job["id"])
    if record["status"] != "completed":
        raise RuntimeError(f"The fixture run ended {record['status']}: {record.get('error')}")
    return store.artifact(job["id"], "result"), store.artifact(job["id"], "network")


def write(folder, result, network):
    """Write the gzip artifacts (as the job store encodes them, without a gzip timestamp) and the label file."""
    folder.mkdir(parents=True, exist_ok=True)
    for name, value in (("result", result), ("network", network)):
        encoded = json.dumps(value, separators=(",", ":"), allow_nan=False).encode()
        (folder / f"{name}.json.gz").write_bytes(gzip.compress(encoded, mtime=0))
    (folder / "fixture.json").write_text(json.dumps(FIXTURE_LABEL, indent=2) + "\n")
    oversized = [path.name for path in folder.iterdir() if path.stat().st_size >= MAX_FILE_BYTES]
    if oversized:
        raise RuntimeError(f"Fixture files must stay under 1 MiB: {', '.join(oversized)}")


def main():
    """Build the fixture and fail before writing it unless the browser harness can use it."""
    with tempfile.TemporaryDirectory(prefix="browser-fixture-") as data:
        # Set before traffic_simulator is imported: a local store and scratch folders in this temporary folder only.
        os.environ["STORAGE_MODE"] = "local"
        os.environ["DATA_DIR"] = data
        sys.path.insert(0, str(ROOT / "traffic-simulator"))
        result, network = run_worker(Path(data))
    problems = harness_problems(result)
    if problems:
        raise RuntimeError("The browser harness cannot use this run: " + "; ".join(problems))
    write(FIXTURE, result, network)
    print(f"Wrote {FIXTURE.relative_to(ROOT)}: " + ", ".join(
        f"{path.name} {path.stat().st_size:,} bytes" for path in sorted(FIXTURE.iterdir())))


if __name__ == "__main__":
    main()
