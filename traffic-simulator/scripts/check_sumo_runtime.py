"""Verify the container's headless SUMO binaries and reject optional native parsers."""
import json
import re
import shutil
import subprocess  # nosec B404 - runs only the fixed version and ldd argument lists below, never a shell
from pathlib import Path

PIN = Path(__file__).resolve().parents[1] / "requirements-sumo.txt"


def pinned_version(path: Path = PIN) -> str:
    """Return the reviewed eclipse-sumo version that the source build must match."""
    match = re.search(r"^eclipse-sumo==([\w.]+)$", path.read_text(), flags=re.MULTILINE)
    if not match:
        raise RuntimeError(f"{path.name} must pin eclipse-sumo")
    return match.group(1)


def check():
    """Verify the installed sumo and netconvert report the pinned eclipse-sumo version."""
    expected = pinned_version()
    evidence = {}
    for name in ("sumo", "netconvert"):
        executable = shutil.which(name)
        if executable != f"/opt/sumo/bin/{name}":
            raise RuntimeError(f"{name} must use the verified source build")
        # The executable was just checked to be the image's own /opt/sumo/bin build; fixed arguments, no shell.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        version = subprocess.check_output([executable, "--version"], text=True)  # nosec B603 - fixed arguments
        if expected not in version:
            raise RuntimeError("Source-built SUMO does not match the reviewed version pin")
        # The image's ldd on that same executable (this gate runs only in the image build); no shell.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        libraries = subprocess.check_output(["ldd", executable], text=True)  # nosec B603 B607 - fixed arguments
        forbidden = ("not found", "libxml2", "libgdal", "libFOX", "libX11", "libGL", "libavcodec",
                     "libavformat", "libcurl", "libtiff", "libicu", "libssh", "libldap", "librtmp")
        if any(value in libraries for value in forbidden):
            raise RuntimeError(f"{name} loads a missing or excluded optional library")
        evidence[name] = {"executable": executable, "version": version.strip(),
                          "libraries": libraries.splitlines()}
    print("HEADLESS_SUMO_RUNTIME " + json.dumps(evidence))


if __name__ == "__main__":
    check()
