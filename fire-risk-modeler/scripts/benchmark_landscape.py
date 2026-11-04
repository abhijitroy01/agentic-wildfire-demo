"""Synthetic numerical capacity measurements; no provider requests, substitutes or wall-clock deadline.

Run in the verified Linux runtime with explicit Docker CPU/memory limits and this scripts directory mounted:
    python /benchmark/benchmark_landscape.py --size 102 --output /tmp/representative.json
    python /benchmark/benchmark_landscape.py --max-grid --enforce-resource-limits --output /tmp/maximum.json

Each case runs in a separate process. Observation-only proxies count actual native calls and retain the solver's
arrival array, including partial work on DOMAIN_TRUNCATED. Native rates, grid physics and calculation code are
unchanged. Synthetic inputs follow the calculator's manifest/weather schema; its source fields do not establish
an acquisition, and none of the named provider endpoints are contacted.
"""
import argparse
import hashlib
import json
import math
import resource
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

CASES = ("all-burnable-wind", "varied-terrain-fuel")
RETAINED_GUARD_BYTES = 350 * 1024


def emit(value):
    print(json.dumps(value, allow_nan=False, separators=(",", ":")), flush=True)


def read(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def number(value):
    return int(value) if value and value.lstrip("-").isdecimal() else None


def counters(value):
    return {line.split()[0]: int(line.split()[1]) for line in (value or "").splitlines()
            if len(line.split()) == 2 and line.split()[1].isdecimal()}


def resources():
    """Read effective Linux cgroup limits and observed usage, never environment variables or credentials."""
    root = Path("/sys/fs/cgroup")
    cpu_max = read(root / "cpu.max")
    if cpu_max:
        quota, period = cpu_max.split()
        cpu = int(quota) / int(period) if quota != "max" else None
        memory_max = number(read(root / "memory.max"))
        memory_current = number(read(root / "memory.current"))
        memory_peak = number(read(root / "memory.peak"))
        version = 2
    else:
        quota = number(read(root / "cpu/cpu.cfs_quota_us"))
        period = number(read(root / "cpu/cpu.cfs_period_us"))
        cpu = quota / period if quota is not None and quota > 0 and period else None
        memory_max = number(read(root / "memory/memory.limit_in_bytes"))
        memory_current = number(read(root / "memory/memory.usage_in_bytes"))
        memory_peak = number(read(root / "memory/memory.max_usage_in_bytes"))
        version = 1 if quota is not None else None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss = next((int(line.split()[1]) * 1024 for line in (read("/proc/self/status") or "").splitlines()
                if line.startswith("VmRSS:")), None)
    return {"cgroup_version": version, "cpu_limit_vcpu": cpu, "memory_limit_bytes": memory_max,
            "memory_current_bytes": memory_current, "memory_peak_bytes": memory_peak,
            "cpu_stat": counters(read(root / "cpu.stat")), "memory_events": counters(read(root / "memory.events")),
            "process_rss_bytes": rss, "process_peak_rss_bytes": int(peak if sys.platform == "darwin" else peak*1024)}


def settings():
    paths = (Path.cwd() / "deployment/settings.toml",
             Path(__file__).resolve().parent.parent.parent / "deployment/settings.toml")
    path = next((path for path in paths if path.is_file()), None)
    if path is None:
        raise RuntimeError("Captured deployment/settings.toml is required for resource and protocol limits")
    return tomllib.loads(path.read_text())


def specification(config):
    capacity, simulation = config["capacity"], config["simulation"]
    return {"cpu_vcpu": capacity["fire_risk_vcpu"]["value"],
            "memory_bytes": capacity["fire_risk_memory_gib"]["value"]*1024**3,
            "max_cells": simulation["fire_risk_landscape_max_cells"]["value"],
            "directional_radius": simulation["fire_risk_landscape_directional_radius"]["value"],
            "request_max_bytes": capacity["fire_risk_landscape_request_max_kib"]["value"]*1024,
            "response_max_bytes": capacity["fire_risk_response_max_kib"]["value"]*1024,
            "calculations_per_process": capacity["fire_risk_calculations_per_process"]["value"]}


def retained_envelope_bytes(result):
    """Measure the actual result's AttributeValue envelope, without importing API/storage clients.

    Matches the published revision-two record, with a full 69-character demo owner and conservative timestamp
    widths. The API's existing independent 350 KiB guard remains authoritative. Nested coordinate/number/list
    wrappers are measured, rather than estimating retained size from the plain response bytes.
    """
    def attribute(value):
        if value is None:
            return {"NULL": True}
        if type(value) is bool:
            return {"BOOL": value}
        if isinstance(value, str):
            return {"S": value}
        if type(value) in (float, int):
            return {"N": str(Decimal(str(value)))}
        if isinstance(value, list):
            return {"L": [attribute(item) for item in value]}
        if isinstance(value, dict):
            return {"M": {key: attribute(item) for key, item in value.items()}}
        raise TypeError("The retained benchmark envelope must contain JSON values")

    document = {"id": "fire_exposure:"+result.assessment_id, "kind": "fire_exposure",
                "subject": "demo-"+"f"*64, "revision": 2, "created_at": 9999999999.999999,
                "expires": 9999999999, "input_hash": result.input_hash, "mode": "landscape",
                "result": result.model_dump(mode="json")}
    return len(json.dumps({key: attribute(value) for key, value in document.items()},
                          ensure_ascii=False, separators=(",", ":")).encode())


def fixture(case, size, radius):
    import numpy as np
    from fire_risk_modeler.landscape import regional_sources, seal_landscape
    from fire_risk_modeler.models import (
        FireObservation,
        LandscapeAssessmentRequest,
        LandscapeScenario,
        WeatherObservation,
        model_input_hash,
    )
    from pyproj import Transformer
    fire = FireObservation(latitude=39.8, longitude=-105.5, name="Synthetic capacity fixture",
                           source_id="synthetic:capacity:"+case)
    x, y = Transformer.from_crs(4326, 5070, always_xy=True).transform(fire.longitude, fire.latitude)
    centre = size // 2
    offsets = (np.arange(size)-centre)*30.0
    xx, yy = np.meshgrid(offsets, offsets)
    if case == "all-burnable-wind":
        fuel, elevation, speed, direction = np.full((size, size), 109), np.full((size, size), 1000.0), 20.0, 270.0
        description = "All cells GR9; measured synthetic flat terrain; synthetic 20 m/s westerly wind."
    else:
        row, column = np.indices((size, size))
        fuel = np.array([104, 107, 109])[(row+column) % 3]
        # Explicit extreme synthetic cone, not plausible acquired terrain. Vary exact native slope/aspect states.
        elevation = 900 + np.hypot(xx, yy)*math.tan(math.radians(58)) + .2*np.sin(xx/17)*np.cos(yy/23)
        fuel[[0, -1], :] = 91
        fuel[:, [0, -1]] = 91
        speed, direction = 0.0, None
        description = ("GR4/GR7/GR9 on an extreme synthetic 58-degree cone with a declared NB91 boundary. "
                       "Synthetic calm; tests a nearly complete burnable interior and costly distinct native states.")
    raw_fuel, raw_elevation = fuel.astype("<i2").tobytes(), elevation.astype("<f4").tobytes()
    fuel_source, elevation_source = regional_sources("CONUS")
    captured = datetime.now(UTC).isoformat()
    landscape = seal_landscape(fuel, elevation, region="CONUS", fuel_source=fuel_source,
                               elevation_source=elevation_source, captured_at=captured, crs="EPSG:5070",
                               resolution_m=30.0, west=x-(centre+.5)*30, north=y+(centre+.5)*30,
                               fuel_sha256=hashlib.sha256(raw_fuel).hexdigest(),
                               elevation_sha256=hashlib.sha256(raw_elevation).hexdigest())
    scenario = LandscapeScenario(
        moisture_profile="low",
        wind_height_assumption="DTN wind treated as 10 m above ground; open unsheltered fuel assumed",
        initialization="reported point treated as demo ignition",
        horizons_minutes=[30.0, 60.0, 120.0], directional_radius=radius,
    )
    weather = WeatherObservation(query_latitude=fire.latitude, query_longitude=fire.longitude,
                                 observed_at=captured, availability="available",
                                 wind_speed_mps=speed, wind_from_degrees=direction)
    request = LandscapeAssessmentRequest(
        assessment_id=str(uuid5(NAMESPACE_URL, f"synthetic-benchmark:{case}:{size}")),
        fire_ref="synthetic:capacity:"+case, input_hash="0"*64,
        fire=fire, weather=weather, scenario=scenario, landscape=landscape)
    return request.model_copy(update={"input_hash": model_input_hash(request)}), description


@contextmanager
def observations(module, case, size, horizon):
    """Observe existing arrays/calls without modifying their values, rate calculation or graph edges."""
    import numpy as np
    actual_numpy, actual_heap, actual_native = module.np, module.heapq, module.native_wavelet
    state = {"arrivals": None, "native_states": 0, "native_wall_seconds": 0.0,
             "settled_cells": 0, "heap_pops": 0, "last_report": time.monotonic(), "started": time.monotonic()}

    def snapshot():
        reached = int(np.isfinite(state["arrivals"]).sum()) if state["arrivals"] is not None else 0
        return {"reached_cells": reached, "settled_cells": state["settled_cells"],
                "native_states": state["native_states"], "native_wall_seconds": state["native_wall_seconds"],
                "heap_pops": state["heap_pops"]}

    def progress():
        now = time.monotonic()
        if now-state["last_report"] >= 5:
            state["last_report"] = now
            emit({"event": "progress", "case": case, "cells": size*size,
                  "elapsed_seconds": now-state["started"], **snapshot(), "resources": resources()})

    class NumpyView:
        def __getattr__(self, name):
            return getattr(actual_numpy, name)

        def full(self, shape, fill, *args, **kwargs):
            value = actual_numpy.full(shape, fill, *args, **kwargs)
            if shape == (size, size) and fill == np.inf:
                state["arrivals"] = value
            return value

    class HeapView:
        def __getattr__(self, name):
            return getattr(actual_heap, name)

        def heappop(self, queue):
            value = actual_heap.heappop(queue)
            state["heap_pops"] += 1
            elapsed, row, column = value
            if elapsed == state["arrivals"][row, column] and elapsed <= horizon*60:
                state["settled_cells"] += 1
                if state["settled_cells"] % 5000 == 0:
                    progress()
            return value

    def native(*args):
        started = time.monotonic()
        value = actual_native(*args)
        state["native_wall_seconds"] += time.monotonic()-started
        state["native_states"] += 1
        if state["native_states"] % 1000 == 0:
            progress()
        return value

    module.np, module.heapq, module.native_wavelet = NumpyView(), HeapView(), native
    try:
        yield snapshot
    finally:
        module.np, module.heapq, module.native_wavelet = actual_numpy, actual_heap, actual_native


def worker(args, limits):
    from fire_risk_modeler import landscape_spread

    request, description = fixture(args.case, args.size, limits["directional_radius"])
    edges = landscape_spread.stencil(request.scenario.directional_radius)
    before = resources()
    emit({"event": "case_started", "case": args.case, "size": args.size, "cells": args.size**2,
          "description": description, "resources": before})
    started, cpu_started = time.monotonic(), time.process_time()
    with observations(landscape_spread, args.case, args.size, max(request.scenario.horizons_minutes)) as observed:
        result = landscape_spread.assess_landscape(request, maximum_seconds=None)
        metrics = observed()
    elapsed, cpu = time.monotonic()-started, time.process_time()-cpu_started
    body_size = len(result.model_dump_json().encode())
    request_size = len(request.model_dump_json().encode())
    retained_size = retained_envelope_bytes(result)
    report = {"case": args.case, "description": description, "synthetic": True, "provider_calls": 0,
              "status": "measurement_completed", "size": args.size, "input_cells": args.size**2,
              "max_input_cells_exercised": args.size**2 == limits["max_cells"],
              "directional_radius": request.scenario.directional_radius, "directions": len(edges),
              "segments_per_settled_cell": sum(len(edge[2]) for edge in edges),
              "barrier_visits_per_settled_cell": sum(len(edge[3]) for edge in edges),
              "wall_seconds": elapsed, "cpu_seconds": cpu, "instrumentation": "observation-only proxies",
              **metrics, "reached_fraction": metrics["reached_cells"]/(args.size**2),
              "availability": result.availability, "failure": result.failure.model_dump() if result.failure else None,
              "horizons_minutes": request.scenario.horizons_minutes,
              "output_features": len(result.geojson.features), "request_bytes": request_size,
              "response_bytes": body_size, "request_within_protocol_limit": request_size <= limits["request_max_bytes"],
              "response_within_protocol_limit": body_size <= limits["response_max_bytes"],
              "retained_envelope_attribute_bytes": retained_size,
              "retained_envelope_guard_bytes": RETAINED_GUARD_BYTES,
              "retained_envelope_within_guard": retained_size <= RETAINED_GUARD_BYTES,
              "resources_before": before, "resources_after": resources()}
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False)+"\n")
    emit({"event": "case_completed", **report})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    extent = parser.add_mutually_exclusive_group(required=True)
    extent.add_argument("--size", type=int, help="Explicit square grid side, 21..500; no hidden small default.")
    extent.add_argument("--max-grid", action="store_true", help="Largest square admitted by captured cell limit.")
    parser.add_argument("--case", choices=(*CASES, "all"), default="all")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--enforce-resource-limits", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    limits = specification(settings())
    if args.max_grid:
        args.size = math.isqrt(limits["max_cells"])
    if args.size is None or not 21 <= args.size <= 500 or args.size**2 > limits["max_cells"]:
        parser.error("Grid must be 21..500 cells per side and within the captured maximum cell count")
    current = resources()
    if args.enforce_resource_limits and (current["cpu_limit_vcpu"] != limits["cpu_vcpu"]
                                        or current["memory_limit_bytes"] != limits["memory_bytes"]):
        emit({"event": "resource_limits_mismatch", "expected": limits, "actual": current})
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.worker:
        worker(args, limits)
        return 0
    report = {"synthetic": True, "provider_calls": 0, "platform": sys.platform, "configured_limits": limits,
              "maximum_grid_selected": args.size**2 == limits["max_cells"],
              "simultaneous_calculations_tested": 1, "cases": [],
              "limitations": ["Not acquired landscape, Esri fire or DTN weather acceptance.",
                              "Partial reached work on domain failure is not a successful risk result.",
                              "One isolated calculation; does not prove configured concurrent calculations.",
                              "Uninstrumented service latency and HTTP delivery are separate checks."]}
    with tempfile.TemporaryDirectory(prefix="fire-risk-benchmark-") as temporary:
        for case in CASES if args.case == "all" else (args.case,):
            destination = Path(temporary)/f"{case}.json"
            command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--size", str(args.size),
                       "--case", case, "--output", str(destination)]
            if args.enforce_resource_limits:
                command.append("--enforce-resource-limits")
            # No operation timeout. The external benchmark owner may explicitly cancel this process.
            result = subprocess.run(command, check=False)  # nosec B603 - fixed owned script and typed flags, no shell
            value = json.loads(destination.read_text()) if destination.exists() else {
                "case": case, "status": "process_failed", "exit_code": result.returncode, "resources": resources()}
            report["cases"].append(value)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False)+"\n")
    emit({"event": "benchmark_completed", "output": str(args.output),
          "case_statuses": [{"case": case["case"], "status": case["status"]} for case in report["cases"]]})
    return int(any(case["status"] != "measurement_completed" for case in report["cases"]))


if __name__ == "__main__":
    raise SystemExit(main())
