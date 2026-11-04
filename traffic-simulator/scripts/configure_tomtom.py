"""Save a TomTom key privately for the next local server launch."""
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from traffic_simulator.traffic import LOCAL_KEY_FILE as KEY_FILE


def main():
    if KEY_FILE.exists():
        print("A local TomTom key is already saved. No changes made.")
        return
    key = getpass.getpass("Paste your TomTom Traffic API key (hidden): ").strip()
    if not 16 <= len(key) <= 512 or any(character.isspace() for character in key):
        raise SystemExit("The key must be 16–512 characters without whitespace. Nothing was saved.")
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids replacing another configuration; owner-only
    # permissions apply at creation, before any secret bytes are written.
    descriptor = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(key + "\n")
    print("Key saved privately under traffic-simulator/.runtime (excluded from git and source packages).")
    print("Restart the local API (backend/scripts/local_server.py) to load it.")
    print("Account access and highway coverage have not been checked yet.")


if __name__ == "__main__":
    main()
