"""The API image's ui-metrics stage, rebuilt locally from the release bundle (UIWS-6).

The stage copies a few UI folders and files, then runs the policy-metrics node test against them. A file it
needs that the stage does not copy, or that the release bundle leaves out, otherwise fails only in the cloud
image build at deploy time.
"""
import os
import re
import shlex
import shutil
import subprocess  # nosec B404 - runs only the Dockerfile's own node command, never a shell
import zipfile
from pathlib import Path

import pytest
from dockerfile_layout import dockerignore_rules, image_layout, instructions, stage

from deployment.package_source import git_files, package_source

REPOSITORY = Path(__file__).resolve().parents[2]
DOCKERFILE = "backend/Dockerfile.api"


def test_the_ui_metrics_stage_passes_from_the_release_bundle(tmp_path):
    """Copy what the stage copies, from the bundle the cloud build receives, and run the stage's own test."""
    if not shutil.which("node") or not (REPOSITORY / "ui/node_modules/typescript").exists():
        pytest.skip("Install the UI dependencies (npm ci in ui/) to run the ui-metrics stage locally.")
    with zipfile.ZipFile(package_source(REPOSITORY, tmp_path / "release.zip")) as archive:
        bundle = {name: archive.read(name) for name in archive.namelist()}
    rules = dockerignore_rules(bundle[DOCKERFILE + ".dockerignore"].decode())
    lines = stage(instructions(bundle[DOCKERFILE].decode()), "ui-metrics")
    runs = [shlex.split(line[4:]) for line in lines if line.startswith("RUN ")]
    # The dependency install becomes a link to ui/node_modules; any other step would go unchecked here.
    assert [args[:2] for args in runs] == [["npm", "ci"], ["node", "--test"]], runs
    workdir, layout = image_layout(lines, rules, bundle)
    # Everything the stage would copy from a checkout is in the bundle, so the cloud build sees the same files.
    _, from_checkout = image_layout(lines, rules,
                                   {path.as_posix() for path in git_files(REPOSITORY)
                                    if (REPOSITORY / path).is_file()})
    assert sorted(from_checkout.keys() - bundle.keys()) == []
    image = tmp_path / "image"
    for name, target in layout.items():
        (image / target.lstrip("/")).parent.mkdir(parents=True, exist_ok=True)
        (image / target.lstrip("/")).write_bytes(bundle[name])
    app = image / workdir.lstrip("/")
    (app / "node_modules").symlink_to(REPOSITORY / "ui/node_modules", target_is_directory=True)
    # The node found on PATH runs the stage's own fixed command in the stage's folder. No shell.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    done = subprocess.run(  # nosec B603 - the Dockerfile's fixed argument list, no shell
        runs[-1], cwd=app, env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-2000:]
    assert re.search(r"^ℹ tests [1-9]\d*$", done.stdout, re.MULTILINE), done.stdout[-2000:]
