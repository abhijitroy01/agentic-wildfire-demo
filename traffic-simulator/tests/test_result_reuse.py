"""Identical runs reuse a completed run's result, which is byte for byte what simulating again gives."""
import copy
import hashlib
import json
import threading
import uuid
from unittest.mock import Mock

import pytest
import sumolib
from traffic_simulator import input_bundles, result_reuse, worker
from traffic_simulator.models import Scenario
from traffic_simulator.persistence import digest
from traffic_simulator.simulation import simulate
from traffic_simulator.store import Store
from traffic_simulator.testing import create_job

BUNDLE_ID = "00000000-0000-4000-8000-000000000001"


def artifact_bytes(value):
    """Encode a value as the job store writes an artifact (Store.artifact), for byte comparison."""
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode()


def frozen_bundle(**changes):
    """Return a small frozen input bundle shaped like input_bundles.freeze_inputs's, with `changes` applied."""
    content = {"bundle_version": input_bundles.BUNDLE_VERSION, "model_version": input_bundles.MODEL_VERSION,
               "engine_version": sumolib.__version__, "bundle_id": BUNDLE_ID, "network_xml": "<net/>",
               "network": {"type": "FeatureCollection", "features": []},
               "metadata": {"net_sha256": hashlib.sha256(b"<net/>").hexdigest(), "retrieved_at": 1.0},
               "edge_ids": ["a", "b"], "gate_ids": ["b"], "fire_xy": [0.0, 0.0],
               "sources": {"sources": [{"name": "Fixture feed", "retrieved_at": 1.0}]}, "traffic": None,
               "population": [{"id": "evac-0", "departure_fraction": 0.5}], "crews": [],
               "scenario": Scenario().model_dump(), **changes}
    return {**content, "sha256": digest(content)}


def test_the_reuse_key_covers_the_scenario_frozen_inputs_and_versions(monkeypatch):
    """Only the same scenario on the same frozen inputs, model, engine and result schema gives the same key."""
    scenario = Scenario()
    run_key = result_reuse.key(frozen_bundle(), scenario)
    # The bundle's own ID and hash are not inputs; net_sha256 in its metadata stands for the network XML.
    assert result_reuse.key(frozen_bundle(bundle_id="another-bundle"), scenario) == run_key
    assert result_reuse.key({**frozen_bundle(), "network_xml": "<net/>\n"}, scenario) == run_key
    for changed in ({"sources": {"sources": []}}, {"traffic": {"status": "applied", "retrieved_at": "later"}},
                    {"metadata": {"net_sha256": "0" * 64, "retrieved_at": 1.0}}, {"gate_ids": ["a"]},
                    {"population": [{"id": "evac-0", "departure_fraction": 0.25}]}):
        assert result_reuse.key(frozen_bundle(**changed), scenario) != run_key, changed
    assert result_reuse.key(frozen_bundle(), scenario.model_copy(update={"vehicles": 401})) != run_key
    for name, value in (("MODEL_VERSION", "corridor-policies-v2"), ("RESULT_SCHEMA_VERSION", 5),
                        ("model_code", lambda: "edited simulator code")):
        with monkeypatch.context() as patch:
            patch.setattr(result_reuse, name, value)
            assert result_reuse.key(frozen_bundle(), scenario) != run_key, name
    with monkeypatch.context() as patch:
        patch.setattr(result_reuse.sumolib, "__version__", "99.0.0")
        assert result_reuse.key(frozen_bundle(), scenario) != run_key
    assert result_reuse.key(frozen_bundle(), scenario) == run_key


def test_the_model_code_digest_covers_the_simulation_demand_bundle_and_worker_code():
    """Shared demand code participates in result identity after its extraction from simulation."""
    files = [result_reuse.settings.PACKAGE / name
             for name in ("simulation.py", "demand.py", "input_bundles.py", "worker.py")]
    assert result_reuse.model_code() == hashlib.sha256(b"".join(f.read_bytes() for f in files)).hexdigest()


def test_changing_shared_demand_invalidates_completed_result_reuse(monkeypatch, tmp_path):
    for name in result_reuse.MODEL_CODE:
        (tmp_path/name).write_bytes((result_reuse.settings.PACKAGE/name).read_bytes())
    original = result_reuse.model_code.__wrapped__()
    monkeypatch.setattr(result_reuse.settings, "PACKAGE", tmp_path)
    assert result_reuse.model_code.__wrapped__() == original
    with (tmp_path/"demand.py").open("a") as file:
        file.write("\n# changed demand behavior\n")
    assert result_reuse.model_code.__wrapped__() != original


def test_only_a_live_readable_recorded_result_is_reused(tmp_path, caplog):
    """An entry serves until its run's record expires, and only while its result reads as a comparison."""
    store = Store(tmp_path)
    job = create_job(store, "alice", Scenario().model_dump())
    attempt = str(uuid.uuid4())
    store.artifact(job["id"], "result", {"policies": [], "engine": "Eclipse SUMO"}, attempt=attempt)
    assert result_reuse.find(store, "k") is None
    result_reuse.remember(store, "k", job, attempt)
    found = result_reuse.find(store, "k")
    assert found["policies"] == [] and found["reused_result"]["basis"] == result_reuse.BASIS
    assert found["reused_result"]["simulated_at"] >= job["created"]
    assert result_reuse.find(store, "k", now=job["expires"]) is None, "it expires with the run's own record"
    # The reuse record never shows as a job of any owner, and the local worker never picks it up.
    assert [j["id"] for j in store.list("alice")] == [job["id"]] == [j["id"] for j in store.pending()]
    (tmp_path / "runs" / job["id"] / "attempts" / attempt / "result.json.gz").write_bytes(b"not gzip")
    assert result_reuse.find(store, "k") is None and "is unreadable (BadGzipFile); simulating again" in caplog.text
    store.artifact(job["id"], "result", {"no": "comparison"}, attempt=attempt)
    assert result_reuse.find(store, "k") is None


def test_cloud_reuse_records_are_one_put_without_job_index_attributes():
    """In AWS mode a reuse record is a single PutItem with the run's TTL and no owner or created attribute."""
    store = Store.__new__(Store)
    store.cloud, store.table = True, Mock()
    result_reuse.remember(store, "k", {"id": "run", "expires": 1_790_604_800}, "attempt")
    item = store.table.put_item.call_args.kwargs["Item"]
    assert (item["id"], item["kind"], item["run_id"], item["artifact_attempt"], item["expires"]) == (
        "reuse:k", "result_reuse", "run", "attempt", 1_790_604_800)
    assert not {"owner", "created", "status", "outbox_pending"} & item.keys()


@pytest.fixture
def fixed_inputs(monkeypatch, grid):
    """Give every fresh run the same road network, observations and bundle ID; return the simulate() calls."""
    metadata = {"net_sha256": hashlib.sha256(grid[0].read_bytes()).hexdigest(), "selected_route": {"districts": []}}
    observed = {"sources": [{"name": "Fixture feed", "status": "available", "records": []}]}
    monkeypatch.setattr(input_bundles, "uuid4", lambda: BUNDLE_ID)

    def prepare(scenario, progress, check, *, timings, route_selected=None):
        """Return the grid network with fixed metadata."""
        return (*grid[:7], copy.deepcopy(metadata))

    async def fetch(*_):
        """Return the fixed observations."""
        return copy.deepcopy(observed)

    simulated, real = [], worker.simulate

    def counted(*args, **kwargs):
        """Simulate for real, counting the call."""
        simulated.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(worker, "prepare_network", prepare)
    monkeypatch.setattr(worker, "fetch_sources", fetch)
    monkeypatch.setattr(worker, "simulate", counted)
    return simulated


def run(store, owner, scenario, **values):
    """Queue and execute one run of `owner` (values add job fields, such as a parent); return its record and result."""
    job = create_job(store, owner, scenario.model_dump())
    if values:
        store.update(job["id"], **values)
    assert worker.execute(store, job, threading.Event())
    record = store.get(job["id"])
    assert record["status"] == "completed", record.get("error")
    return record, store.artifact(job["id"], "result")


@pytest.mark.integration
def test_an_identical_run_reuses_the_result_byte_for_byte_and_says_so(tmp_path, fixed_inputs):
    """A second identical run, of another owner, completes from the first one's result without simulating."""
    scenario = Scenario(vehicles=20, crews=1, radius_km=3, duration_minutes=5, departure_minutes=1,
                        hazard_radius_m=0, hazard_growth_m_min=0, policies=["baseline", "phased"])
    store = Store(tmp_path / "shared")
    first, first_result = run(store, "alice", scenario)
    second, second_result = run(store, "bob", scenario)
    assert len(fixed_inputs) == 1, "the identical second run was not simulated"
    assert (first["phase"], second["phase"], second["owner"]) == ("Comparison ready", worker.REUSED_PHASE, "bob")
    assert "result_reuse" in second["timings"] and not {"simulation", "policy_baseline"} & second["timings"].keys()
    provenance = second_result.pop("reused_result")
    assert provenance["basis"] == result_reuse.BASIS and provenance["simulated_at"] >= first["created"]
    assert store.artifact(second["id"], "manifest")["reused_result"] == provenance
    assert second["summaries"] == first["summaries"]
    # Simulating the same inputs again, with nothing to reuse, gives exactly the reused bytes.
    _, fresh_result = run(Store(tmp_path / "fresh"), "carol", scenario)
    assert len(fixed_inputs) == 2
    assert artifact_bytes(second_result) == artifact_bytes(fresh_result) == artifact_bytes(first_result)
    # Another scenario on the same inputs is simulated.
    run(store, "alice", scenario.model_copy(update={"vehicles": 25}))
    assert len(fixed_inputs) == 3


@pytest.mark.integration
def test_live_traffic_runs_reuse_a_result_only_with_the_same_frozen_snapshot(tmp_path, monkeypatch, fixed_inputs):
    """A new TomTom capture is a new input; a variant on its parent's frozen snapshot reuses the parent's result."""
    scenario = Scenario(vehicles=20, crews=0, radius_km=3, duration_minutes=5, departure_minutes=1,
                        hazard_radius_m=0, hazard_growth_m_min=0, policies=["baseline"],
                        traffic_mode="provider_speed_limits")
    captured = iter(["2026-10-02T17:00:00+00:00", "2026-10-02T17:05:00+00:00"])

    async def capture(*_, **__):
        """Return a new TomTom snapshot per fresh run: the same speed on the gate road, captured later."""
        return {"mode": "provider_speed_limits", "provider": "TomTom", "status": "applied",
                "retrieved_at": next(captured), "requests": [], "segments": [],
                "matches": [{"edge_id": "n1_2-n0_2", "speed_limit_m_s": 6.0}], "excluded": [],
                "matched_edges": 1, "highway_edges": 1}

    monkeypatch.setattr(worker, "capture_speeds", capture)
    store = Store(tmp_path)
    parent, parent_result = run(store, "alice", scenario)
    again, _ = run(store, "alice", scenario)
    assert len(fixed_inputs) == 2 and again["phase"] == "Comparison ready", "a new snapshot is simulated"
    variant, variant_result = run(store, "alice", scenario, parent_run_id=parent["id"], changes={})
    assert len(fixed_inputs) == 2 and variant["phase"] == worker.REUSED_PHASE
    assert variant_result.pop("reused_result")["basis"] == result_reuse.BASIS
    assert variant_result["traffic"] == parent_result["traffic"]
    assert {**variant_result, "parent_run_id": None} == parent_result
    # Simulating the variant's frozen inputs directly gives the policies it reused.
    bundle = store.artifact(parent["id"], "bundle")
    direct = simulate(scenario, input_bundles.restore(bundle), lambda *_: None, lambda: None,
                      traffic=bundle["traffic"], demand=input_bundles.demand_for_bundle(bundle, scenario),
                      parallelism=1)
    assert artifact_bytes(direct["policies"]) == artifact_bytes(variant_result["policies"])
