"""Reuse identical runs: a completed run's result serves a later run with the same scenario and frozen inputs.

The key is a digest of the scenario; the frozen input bundle without its own ID and hashes (the road network by its
net_sha256, the network metadata, edges and gates, the observation and traffic snapshots, and the demand population
and crew plans); the model version with a digest of the code that turns those inputs into a result; the SUMO engine
version; and the result schema version. A fresh run fetches new observations and a new traffic snapshot, so it never
matches an earlier run; variants of one frozen parent with the same changes do. The jobs table holds
reuse:<key> -> the run and artifact attempt whose result to copy, until that run's own record expires. A reused
result says so in its reused_result field (provenance per traffic-simulator/AGENTS.md); the new run keeps its own
job record, owner and artifacts.
"""
import hashlib
import logging
import sqlite3
import time
import zlib
from functools import cache

import sumolib
from botocore.exceptions import BotoCoreError, ClientError

from traffic_simulator import settings
from traffic_simulator.input_bundles import MODEL_VERSION
from traffic_simulator.persistence import digest, expires_at

# The schema version of every result the worker writes.
RESULT_SCHEMA_VERSION = 4
# The code that turns frozen inputs into a result. Any change to it gives new keys, so no earlier result is reused.
MODEL_CODE = ("simulation.py", "demand.py", "input_bundles.py", "worker.py")
# Bundle fields that identify the bundle rather than its inputs; metadata.net_sha256 stands for network_xml.
OWN_FIELDS = ("bundle_id", "sha256", "network_xml")
BASIS = ("Simulated outcomes reused from an identical earlier run: the same scenario, frozen inputs (road network, "
         "observation and traffic snapshots, demand and crew plans), model, SUMO engine and result schema. "
         "No new simulation was run.")
logger = logging.getLogger(__name__)


@cache
def model_code() -> str:
    """Return the SHA-256 of the simulator code that turns frozen inputs into a result (the MODEL_CODE files)."""
    return hashlib.sha256(b"".join((settings.PACKAGE / name).read_bytes() for name in MODEL_CODE)).hexdigest()


def key(bundle: dict, scenario) -> str:
    """Return the identical-run key of a scenario simulated on a validated frozen input bundle."""
    inputs = {name: value for name, value in bundle.items() if name not in OWN_FIELDS}
    return digest({"scenario": scenario.model_dump(), "inputs": inputs, "model_version": MODEL_VERSION,
                   "model_code": model_code(), "engine_version": sumolib.__version__,
                   "schema_version": RESULT_SCHEMA_VERSION})


def find(store, run_key: str, now: float | None = None) -> dict | None:
    """Return the result of a completed identical run, marked as reused, or None when there is none to reuse.

    An entry past its run's record lifetime, or whose result cannot be read, is a miss: the run is simulated again.
    """
    entry = store.get(f"reuse:{run_key}")
    if not entry or entry.get("expires", 0) <= (time.time() if now is None else now):
        return None
    try:
        result = store.artifact(entry["run_id"], "result", attempt=entry["artifact_attempt"])
    except (BotoCoreError, ClientError, OSError, EOFError, ValueError, zlib.error) as exc:
        logger.warning("Result of identical run %s is unreadable (%s); simulating again", entry["run_id"],
                       type(exc).__name__)
        return None
    if not isinstance(result, dict) or not isinstance(result.get("policies"), list):
        logger.warning("Result of identical run %s holds no comparison; simulating again", entry["run_id"])
        return None
    logger.info("Reusing the result of identical run %s", entry["run_id"])
    result["reused_result"] = {"simulated_at": entry["simulated_at"], "basis": BASIS}
    return result


def remember(store, run_key: str, job: dict, attempt: str) -> None:
    """Record a completed, simulated run as the result to reuse for its key, until the run's own record expires."""
    try:
        store.put_record({"id": f"reuse:{run_key}", "kind": "result_reuse", "run_id": job["id"],
                          "artifact_attempt": attempt, "simulated_at": time.time(),
                          "expires": job.get("expires") or expires_at()})
    except (BotoCoreError, ClientError, OSError, sqlite3.Error) as exc:
        # Reuse only saves work; the run itself is already complete.
        logger.warning("Run %s was not recorded for reuse (%s)", job["id"], type(exc).__name__)
