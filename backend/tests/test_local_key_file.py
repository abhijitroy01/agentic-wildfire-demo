"""The local TomTom key writer and the local server read one key-file path."""
import runpy
from pathlib import Path

import pytest
from traffic_simulator.traffic import LOCAL_KEY_FILE

REPOSITORY = Path(__file__).resolve().parents[2]
WRITER = REPOSITORY / "traffic-simulator/scripts/configure_tomtom.py"
LOADER = REPOSITORY / "backend/scripts/local_server.py"


@pytest.mark.skipif(not (WRITER.exists() and LOADER.exists()),
                    reason="container verification images exclude the local development launchers")
def test_configure_tomtom_and_local_server_share_the_key_file():
    # run_path executes module setup only; neither script's main() runs, so no key file is read.
    writer, loader = runpy.run_path(str(WRITER)), runpy.run_path(str(LOADER))
    assert writer["KEY_FILE"] == loader["KEY_FILE"] == LOCAL_KEY_FILE
    assert LOCAL_KEY_FILE == REPOSITORY / "traffic-simulator/.runtime/tomtom-api-key"
