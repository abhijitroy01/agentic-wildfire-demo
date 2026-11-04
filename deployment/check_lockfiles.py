"""Fail closed if a reviewed dependency change has not reached its lockfile."""
import json
import re

from deployment import ROOT

NPM_PROJECTS = ("ui", "deployment/infra")
# Every requirements file a release image or Lambda package installs with --constraint requirements-lock.txt,
# plus the development environment's files (backend/deployment/requirements.txt and the root requirements.txt,
# which scripts/bootstrap-dev.sh installs with the same constraint).
PYTHON_REQUIREMENTS = (
    "traffic-simulator/requirements.txt", "traffic-simulator/requirements-sumo.txt",
    "backend/requirements-api.txt", "fire-risk-modeler/requirements.txt", "fire-risk-modeler/requirements-dev.txt",
    "firetruck-simulator/requirements.txt",
    "broadcastify/monitor/requirements.txt", "backend/runtime/requirements.txt", "backend/gateway/weather/requirements-dtn.txt",
    "requirements-dev.txt", "backend/deployment/requirements.txt", "requirements.txt",
)
COMMENT = re.compile(r"(?:^|\s)#.*")
HASH_OPTION = re.compile(r"\s--hash(?:=|\s+)\S+")
INCLUDE = re.compile(r"(?:-r\s*|--requirement(?:=|\s+))(\S+)")
REQUIREMENT = re.compile(r"([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(?:\[[^\]]*\])?\s*([^;]*?)\s*(?:;.*)?")
CLAUSE = re.compile(r"\s*(~=|===|==|!=|<=|>=|<|>)\s*([A-Za-z0-9.*+!_-]+)\s*")


def normalize(name):
    """Normalize a Python package name per PEP 503 (case-insensitive, runs of -_. become -)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_lines(path, root, seen=()):
    """Read a pip requirements file and its -r includes as pip does, yielding (file, requirement line); refuse a
    missing file, an include cycle and any other pip option."""
    path, root = path.resolve(), root.resolve()
    label = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
    if not path.is_file():
        raise RuntimeError(f"{label} is missing. Restore it, or remove it from PYTHON_REQUIREMENTS in "
                           "deployment/check_lockfiles.py if no image installs it any more.")
    if path in seen:
        raise RuntimeError(f"{label} includes itself through -r")
    # pip joins backslash continuations, then drops comments and per-requirement --hash options.
    for raw in re.sub(r"\\\r?\n", " ", path.read_text()).splitlines():
        line = HASH_OPTION.sub("", COMMENT.sub("", raw)).strip()
        if not line:
            continue
        if line.startswith("-"):
            include = INCLUDE.fullmatch(line)
            if not include:
                raise RuntimeError(f"{label} uses unsupported pip option {line!r}. Only -r includes are allowed; "
                                   "requirements-lock.txt is the only constraint and index source.")
            yield from requirement_lines((path.parent / include[1]).resolve(), root, (*seen, path))
            continue
        yield label, line


def requirements(path, root):
    """Parse a pip requirements file and its -r includes into (file, normalized name, exact pin or None)."""
    result = []
    for label, line in requirement_lines(path, root):
        match = REQUIREMENT.fullmatch(line)
        clauses = [CLAUSE.fullmatch(part) for part in match[2].split(",")] if match and match[2] else []
        if not match or not all(clauses):
            raise RuntimeError(f"{label} has a requirement that cannot be checked: {line!r}. Use a package name "
                               "with optional extras, version specifiers and markers; no URLs or paths.")
        exact = len(clauses) == 1 and clauses[0][1] == "==" and "*" not in clauses[0][2]
        result.append((label, normalize(match[1]), clauses[0][2] if exact else None))
    return result


def locked_versions(path, root):
    """Read requirements-lock.txt into {normalized name: version}, requiring one exact == pin per package."""
    locked = {}
    for _, name, version in requirements(path, root):
        if version is None:
            raise RuntimeError(f"requirements-lock.txt must pin {name} with ==; regenerate the lock.")
        if locked.setdefault(name, version) != version:
            raise RuntimeError(f"requirements-lock.txt pins {name} twice ({locked[name]} and {version}).")
    return locked


def check(root):
    """Verify npm manifests match their locks and requirements-lock.txt pins every image Python requirement."""
    root = root.resolve()
    lock_path = root / "requirements-lock.txt"
    if not lock_path.is_file():
        raise RuntimeError("requirements-lock.txt is missing; regenerate it from a clean environment")
    locked = locked_versions(lock_path, root)
    for name in PYTHON_REQUIREMENTS:
        for source, package, version in requirements(root / name, root):
            if package not in locked:
                raise RuntimeError(f"{source} requires {package} but requirements-lock.txt does not pin it. "
                                   "Add an exact pin to the lock and review the change.")
            if version is not None and version != locked[package]:
                raise RuntimeError(f"{source} pins {package}=={version} but requirements-lock.txt has "
                                   f"{locked[package]}. Update the lock and review the change.")
    for folder in NPM_PROJECTS:
        package = json.loads((root / folder / "package.json").read_text())
        lock = json.loads((root / folder / "package-lock.json").read_text())
        locked_root = lock["packages"][""]
        for group in ("dependencies", "devDependencies"):
            if package.get(group, {}) != locked_root.get(group, {}):
                raise RuntimeError(f"{folder}/package-lock.json does not match package.json. "
                                   f"Run npm --prefix {folder} install and review the audit.")


if __name__ == "__main__":
    try:
        check(ROOT)
    except (RuntimeError, FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from None
    print("Required locks checked.")
