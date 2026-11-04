"""Immutable input snapshots for controlled departure-window and cohort variants.

A frozen road network (freeze_network) is sealed with the SHA-256 digest of its bundle and checked
(verify_sealed) before a run or a prepared draft rebuilds it (thaw_network).
"""
import copy
import hashlib
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

import sumolib
from shapely.geometry import LineString

from traffic_simulator import settings
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import digest
from traffic_simulator.simulation import demand_for

BUNDLE_VERSION = 1
MODEL_VERSION = "corridor-policies-v1"


def freeze_network(prepared):
    """Return a prepared road network's serializable fields: its SUMO XML, public GeoJSON, metadata and IDs.

    The field names are part of every sealed bundle's digest, so saved bundles verify only while they stay the same.
    """
    return {"network_xml": prepared[0].read_text(), "network": prepared[6], "metadata": prepared[7],
            "edge_ids": [e.getID() for e in prepared[2]], "gate_ids": [g.getID() for g in prepared[4]],
            "fire_xy": list(prepared[5])}


def verify_sealed(bundle, **versions):
    """Refuse a sealed bundle whose versions (and SUMO engine), digest or network checksum do not match.

    versions names each bundle field and the value it must hold, for example bundle_version=1. Raises 409
    BUNDLE_VERSION_UNSUPPORTED, BUNDLE_INTEGRITY_ERROR or NETWORK_INTEGRITY_ERROR.
    """
    expected = {**versions, "engine_version": sumolib.__version__}
    if any(bundle.get(field) != value for field, value in expected.items()):
        raise IntegrationError("BUNDLE_VERSION_UNSUPPORTED", "Parent inputs use an incompatible model or engine.", 409)
    if bundle.get("sha256") != digest({k: v for k, v in bundle.items() if k != "sha256"}):
        raise IntegrationError("BUNDLE_INTEGRITY_ERROR", "Frozen parent inputs failed their integrity check.", 409)
    if hashlib.sha256(bundle["network_xml"].encode()).hexdigest() != bundle["metadata"]["net_sha256"]:
        raise IntegrationError("NETWORK_INTEGRITY_ERROR", "Frozen network failed its integrity check.", 409)


def thaw_network(content, prefix):
    """Rebuild a frozen road network in a new folder under DATA_DIR and return its prepared-network tuple.

    The folder (named prefix plus a random suffix) is removed again when the network cannot be loaded; the caller
    removes it after a successful run.
    """
    folder = Path(tempfile.mkdtemp(prefix=prefix, dir=settings.data_dir()))
    try:
        netfile = folder / "network.net.xml"
        netfile.write_text(content["network_xml"])
        net = sumolib.net.readNet(str(netfile), withInternal=False)
        edges = [net.getEdge(identifier) for identifier in content["edge_ids"]]
        lines = {e.getID(): LineString(e.getShape()) for e in edges if len(e.getShape()) >= 2}
        gates = [net.getEdge(identifier) for identifier in content["gate_ids"]]
        return (netfile, net, edges, lines, gates, content["fire_xy"],
                copy.deepcopy(content["network"]), copy.deepcopy(content["metadata"]))
    except BaseException:
        shutil.rmtree(folder)
        raise


def freeze_inputs(scenario, prepared, sources, traffic):
    """Freeze a run's complete simulation inputs (network, sources, traffic, demand) as a bundle."""
    # Precompute the entire bounded population once. The existing seeded sampling
    # sequence means every smaller cohort is exactly a prefix, with identical crews.
    population_scenario = scenario.model_copy(update={"vehicles": 1500})
    population, crews = demand_for(population_scenario, *prepared[1:6])
    content = {
        "bundle_version": BUNDLE_VERSION, "model_version": MODEL_VERSION,
        "engine_version": sumolib.__version__, "bundle_id": str(uuid4()), **freeze_network(prepared),
        "sources": sources, "traffic": traffic,
        "population": population, "crews": crews, "scenario": scenario.model_dump(),
    }
    return {**content, "sha256": digest(content)}


def validate_bundle(bundle):
    """Refuse a frozen input bundle with another model or engine version or a bad digest."""
    verify_sealed(bundle, bundle_version=BUNDLE_VERSION, model_version=MODEL_VERSION)


def demand_for_bundle(bundle, scenario):
    """Derive a variant's demand from a frozen bundle when only vehicles or departures change."""
    validate_bundle(bundle)
    original = bundle["scenario"]
    allowed = {"vehicles", "departure_minutes"}
    if any(value != original.get(key) for key, value in scenario.model_dump().items() if key not in allowed):
        raise IntegrationError("UNSUPPORTED_VARIANT", "Controlled variants may change only vehicle count or departure window.")
    civilians = copy.deepcopy(bundle["population"][:scenario.vehicles])
    for civilian in civilians:
        civilian["depart"] = round(civilian["departure_fraction"] * scenario.departure_minutes * 60, 1)
    return civilians, copy.deepcopy(bundle["crews"])


def restore(bundle):
    """Rebuild the SUMO network and inputs from a validated frozen bundle in a temporary folder."""
    validate_bundle(bundle)
    return thaw_network(bundle, "frozen-network-")
