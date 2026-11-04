"""One local/CI quality entrypoint covering every active component.

Separate Python processes preserve the legacy firetruck import layout and keep
the Runtime's top-level utils, agents and schemas names apart from the root's.
All tests use fixtures and offline credentials; live acceptance remains explicit.
`coverage` runs the pytest suites under coverage.py (pinned in
requirements-dev.txt) and writes one LCOV report, coverage.lcov.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Each pytest suite runs in its own process: `test` runs them as they are, `coverage` under coverage.py.
PYTEST_SUITES = (
    (ROOT, ("-m", "pytest", "-q")),
    (ROOT, ("-m", "pytest", "broadcastify/monitor/test_private.py", "broadcastify/monitor/test_radio_pipeline.py",
            "broadcastify/monitor/test_audio_resilience.py", "-q")),
    (ROOT / "firetruck-simulator", ("-m", "pytest", "tests", "-q")),
    # The Runtime image runs this folder with unittest discover (4-agentcore-resources.yaml BuildSpec).
    (ROOT, ("-m", "pytest", "backend/runtime/tests", "-q")),
    (ROOT, ("-m", "pytest", "fire-risk-modeler/tests", "-q")),
)


def commands_for(mode, python):
    """List the (working directory, argv) pairs a quality mode runs, in order."""
    commands = []
    if mode in ("lint", "all"):
        commands.extend([
            (ROOT, [python, "-m", "ruff", "check", "."]),
            (ROOT, ["npm", "--prefix", "ui", "run", "lint", "--", "--max-warnings", "0"]),
            (ROOT / "ui", ["node", "node_modules/typescript/bin/tsc", "--noEmit"]),
            (ROOT, ["npm", "--prefix", "deployment/infra", "run", "build"]),
            (ROOT, [python, "scripts/export_integration_contracts.py", "--check"]),
            (ROOT, [python, "-m", "deployment.check_lockfiles"]),
            (ROOT, [python, "-m", "deployment.validate_infra_baseline"]),
        ])
    if mode in ("test", "all"):
        commands.extend((directory, [python, *suite]) for directory, suite in PYTEST_SUITES)
        commands.extend([
            (ROOT, [python, "backend/runtime/check_startup.py"]),
            (ROOT, ["npm", "--prefix", "ui", "run", "test:metrics"]),
            (ROOT, ["npm", "--prefix", "deployment/infra", "test"]),
        ])
    if mode == "coverage":
        # Every suite appends to its own data file beside ROOT/.coverage; combine merges them for one report.
        data = ["--data-file", str(ROOT / ".coverage")]
        commands.append((ROOT, [python, "-m", "coverage", "erase", *data]))
        commands.extend((directory, [python, "-m", "coverage", "run", "--parallel-mode", *data, *suite])
                        for directory, suite in PYTEST_SUITES)
        commands.append((ROOT, [python, "-m", "coverage", "combine", "--quiet", *data]))
        commands.append((ROOT, [python, "-m", "coverage", "lcov", *data, "-o", str(ROOT / "coverage.lcov")]))
    return commands


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("lint", "test", "coverage", "all"), default="all", nargs="?")
    options = parser.parse_args()
    environment = dict(os.environ)
    for key in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN"):
        environment.pop(key, None)
    environment.update(AWS_ACCESS_KEY_ID="offline", AWS_SECRET_ACCESS_KEY="offline",
                       AWS_EC2_METADATA_DISABLED="true", AWS_DEFAULT_REGION="us-west-2",
                       LAB_SYNTH_OFFLINE="1")
    for directory, command in commands_for(options.mode, sys.executable):
        print(f"\nChecking {directory.relative_to(ROOT) or '.'}: {' '.join(command)}", flush=True)
        subprocess.run(command, cwd=directory, env=environment, check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
