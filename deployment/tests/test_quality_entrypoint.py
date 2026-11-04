"""Tests for scripts/check_quality.py: the coverage mode measures exactly the pytest suites the test mode runs, and
the UI node suites it starts are found."""
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_quality_under_test", ROOT / "scripts" / "check_quality.py")
check_quality = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_quality)
DATA = ["--data-file", str(ROOT / ".coverage")]


def pytest_runs(mode):
    """The (directory, arguments after the interpreter) of every pytest process a mode starts."""
    return [(directory, command[1:]) for directory, command in check_quality.commands_for(mode, "py")
            if command[1:3] == ["-m", "pytest"]]


def test_coverage_mode_wraps_every_pytest_suite_of_the_test_mode_and_writes_one_lcov_report():
    commands = check_quality.commands_for("coverage", "py")
    suites = pytest_runs("test")
    assert len(suites) == 5, "root, radio, firetruck, Runtime and fire risk modeler suites run in separate processes"
    assert (ROOT, ["-m", "pytest", "backend/runtime/tests", "-q"]) in suites, "the Runtime release checks"
    assert (ROOT, ["-m", "pytest", "fire-risk-modeler/tests", "-q"]) in suites, "the native modeler checks"
    assert commands[0] == (ROOT, ["py", "-m", "coverage", "erase", *DATA])
    assert commands[1:-2] == [(directory, ["py", "-m", "coverage", "run", "--parallel-mode", *DATA, *arguments])
                              for directory, arguments in suites]
    assert commands[-2] == (ROOT, ["py", "-m", "coverage", "combine", "--quiet", *DATA])
    assert commands[-1] == (ROOT, ["py", "-m", "coverage", "lcov", *DATA, "-o", str(ROOT / "coverage.lcov")])


def test_coverage_mode_starts_only_coverage_processes_and_all_mode_leaves_it_out():
    assert all(command[1:3] == ["-m", "coverage"] for _, command in check_quality.commands_for("coverage", "py"))
    assert not any(command[1:3] == ["-m", "coverage"] for _, command in check_quality.commands_for("all", "py"))
    assert pytest_runs("all") == pytest_runs("test")


def test_radio_quality_gate_includes_every_monitor_regression_suite():
    expected = {path.relative_to(ROOT).as_posix() for path in (ROOT / "broadcastify/monitor").glob("test_*.py")}
    included = {argument for _, command in pytest_runs("test") for argument in command
                if argument.startswith("broadcastify/monitor/test_")}
    assert expected and included == expected, "New radio regression files must join the permanent quality gate"


def test_every_command_runs_in_order_offline_and_without_the_callers_aws_profile(monkeypatch):
    calls = []
    monkeypatch.setattr(check_quality.subprocess, "run",
                        lambda command, cwd, env, check: calls.append((cwd, command, env, check)))
    monkeypatch.setattr(sys, "argv", ["check_quality.py", "coverage"])
    monkeypatch.setenv("AWS_PROFILE", "developer-default")
    assert check_quality.main() == 0
    assert [(cwd, command) for cwd, command, _, _ in calls] == check_quality.commands_for("coverage", sys.executable)
    for _, _, env, check in calls:
        assert check is True, "a failing suite must stop the run"
        assert "AWS_PROFILE" not in env
        assert (env["AWS_ACCESS_KEY_ID"], env["AWS_EC2_METADATA_DISABLED"]) == ("offline", "true")


def test_the_ui_node_test_glob_matches_the_named_suites():
    """S-F2: node --test exits 0 when its glob matches nothing, so the test:metrics glob must find the UI suites."""
    script = json.loads((ROOT / "ui/package.json").read_text())["scripts"]["test:metrics"]
    quoted = re.fullmatch(r'node --test "([^"]+)"', script)
    assert quoted, f"test:metrics is no longer node --test with one quoted glob, so update this check: {script}"
    suites = {path.name.removesuffix(".test.cjs") for path in (ROOT / "ui").glob(quoted[1])}
    assert {"untrusted-input", "traffic-lab-access", "policy-metrics", "camera", "fire-exposure"} <= suites, (
        quoted[1], sorted(suites))
