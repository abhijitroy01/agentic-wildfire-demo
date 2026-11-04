"""The maintained native proof must preserve one source and demand, and fail rather than certify invalid output."""
import importlib.util
import json
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from traffic_simulator.models import PreparationScenario, Scenario

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_osm.py"
SPEC = importlib.util.spec_from_file_location("benchmark_osm", SCRIPT)
benchmark_osm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark_osm)


def proof_runs():
    cold = {"resolved_scenario": {"seed": 42}, "study_bounds": [[-123, 47, -122, 48]], "gate_ids": ["1"],
            "network_sha256": "network", "road_xml_sha256": "roads", "source_identity": {"sha256": "source"},
            "demand_sha256": "all-demand", "demand_counts": {"civilian_requested": 20, "crew_requested": 2},
            "sumo_summaries": {"baseline": {"valid_accounting": True}}, "timings": {"osm_extract": 2}}
    warm = deepcopy(cold)
    warm["timings"] = {"network_cache_hit": .01}
    return cold, warm


def setup_runs(monkeypatch, cold, warm):
    source, plans, captures = {"publisher": "Geofabrik", "sources": ["exact-fixture"]}, [], []

    def plan(*args):
        plans.append(args)
        return source

    def run(_pending, selected, *_):
        captures.append(selected)
        return cold if len(captures) == 1 else warm

    monkeypatch.setattr(benchmark_osm.osm_extracts, "plan", plan)
    monkeypatch.setattr(benchmark_osm, "prepared_run", run)
    return source, plans, captures


def test_cold_warm_proof_resolves_one_source_and_compares_complete_demand(monkeypatch):
    cold, warm = proof_runs()
    source, plans, captures = setup_runs(monkeypatch, cold, warm)
    result = benchmark_osm.benchmark(PreparationScenario(), lambda *_: None, lambda: None)
    assert result["status"] == "passed" and all(result["same_inputs"].values())
    assert len(plans) == 1 and len(captures) == 2
    assert all(selected is source for selected in captures)


@pytest.mark.parametrize("changed", ["demand_sha256", "source_identity", "network_sha256", "sumo_summaries"])
def test_mismatched_warm_inputs_or_native_outcomes_never_pass(monkeypatch, changed):
    cold, warm = proof_runs()
    warm[changed] = "mismatch"
    setup_runs(monkeypatch, cold, warm)
    with pytest.raises(benchmark_osm.ProofError, match="did not preserve"):
        benchmark_osm.benchmark(PreparationScenario(), lambda *_: None, lambda: None)


def test_cold_label_requires_a_real_derived_network_miss(monkeypatch):
    cold, warm = proof_runs()
    cold["timings"]["network_cache_hit"] = .01
    setup_runs(monkeypatch, cold, warm)
    with pytest.raises(benchmark_osm.ProofError, match="cold road proof"):
        benchmark_osm.benchmark(PreparationScenario(), lambda *_: None, lambda: None)


def test_linux_peak_rss_is_normalized_without_inventing_an_aggregate(monkeypatch):
    monkeypatch.setattr(benchmark_osm.sys, "platform", "linux")
    monkeypatch.setattr(benchmark_osm.resource, "getrusage",
                        lambda _: SimpleNamespace(ru_maxrss=4096, ru_utime=1, ru_stime=.5))
    result = benchmark_osm.resources()
    assert result["process_peak_rss_bytes"] == result["reaped_child_peak_rss_bytes"] == 4096 * 1024
    assert result["process_cpu_seconds"] == 1.5 and "do not add" in result["rss_scope"]


@pytest.mark.parametrize("version", [1, 2])
def test_cgroup_readers_report_effective_container_quota_and_aggregate_peak(tmp_path, version):
    files = ({"cpu.max": "400000 100000", "memory.max": str(8 * 1024**3),
              "memory.current": "2000000", "memory.peak": "5000000"} if version == 2 else
             {"cpu/cpu.cfs_quota_us": "400000", "cpu/cpu.cfs_period_us": "100000",
              "memory/memory.limit_in_bytes": str(8 * 1024**3),
              "memory/memory.usage_in_bytes": "2000000", "memory/memory.max_usage_in_bytes": "5000000"})
    for name, value in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    result = benchmark_osm.cgroup_resources(tmp_path)
    assert result["cgroup_version"] == version and result["cgroup_cpu_limit_vcpu"] == 4
    assert result["cgroup_memory_limit_bytes"] == 8 * 1024**3
    assert result["cgroup_memory_current_bytes"] == 2000000 and result["cgroup_memory_peak_bytes"] == 5000000


def test_owned_scratch_samples_preserve_a_removed_intermediate_high_water(tmp_path):
    output = tmp_path / "native-intermediate.pbf"
    with benchmark_osm.scratch_observations(tmp_path, interval=.01) as observed:
        output.write_bytes(b"x" * 1024)
        deadline = time.monotonic() + 3
        while observed["sampled_peak_content_bytes"] < 1024 and time.monotonic() < deadline:
            time.sleep(.01)
        assert observed["sampled_peak_content_bytes"] == 1024
        output.unlink()
    assert observed["samples"] >= 2 and observed["sample_errors"] == 0
    assert observed["sampled_peak_content_bytes"] == 1024 and benchmark_osm.scratch_bytes(tmp_path) == (0, 0)
    assert "changes between samples" in observed["scope"]


@pytest.mark.parametrize("changed", [
    {"valid_engine_run": False}, {"valid_accounting": False}, {"exited_vehicles": 19},
])
def test_invalid_engine_or_demand_accounting_is_refused(monkeypatch, changed):
    scenario = Scenario(vehicles=20, crews=2, policies=["baseline"])
    prepared = (None, None, None, None, None, None, None, {"resolved_scenario": scenario.model_dump()})
    monkeypatch.setattr(benchmark_osm.network, "prepare_network", lambda *_args, **_kwargs: prepared)
    monkeypatch.setattr(benchmark_osm, "demand_for", lambda *_args, **_kwargs: ([], []))
    summary = {"valid_engine_run": True, "valid_accounting": True, "exited_vehicles": 20,
               "active_vehicles": 0, "not_departed_vehicles": 0, **changed}
    monkeypatch.setattr(benchmark_osm.simulation, "simulate",
                        lambda *_args, **_kwargs: {"policies": [{"policy": "baseline", "summary": summary}]})
    with pytest.raises(benchmark_osm.ProofError, match="accounting|conservation"):
        benchmark_osm.prepared_run(PreparationScenario(), {}, lambda *_: None, lambda: None)


@pytest.mark.integration
def test_actual_native_entrypoint_writes_identity_and_conserved_cold_warm_counts(monkeypatch, tmp_path, capsys):
    from test_network_providers import source_manifest
    from test_osm_corridors import road_xml

    plans = []

    def plan(*args):
        plans.append(args)
        return source_manifest()

    monkeypatch.setattr(benchmark_osm.osm_extracts, "plan", plan)
    monkeypatch.setattr(benchmark_osm.osm_extracts, "extract", lambda *_: (road_xml(), "fixture-sha"))
    output = tmp_path / "native-proof.json"
    assert benchmark_osm.main(["--lat", "47.6062", "--lon", "-122.3321", "--radius-km", "3",
                               "--output", str(output), "--data-dir", str(tmp_path / "scratch")]) == 0
    report = json.loads(output.read_text())
    assert len(plans) == 1 and report["status"] == "passed"
    assert all(report["same_inputs"].values())
    assert report["scratch"]["file_observation"]["samples"] > 1
    assert report["scratch"]["file_observation"]["sample_errors"] == 0
    for run in (report["cold"], report["warm"]):
        summary = run["sumo_summaries"]["baseline"]
        assert summary["valid_accounting"] and summary["valid_engine_run"]
        assert summary["exited_vehicles"] + summary["active_vehicles"] + summary["not_departed_vehicles"] == 20
        assert summary["crews_requested"] == 2 and run["demand_counts"]["crew_routable"] == 2
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[-1] == report and any(record["event"] == "phase" for record in records[:-1])
