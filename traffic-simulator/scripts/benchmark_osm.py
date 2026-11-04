"""Measure the canonical road path and real SUMO accounting for an explicitly requested study area.

Run with STORAGE_MODE=local. This command resolves public Geofabrik metadata and downloads its selected dated
source once; it never submits jobs or writes to AWS. Each invocation uses fresh isolated scratch so "cold" cannot
mean an existing derived network. The warm run uses that same captured source, without resolving a second snapshot.
"""
import argparse
import hashlib
import json
import os
import platform
import resource
import signal
import stat
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from traffic_simulator import network, osm_extracts, settings, simulation
from traffic_simulator.demand import demand_for
from traffic_simulator.errors import IntegrationError
from traffic_simulator.models import PreparationScenario, Scenario
from traffic_simulator.timings import Timings


class ProofError(RuntimeError):
    """A curated benchmark-invariant failure safe to include in the final JSON."""


def digest(value):
    """Hash complete plain inputs with deterministic JSON; never omit unresolved demand."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cgroup_resources(root=Path("/sys/fs/cgroup")):
    """Read effective v1/v2 quotas without mistaking separate process RSS for aggregate memory."""
    def read(name):
        try:
            return (root / name).read_text().strip()
        except OSError:
            return None

    def number(value):
        return int(value) if value and value.lstrip("-").isdecimal() else None

    raw = read("cpu.max")
    if raw:
        quota, period = raw.split()
        quota, period = number(quota), number(period)
        version = 2
        maximum, current, peak = (number(read(name)) for name in ("memory.max", "memory.current", "memory.peak"))
    else:
        quota = number(read("cpu/cpu.cfs_quota_us") or read("cpu,cpuacct/cpu.cfs_quota_us"))
        period = number(read("cpu/cpu.cfs_period_us") or read("cpu,cpuacct/cpu.cfs_period_us"))
        version = 1 if quota is not None else None
        maximum, current, peak = (number(read("memory/" + name)) for name in
                                  ("memory.limit_in_bytes", "memory.usage_in_bytes", "memory.max_usage_in_bytes"))
    return {
        "cgroup_version": version,
        "cgroup_cpu_limit_vcpu": quota / period if quota is not None and quota > 0 and period else None,
        "cgroup_memory_limit_bytes": maximum,
        "cgroup_memory_current_bytes": current,
        "cgroup_memory_peak_bytes": peak,
        "cgroup_scope": "Container aggregate high-water mark, when exposed; may include other container work",
    }


def resources():
    """Observed process/child high-water marks, plus cgroup limits when this process runs in a Linux container.

    Linux ru_maxrss is KiB; macOS uses bytes. Child RSS is the largest reaped child, not simultaneous aggregate
    memory. The v1/v2 cgroup memory peak is the container aggregate when exposed and can include other container work.
    """
    factor = 1 if sys.platform == "darwin" else 1024
    own, children = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    values = {
        "process_peak_rss_bytes": int(own.ru_maxrss * factor),
        "reaped_child_peak_rss_bytes": int(children.ru_maxrss * factor),
        "process_cpu_seconds": round(own.ru_utime + own.ru_stime, 3),
        "reaped_child_cpu_seconds": round(children.ru_utime + children.ru_stime, 3),
        "rss_scope": "Independent process and largest reaped-child high-water marks; do not add as aggregate peak",
    }
    values.update(cgroup_resources())
    return values


def scratch_bytes(root):
    """Count only this proof's currently retained regular files, tolerating concurrent native cleanup."""
    content, allocated = 0, 0
    for path in root.rglob("*"):
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode):
            content += info.st_size
            allocated += info.st_blocks * 512
    return content, allocated


@contextmanager
def scratch_observations(root, interval=.1):
    """Sample this owned scratch tree; a sampled high-water mark is not an enforced quota or exact peak."""
    values = {"sample_interval_seconds": interval, "samples": 0, "sample_errors": 0,
              "sampled_peak_content_bytes": 0, "sampled_peak_allocated_file_bytes": 0,
              "scope": "Owned regular-file samples; excludes filesystem metadata and changes between samples"}
    done = threading.Event()

    def sample():
        try:
            content, allocated = scratch_bytes(root)
        except OSError:
            values["sample_errors"] += 1
            return
        values["samples"] += 1
        values["sampled_peak_content_bytes"] = max(values["sampled_peak_content_bytes"], content)
        values["sampled_peak_allocated_file_bytes"] = max(values["sampled_peak_allocated_file_bytes"], allocated)

    def watch():
        while not done.wait(interval):
            sample()

    sample()
    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        yield values
    finally:
        done.set()
        thread.join()
        sample()


def native_versions(check):
    """Record actual binaries, rather than relying on image tags or recipe labels."""
    return {
        name: {"executable": executable, "version": network.command([executable, "--version"], check).strip()}
        for name, executable in (("osmium", osm_extracts.tool()), ("netconvert", network.binary("netconvert")),
                                 ("sumo", network.binary("sumo")))
    }


def prepared_run(pending, source, progress, check):
    """Prepare one actual network, preserve exact seeded demand and run the production baseline policy."""
    started, timings = time.monotonic(), Timings()
    prepared = network.prepare_network(pending, progress, check, timings=timings, source_manifest=source)
    resolved = Scenario.model_validate(prepared[7]["resolved_scenario"])
    civilians, crews = demand_for(resolved, *prepared[1:6], check_cancel=check)
    demand = (civilians, crews)
    with timings.measure("simulation"):
        result = simulation.simulate(resolved, prepared, progress, check, demand=demand, parallelism=1,
                                     timings=timings)
    summaries = {policy["policy"]: policy["summary"] for policy in result["policies"]}
    for summary in summaries.values():
        if not summary["valid_engine_run"] or not summary["valid_accounting"]:
            raise ProofError("The native road proof failed engine or demand accounting checks")
        if (summary["exited_vehicles"] + summary["active_vehicles"] + summary["not_departed_vehicles"]
                != resolved.vehicles):
            raise ProofError("The native road proof failed civilian demand conservation")
    metadata = prepared[7]
    return {
        "seconds": round(time.monotonic() - started, 3),
        "timings": timings.public(),
        "resolved_scenario": resolved.model_dump(),
        "edge_count": len(prepared[2]),
        "gate_ids": [gate.getID() for gate in prepared[4]],
        "corridor_label": metadata["corridor_label"],
        "study_bounds": metadata["study_bounds"],
        "network_sha256": hashlib.sha256(prepared[0].read_bytes()).hexdigest(),
        "road_xml_sha256": metadata["osm_sha256"],
        "source_identity": osm_extracts.identity(metadata["source_manifest"]),
        "demand_sha256": digest(demand),
        "demand_counts": {"civilian_requested": len(civilians),
                          "civilian_routable": sum(bool(record["route"]) for record in civilians),
                          "crew_requested": len(crews),
                          "crew_routable": sum(bool(record["route"]) for record in crews)},
        "sumo_summaries": summaries,
        "resources": resources(),
    }


def benchmark(pending, progress, check):
    """Capture one current source and prove cold/warm equality without a second metadata or provider resolution."""
    source_timings = Timings()
    started = time.monotonic()
    source = osm_extracts.plan(pending, network.command, progress, check, source_timings)
    source_seconds = time.monotonic() - started
    progress("Cold extraction, conversion, demand and actual SUMO baseline", 0)
    cold = prepared_run(pending, source, progress, check)
    if "network_cache_hit" in cold["timings"] or "network_cache_download" in cold["timings"]:
        raise ProofError("The cold road proof unexpectedly reused an existing derived network")
    progress("Warm exact-source network reuse, demand and actual SUMO baseline", 0)
    warm = prepared_run(pending, source, progress, check)
    compared = ("resolved_scenario", "study_bounds", "gate_ids", "network_sha256", "road_xml_sha256",
                "source_identity", "demand_sha256", "demand_counts", "sumo_summaries")
    equality = {key: cold[key] == warm[key] for key in compared}
    if not all(equality.values()) or "network_cache_hit" not in warm["timings"]:
        raise ProofError("The warm road proof did not preserve the cold source, graph, demand and native outcomes")
    return {
        "status": "passed", "requested_scenario": pending.model_dump(),
        "source_manifest": source, "source_resolution_seconds": round(source_seconds, 3),
        "source_timings": source_timings.public(), "cold": cold, "warm": warm, "same_inputs": equality,
        "meaning": "Observed OSM roads, synthetic matched demand and simulated baseline; no safety or closure claim",
    }


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lat", required=True, type=float)
    parser.add_argument("--lon", required=True, type=float)
    parser.add_argument("--radius-km", required=True, type=float)
    parser.add_argument("--vehicles", type=int, default=20)
    parser.add_argument("--crews", type=int, default=2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--duration-minutes", type=int, default=5)
    parser.add_argument("--departure-minutes", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--data-dir", type=Path, help="Parent for a fresh isolated benchmark scratch directory")
    return parser.parse_args(argv)


def main(argv=None):
    args = arguments(argv)
    if settings.CLOUD:
        raise RuntimeError("Road benchmarks require STORAGE_MODE=local and never use AWS stores")
    pending = PreparationScenario(
        lat=args.lat, lon=args.lon, radius_km=args.radius_km, vehicles=args.vehicles, crews=args.crews,
        seed=args.seed, duration_minutes=args.duration_minutes, departure_minutes=args.departure_minutes,
        hazard_radius_m=0, hazard_growth_m_min=0, traffic_mode="synthetic", policies=["baseline"])
    if args.data_dir:
        args.data_dir.mkdir(parents=True, exist_ok=True)
    started, output = time.monotonic(), sys.stdout
    cancelled = False

    def stop(*_):
        nonlocal cancelled
        cancelled = True

    def check():
        if cancelled:
            raise InterruptedError("Road proof cancelled")

    def progress(phase, fraction):
        print(json.dumps({"event": "phase", "phase": phase, "fraction": fraction,
                          "elapsed_seconds": round(time.monotonic() - started, 3)}), file=output, flush=True)

    previous = {name: signal.signal(name, stop) for name in (signal.SIGINT, signal.SIGTERM)}
    report = {"event": "result", "started_at": datetime.now(UTC).isoformat(),
              "platform": {"system": platform.system(), "machine": platform.machine()},
              "requested_scenario": pending.model_dump(),
              "limits": {"source_http_timeout_seconds": settings.osm_source_http_timeout_seconds(),
                         "preparation_timeout_seconds": settings.road_preparation_timeout_seconds(),
                         "source_max_mib": settings.osm_source_max_mib(),
                         "source_cache_mib": settings.osm_source_cache_mib_per_worker(),
                         "road_xml_max_mb": settings.osm_road_xml_max_mb()}}
    original_data = settings.DATA
    try:
        with tempfile.TemporaryDirectory(prefix="osm-proof-", dir=args.data_dir) as scratch:
            settings.DATA = Path(scratch)
            disk = os.statvfs(scratch)
            report["scratch"] = {"capacity_bytes": disk.f_blocks * disk.f_frsize,
                                 "available_bytes_at_start": disk.f_bavail * disk.f_frsize,
                                 "cache_scope": "Fresh isolated local source and derived-network caches"}
            # TraCI startup can write progress to stdout. Keep stdout exclusively machine-readable JSON.
            with scratch_observations(Path(scratch)) as observation:
                report["scratch"]["file_observation"] = observation
                with redirect_stdout(sys.stderr):
                    report["native_versions"] = native_versions(check)
                    report.update(benchmark(pending, progress, check))
            report["scratch"]["available_bytes_at_end"] = os.statvfs(scratch).f_bavail * disk.f_frsize
            report["scratch"]["retained_bytes_at_end"] = sum(
                path.stat().st_size for path in Path(scratch).rglob("*") if path.is_file())
    except IntegrationError as exc:
        report.update(status="unavailable", error_code=exc.code, error=exc.message)
    except InterruptedError:
        report.update(status="cancelled", error="Road proof cancelled")
    except ProofError as exc:
        report.update(status="failed", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - CLI boundary writes a safe machine-readable failure and exits nonzero.
        report.update(status="failed", error_type=type(exc).__name__,
                      error="Road proof failed; inspect the flushed phase records and native tool logs")
    finally:
        settings.DATA = original_data
        for name, handler in previous.items():
            signal.signal(name, handler)
    report.update(completed_at=datetime.now(UTC).isoformat(), seconds=round(time.monotonic() - started, 3),
                  resources=resources())
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), file=output, flush=True)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
