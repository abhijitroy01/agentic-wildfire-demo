"""Pytest plugin for tests only: a real 5x5 SUMO grid network built with the installed netconvert.

Registered by the tests' conftest.py files (pytest_plugins = ["traffic_simulator.testing"]).
Runtime code never imports this module; it lives in the package because the workspace API
verification image copies only traffic_simulator/ and backend/tests.
"""
import subprocess  # nosec B404 - runs only the fixed netconvert argument list below, never a shell

# This module only builds and writes the grid's node and edge files; it parses no XML, so XML entity attacks do not
# apply (and Python 3.14's expat does not fetch external entities).
# nosemgrep: python.lang.security.use-defused-xml
from xml.etree.ElementTree import Element, ElementTree, SubElement  # nosec B405 - writes XML, never parses

import pytest
import sumolib
from shapely.geometry import LineString

from traffic_simulator.network import binary


def stalled_http_transport(payload, progress=None):
    """Spawned test fixture only: mark actual idle-read entry before the parent cancels its owned child."""
    import os
    from pathlib import Path
    from unittest.mock import patch
    from urllib.parse import urlparse

    import httpx
    from httpcore._backends.sync import SyncStream

    parsed = urlparse(payload["url"])
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise ValueError("Transport fixtures require a local test server")
    actual_read, count = SyncStream.read, 0

    def read(stream, *args, **kwargs):
        nonlocal count
        count += 1
        if count >= 2:
            Path(payload["marker"]).write_text(str(os.getpid()))
        return actual_read(stream, *args, **kwargs)

    with patch.object(SyncStream, "read", read), httpx.Client(timeout=None, trust_env=False) as http:
        return {"bytes": len(http.get(payload["url"]).content)}


def progress_transport(payload, progress=None):
    """Spawned fixture only; native/HTTP progress is observable without putting a byte file in the pipe."""
    import os
    import time
    from pathlib import Path

    if payload.get("marker"):
        Path(payload["marker"]).write_text(str(os.getpid()))
    if progress is not None:
        progress(payload.get("phase", "Downloading us/washington: 1024 / 2048 bytes"),
                 payload.get("fraction", .04))
    while payload.get("block_after_progress"):
        time.sleep(.1)
    return {"pid": os.getpid(), "bytes": 2048}


@pytest.fixture
def grid(tmp_path):
    """Build a bidirectional grid near Paradise, CA and return the simulator's network tuple."""
    nodes, edges = Element("nodes"), Element("edges")
    for row in range(5):
        for col in range(5):
            SubElement(nodes, "node", id=f"n{row}_{col}", x=str(-121.6339 + col * .006),
                       y=str(39.7496 + row * .005), type="priority")
            for nr, nc in ((row + 1, col), (row, col + 1)):
                if nr < 5 and nc < 5:
                    a, b = f"n{row}_{col}", f"n{nr}_{nc}"
                    for start, end in ((a, b), (b, a)):
                        SubElement(edges, "edge", id=f"{start}-{end}",
                                   attrib={"from": start, "to": end}, numLanes="1", speed="13.89")
    nodefile, edgefile, netfile = [tmp_path / p for p in ("nodes.xml", "edges.xml", "net.xml")]
    ElementTree(nodes).write(nodefile)
    ElementTree(edges).write(edgefile)
    # The installed netconvert with this fixture's own files in tmp_path; no shell.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    subprocess.run([binary("netconvert"), "--node-files", str(nodefile), "--edge-files", str(edgefile),  # nosec B603
                    "--proj.utm", "true", "--output-file", str(netfile)], check=True, capture_output=True)
    net = sumolib.net.readNet(str(netfile))
    road_edges = list(net.getEdges())
    lines = {e.getID(): LineString(e.getShape()) for e in road_edges}
    gate = net.getEdge("n1_2-n0_2")
    center = net.convertLonLat2XY(-121.6219, 39.7596)
    return netfile, net, road_edges, lines, [gate], center, {}, {}


def create_job(store, owner, scenario):
    """Create a local worker fixture without exposing a second production submission path."""
    import json
    import time
    from uuid import uuid4

    from traffic_simulator.persistence import expires_at

    if store.cloud:
        raise ValueError("Job fixtures require local storage")
    now = time.time()
    job = {"id": str(uuid4()), "owner": owner, "status": "queued", "created": now,
           "updated": now, "progress": 0, "phase": "Queued", "scenario": scenario,
           "cancel": False, "expires": expires_at(now)}
    with store.db() as db:
        db.execute("INSERT INTO jobs VALUES (?,?,?)", (job["id"], owner, json.dumps(job)))
    return job
