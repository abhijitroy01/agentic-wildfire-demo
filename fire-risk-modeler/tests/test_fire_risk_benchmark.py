"""Capacity evidence must observe actual work, preserve failures and enforce its declared resource limits."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fire_risk_modeler import landscape_spread

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_landscape.py"
SPEC = importlib.util.spec_from_file_location("benchmark_landscape", SCRIPT)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


@pytest.mark.parametrize("case", benchmark.CASES)
def test_actual_native_observations_preserve_available_and_partial_domain_results(case, tmp_path):
    original = landscape_spread.np, landscape_spread.heapq, landscape_spread.native_wavelet
    output = tmp_path / "case.json"
    limits = benchmark.specification(benchmark.settings())
    benchmark.worker(SimpleNamespace(case=case, size=21, output=output), limits)
    report = json.loads(output.read_text())
    assert report["input_cells"] == 441 and report["directions"] == 80
    assert report["native_states"] > 0 and report["settled_cells"] > 0
    assert report["status"] == "measurement_completed" and report["synthetic"]
    assert report["provider_calls"] == 0 and not report["max_input_cells_exercised"]
    assert report["retained_envelope_attribute_bytes"] > report["response_bytes"]
    assert report["retained_envelope_within_guard"]
    assert (landscape_spread.np, landscape_spread.heapq, landscape_spread.native_wavelet) == original
    if case == "varied-terrain-fuel":
        assert report["availability"] == "available"
        assert report["reached_cells"] == report["settled_cells"] == 19**2
        assert report["native_states"] > 300 and report["output_features"] == 3
    else:
        assert report["availability"] == "unavailable" and report["failure"]["code"] == "DOMAIN_TRUNCATED"
        assert report["output_features"] == 0 and report["reached_cells"] > report["settled_cells"]


def test_observation_proxies_restore_solver_even_on_failure():
    original = landscape_spread.np, landscape_spread.heapq, landscape_spread.native_wavelet
    with (
        pytest.raises(RuntimeError, match="owned cancellation"),
        benchmark.observations(landscape_spread, "test", 21, 120) as observe,
    ):
        values = landscape_spread.np.full((21, 21), np.inf)
        values[1, 1] = 1.0
        assert observe()["reached_cells"] == 1
        raise RuntimeError("owned cancellation")
    assert (landscape_spread.np, landscape_spread.heapq, landscape_spread.native_wavelet) == original


def test_mounted_script_reads_captured_cwd_settings_without_assuming_three_parents(monkeypatch):
    monkeypatch.setattr(benchmark, "__file__", "/benchmark/benchmark_landscape.py")
    limits = benchmark.specification(benchmark.settings())
    assert limits["max_cells"] == 250_000 and limits["directional_radius"] == 5


def test_resource_mismatch_refuses_measurement_before_work(monkeypatch, tmp_path):
    output = tmp_path / "refused.json"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--size", "21", "--enforce-resource-limits",
                                     "--output", str(output)])
    monkeypatch.setattr(benchmark, "resources", lambda: {"cpu_limit_vcpu": 99, "memory_limit_bytes": 1})
    monkeypatch.setattr(benchmark.subprocess, "run",
                        lambda *_args, **_kwargs: pytest.fail("Mismatched resources must not run a worker"))
    assert benchmark.main() == 2 and not output.exists()


def test_failed_owned_child_cannot_be_reported_as_success(monkeypatch, tmp_path):
    output = tmp_path / "failed.json"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--size", "21", "--output", str(output)])
    monkeypatch.setattr(benchmark.subprocess, "run",
                        lambda *_args, **_kwargs: SimpleNamespace(returncode=-9))
    assert benchmark.main() == 1
    report = json.loads(output.read_text())
    assert all(case["status"] == "process_failed" and case["exit_code"] == -9 for case in report["cases"])
    assert report["simultaneous_calculations_tested"] == 1


def test_linux_process_rss_units_are_bytes(monkeypatch):
    monkeypatch.setattr(benchmark.sys, "platform", "linux")
    monkeypatch.setattr(benchmark.resource, "getrusage", lambda _: SimpleNamespace(ru_maxrss=4096))
    assert benchmark.resources()["process_peak_rss_bytes"] == 4096 * 1024
