"""Fixed canonical road HTTP operations in an owned process; source cache authority stays in its parent."""
import re
from pathlib import Path

from traffic_simulator import osm_extracts


def index(_payload, progress=None):
    return osm_extracts.fetch_index(lambda: None)


def resolve(payload, progress=None):
    with osm_extracts.client(lambda: None) as http:
        return osm_extracts.resolve(payload["region"], http, lambda: None)


def download(payload, progress=None):
    target, root = Path(payload["target"]), Path(payload["scratch_root"])
    if (not root.is_absolute() or not target.is_absolute()
            or not target.resolve().is_relative_to(root.resolve() / "osm-extracts")
            or not re.fullmatch(r"[a-f0-9]{32}\.part", target.name) or not target.parent.is_dir()):
        raise osm_extracts.invalid("The road source download does not have an owned partial target.")
    received = osm_extracts.download_direct(payload["source"], target, lambda: None, progress or (lambda *_: None))
    return {"bytes": received}
