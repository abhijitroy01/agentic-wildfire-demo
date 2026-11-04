"""Run the workspace API locally with the in-process SUMO worker on http://127.0.0.1:8000.

Loads an optional TomTom key saved by traffic-simulator/scripts/configure_tomtom.py,
then replaces this process with uvicorn so the key never appears in process arguments.
Run from anywhere: .venv/bin/python backend/scripts/local_server.py
"""
import os
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY / "traffic-simulator"))

from traffic_simulator.traffic import LOCAL_KEY_FILE as KEY_FILE


def main():
    """Start uvicorn for the workspace API in local storage mode with an optional saved TomTom key."""
    # The simulator requires an explicit storage mode; this launcher is always local.
    os.environ.setdefault("STORAGE_MODE", "local")
    if not os.environ.get("TOMTOM_API_KEY") and KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
        if not 16 <= len(key) <= 512 or any(character.isspace() for character in key):
            raise SystemExit("The saved TomTom key has an invalid format; server was not started.")
        os.environ["TOMTOM_API_KEY"] = key
    os.environ["PYTHONPATH"] = os.pathsep.join(
        str(REPOSITORY / folder) for folder in ("traffic-simulator", "fire-risk-modeler", "."))
    os.chdir(REPOSITORY)
    # Replaces this process with this interpreter running uvicorn on fixed arguments (loopback only); no shell.
    # nosemgrep: python.lang.security.audit.dangerous-os-exec-audit
    os.execv(sys.executable, [sys.executable, "-m", "uvicorn", "backend.app:app",  # nosec B606
                              "--host", "127.0.0.1", "--port", "8000"])


if __name__ == "__main__":
    main()
