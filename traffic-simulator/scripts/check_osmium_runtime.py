"""Verify the pinned extraction CLI and its complete runtime dependency closure in the image."""
import json
import shutil
import subprocess


def check():
    executable = shutil.which("osmium")
    if executable != "/opt/sumo/bin/osmium":
        raise RuntimeError("osmium must use the verified source build")
    version = subprocess.check_output([executable, "--version"], text=True)  # nosec B603 - fixed image tool
    if "osmium version 1.19.1" not in version or "libosmium version 2.23.1" not in version:
        raise RuntimeError("Road extraction does not match the reviewed native version pins")
    libraries = subprocess.check_output(["ldd", executable], text=True)  # nosec B603 B607 - fixed image tool
    if "not found" in libraries:
        raise RuntimeError("Road extraction has a missing native library")
    print("OSMIUM_RUNTIME " + json.dumps({"executable": executable, "version": version.strip(),
                                        "libraries": libraries.splitlines()}))


if __name__ == "__main__":
    check()
