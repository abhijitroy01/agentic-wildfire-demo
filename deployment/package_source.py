"""Create the bounded release source bundle for the cloud image build.

The bundle is a deterministic ZIP of repository-relative paths, so its SHA-256
identifies the exact reviewed source. It never includes credentials, runtime
data, dependencies, recordings, .git, or files that git ignores.
"""
import io
import os
import subprocess  # nosec B404 - runs only the fixed git argument lists below, never a shell
import zipfile
from pathlib import Path

from deployment import ROOT
from deployment.check_lockfiles import check

REQUIRED_FILES = (
    "requirements-lock.txt", "requirements-dev.txt", "pyproject.toml",
    # Both platform images copy the operational settings; traffic_simulator.settings validates them at start-up.
    "deployment/settings.toml",
    "deployment/image-scan-policy.json", "deployment/image_scans.py", "deployment/release_checks.py",
    "deployment/container_build.py", "deployment/component_images.py", "deployment/runtime_build.py",
    "deployment/deploy_workspace.py", "deployment/deploy_backend.py", "deployment/runtime_binding.py",
    "deployment/acceptance/smoke_simulation_api.py", "deployment/acceptance/accept_firetruck.py",
    "deployment/acceptance/check_guidance_model.py",
    "deployment/acceptance/background_jobs.py",
    "deployment/acceptance/check_background_sources.py",
    "deployment/acceptance/check_terrain_browser.cjs",
    "deployment/acceptance/terrain_browser_checks.cjs",
    "ui/src/features/fire-exposure/services/assessment.ts",
    "ui/src/shared/constants.ts",
    "deployment/acceptance/rehearse_expo.py", "deployment/acceptance/visitor.py",
    "scripts/check_quality.py",
    "traffic-simulator/Dockerfile", "traffic-simulator/Dockerfile.dockerignore",
    "traffic-simulator/requirements.txt", "traffic-simulator/requirements-sumo.txt",
    "backend/Dockerfile.api", "backend/Dockerfile.api.dockerignore",
    "backend/requirements-api.txt",
    "backend/runtime/agents/simulation_agent/tool-inputs.json",
    "scripts/export_integration_contracts.py",
    "schemas/api-schema.yaml", "schemas/simulation/openapi.json",
    "schemas/simulation/public-tools.json", "schemas/simulation/capabilities.json",
    "ui/package.json", "ui/package-lock.json", "ui/src/features/evacuation/generated/contracts.ts",
    # Dependency-free modules that the evacuation metric check imports from outside its folder.
    "ui/src/shared/utils/typeGuards.ts", "ui/src/features/firetruck/utils/truckDisplayUtils.ts",
    "deployment/infra/Dockerfile", "deployment/infra/Dockerfile.dockerignore",
    "deployment/infra/package.json", "deployment/infra/package-lock.json",
    # No cdk.context.json: the image build synthesizes offline, so it needs no account-specific lookup cache.
    "deployment/infra/tsconfig.json", "deployment/infra/cdk.json",
    # The CDK logical-ID test reads the deploy guard's stateful types from its source.
    "deployment/change_sets.py",
    "firetruck-simulator/Dockerfile", "firetruck-simulator/requirements.txt", "firetruck-simulator/api/private.py",
    "broadcastify/monitor/Dockerfile", "broadcastify/monitor/private.py",
    "fire-risk-modeler/Dockerfile", "fire-risk-modeler/Dockerfile.dockerignore",
    "fire-risk-modeler/requirements.txt", "fire-risk-modeler/requirements-dev.txt",
    # Reviewed numerical crop fixture used by both modeler and API verification, never a production seed.
    "fire-risk-modeler/tests/data/king-landscape.json",
    "ui/src/features/fire-exposure/generated/contracts.ts",
    "schemas/fire-exposure/openapi.json",
)
SOURCE_FOLDERS = (
    "traffic-simulator/traffic_simulator", "traffic-simulator/tests", "traffic-simulator/scripts",
    "backend/investigation", "backend/evacuation", "backend/traffic_lab", "backend/tests",
    "ui/scripts", "ui/src/features/evacuation/components",
    "deployment/infra/bin", "deployment/infra/lib", "deployment/infra/test",
)
# Service images copy their whole folder, so only reviewed source file types enter the bundle.
SERVICE_FOLDERS = ("firetruck-simulator", "broadcastify", "fire-risk-modeler")
SERVICE_EXCLUDED = {"recordings", "output", "outputs", "audio", "logs", "venv", "dist", "deployment"}
EXCLUDED_PARTS = {".git", ".runtime", ".venv", ".aws", "__pycache__", "node_modules", "cdk.out"}
SECRET_NAMES = {"credentials", "credentials.json"}
# Repository-location overrides would make git answer for another checkout than root.
GIT_LOCATION = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")


def archive(files):
    """Build reproducible ZIP bytes from {archive name: bytes}; callers select the files."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, data)
    return output.getvalue()


def excluded(relative: Path) -> bool:
    return (any(part in EXCLUDED_PARTS or part.startswith(".env") for part in relative.parts)
            or relative.name in SECRET_NAMES)


def service_file(relative: Path) -> bool:
    """Select service source: Python, Dockerfiles and requirements only."""
    inner = relative.parts[1:]
    if any(part.startswith(".") or part in SERVICE_EXCLUDED for part in inner):
        return False
    return relative.name in {"Dockerfile", "requirements.txt", "requirements-dev.txt"} or relative.suffix == ".py"


def git_files(root):
    """List the paths git tracks or would add under root (git ls-files), so git-ignored files stay out."""
    env = {key: value for key, value in os.environ.items() if key not in GIT_LOCATION}
    try:
        # Fixed git arguments with no shell; root is the checkout path the deployer resolved.
        completed = subprocess.run(  # nosec B603 - fixed argument list, no shell
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            capture_output=True, env=env, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"Could not list release files with git ({exc}). "
                         "Install git and deploy from a git checkout.") from None
    if completed.returncode:
        raise ValueError(f"{root} is not a git work tree. Deploy from a git clone of the repository so files "
                         "that git ignores (keys, local settings) stay out of the release bundle.")
    return {Path(os.fsdecode(name)) for name in completed.stdout.split(b"\0") if name}


def collect_sources(root, *, git=True):
    """Return the sorted absolute paths of the release bundle; git=True drops files git ignores."""
    root = root.resolve()
    files = [root / name for name in REQUIRED_FILES]
    missing = [str(p.relative_to(root)) for p in files if not p.is_file()]
    if missing:
        raise ValueError(f"Source bundle is not ready. Missing required files: {', '.join(missing)}")
    visible = git_files(root) if git else None
    if visible is not None:
        ignored = [name for name in REQUIRED_FILES if Path(name) not in visible]
        if ignored:
            raise ValueError(f"Required release files are ignored by git: {', '.join(ignored)}. "
                             "Track them or fix .gitignore.")
    files.extend((root / "backend").glob("*.py"))
    for folder in SOURCE_FOLDERS + SERVICE_FOLDERS:
        for path in (root / folder).rglob("*"):
            relative = path.relative_to(root)
            if (not path.is_file() and not path.is_symlink()) or excluded(relative):
                continue
            if folder in SERVICE_FOLDERS and not service_file(relative):
                continue
            if visible is not None and relative not in visible:
                continue
            files.append(path)
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Source bundle cannot include a symlink or external file: {path.relative_to(root)}")
    return sorted(set(files))


def source_bundle(root, *, git=True):
    """Return the deterministic release ZIP bytes of root after the lock check; their SHA-256 names the source."""
    root = root.resolve()
    check(root)
    return archive({path.relative_to(root).as_posix(): path.read_bytes()
                    for path in collect_sources(root, git=git)})


def git_commit(root):
    """Return root's HEAD commit and whether it has uncommitted changes, for the release record."""
    env = {key: value for key, value in os.environ.items() if key not in GIT_LOCATION}
    result = {}
    for name, args in (("commit", ["rev-parse", "HEAD"]), ("status", ["status", "--porcelain"])):
        try:
            # Fixed git arguments from the tuple above with no shell; root is the resolved checkout path.
            completed = subprocess.run(  # nosec B603 - fixed argument list, no shell
                ["git", "-C", str(root), *args], capture_output=True, text=True, env=env, timeout=60, check=False)
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        result[name] = completed.stdout.strip() if completed and completed.returncode == 0 else None
    return {"commit": result["commit"] or "unknown",
            "uncommitted_changes": None if result["status"] is None else bool(result["status"])}


def package_source(root, destination=None, *, git=True):
    """Write the deterministic release ZIP and return its path; locks are checked first."""
    root = root.resolve()
    destination = destination or root / ".runtime/source.zip"
    payload = source_bundle(root, git=git)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_bytes(payload)
    temporary.replace(destination)
    return destination


if __name__ == "__main__":
    try:
        print(package_source(ROOT))
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        raise SystemExit(str(exc)) from None
