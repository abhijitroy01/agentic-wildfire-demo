import gzip
import hashlib
import io
import json
import os
import sys
import time
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

import pytest
from botocore.exceptions import ClientError
from traffic_simulator import network, network_cache, settings
from traffic_simulator.models import Scenario
from traffic_simulator.simulation import simulate
from traffic_simulator.timings import Timings


def source_manifest():
    """Exact immutable-source stand-in; native conversion tests never query a public provider."""
    return {"publisher": "Geofabrik", "snapshot_timestamp": "2026-10-25T21:21:21Z",
            "recipe": "fixture-complete-roads", "osmium_version": "1.19.1", "study_bounds": [],
            "sources": [{"region_id": "us/california", "url": "https://download.geofabrik.de/fixture-261025.osm.pbf",
                         "bytes": 100, "md5": "a"*32, "sha256": "b"*64}]}


def test_tool_failure_keeps_its_output_in_the_server_log(caplog):
    script = "import sys; print('Error: /srv/private/scratch/roads.osm.xml is unreadable'); sys.exit(3)"
    with pytest.raises(RuntimeError) as error:
        network.command([sys.executable, "-c", script], lambda: None)
    assert "could not build the road network" in str(error.value)
    assert "/srv/private" not in str(error.value)
    assert "/srv/private/scratch/roads.osm.xml is unreadable" in caplog.text


def test_cached_network_is_reused_only_when_its_checksum_matches(tmp_path):
    """A cache entry is reused only while its network matches the recorded SHA-256; bad metadata is a miss."""
    netfile, meta = tmp_path / "network.net.xml", tmp_path / "metadata.json"
    assert network_cache.cached_network(netfile, meta) is None
    netfile.write_text("<net/>")
    meta.write_text(json.dumps({"net_sha256": hashlib.sha256(b"<net/>").hexdigest(), "cache_key": "k"}))
    assert network_cache.cached_network(netfile, meta)["cache_key"] == "k"
    netfile.write_text("<net>tampered</net>")
    assert network_cache.cached_network(netfile, meta) is None
    meta.write_text('{"net_sha256": ')
    assert network_cache.cached_network(netfile, meta) is None
    meta.write_text("{}")  # earlier cache entries without a recorded checksum stay usable
    assert network_cache.cached_network(netfile, meta) == {}


class Published(Exception):
    """Raised instead of parsing the network so a test can inspect the cache right after publication."""


def stop_at_network_load(monkeypatch):
    """Make loading a SUMO network raise Published with its path, so a test can inspect the cache at that point."""
    def parse(path, **_):
        """Raise Published instead of parsing the network file."""
        raise Published(path)

    monkeypatch.setattr(network.sumolib.net, "readNet", parse)


def offline_providers(monkeypatch, highway_coordinates, network_bytes=None):
    """Replace the immutable-source extraction and netconvert with offline fakes; return the downloads.

    The netconvert stand-in writes network_bytes when given (a real SUMO network), else a numbered placeholder.
    """
    downloads = []
    osm = Element("osm", version="0.6")
    for identifier, (longitude, latitude) in enumerate(highway_coordinates, 1):
        SubElement(osm, "node", id=str(identifier), lon=str(longitude), lat=str(latitude))
    way = SubElement(osm, "way", id="191")
    for identifier in range(1, len(highway_coordinates)+1):
        SubElement(way, "nd", ref=str(identifier))
    SubElement(way, "tag", k="highway", v="primary")
    SubElement(way, "tag", k="ref", v="CA 191")

    def extract(area, source, directory, run, progress, check_cancel):
        """Record one extraction of the exact fixture corridor snapshot."""
        downloads.append(source)
        raw = tostring(osm)
        return raw, hashlib.sha256(raw).hexdigest()

    def convert(args, check_cancel):
        """Write the network netconvert would, into the private scratch folder it was given."""
        output = Path(args[args.index("--output-file") + 1])
        assert output.parent.name.startswith("building-")  # private per-call scratch folder
        output.write_bytes(network_bytes or f"<net build='{len(downloads)}'/>".encode())
        return "conversion log"

    monkeypatch.setattr(network.osm_extracts, "plan", lambda *_: source_manifest())
    monkeypatch.setattr(network.osm_extracts, "extract", extract)
    monkeypatch.setattr(network, "command", convert)
    monkeypatch.setattr(network, "binary", lambda name: name)
    return downloads


def test_selected_route_is_reported_only_after_the_snapshot_and_gates_are_validated(monkeypatch, grid):
    """The callback receives a proven selection from the downloaded snapshot; refused routes never do."""
    offline_providers(monkeypatch, [[-121.6219, 39.7496], [-121.6219, 39.7696]], grid[0].read_bytes())
    events = []
    prepare_area = network.area_network
    monkeypatch.setattr(network, "area_network", lambda *args, **kwargs: events.append("network") or prepare_area(
        *args, **kwargs))
    scenario = Scenario(radius_km=3, hazard_radius_m=0, hazard_growth_m_min=0)
    prepared = network.prepare_network(scenario, lambda *_: None, lambda: None,
                                       route_selected=lambda route: events.append(route["route_ref"]))
    assert prepared[4] and events == ["network", "CA 191"]
    events.clear()
    with pytest.raises(ValueError, match="does not intersect this area"):
        network.prepare_network(Scenario(highway="US 99"), lambda *_: None, lambda: None, route_selected=events.append)
    assert events == ["network"]


def test_network_cache_builds_in_private_scratch_and_rebuilds_a_corrupted_network(monkeypatch):
    """A network is built in private scratch, reused while its checksum matches, and rebuilt when it does not."""
    downloads = offline_providers(monkeypatch, [[-121.63, 39.75], [-121.61, 39.75]])
    stop_at_network_load(monkeypatch)

    def prepare():
        """Prepare the default area and return its cache entry, checking the entry holds only network and metadata."""
        with pytest.raises(Published) as published:
            network.prepare_network(Scenario(), lambda *_: None, lambda: None)
        folder = Path(published.value.args[0]).parent
        assert sorted(p.name for p in folder.iterdir()) == ["metadata.json", "network.net.xml"]
        metadata = json.loads((folder / "metadata.json").read_text())
        assert metadata["net_sha256"] == hashlib.sha256((folder / "network.net.xml").read_bytes()).hexdigest()
        return folder

    folder = prepare()
    assert prepare() == folder and len(downloads) == 1
    (folder / "network.net.xml").write_text("<net>partially written")
    prepare()
    assert len(downloads) == 2


def test_network_preparation_times_each_phase_and_marks_a_cache_hit(monkeypatch, grid):
    """A build times discovery, the complete-way extraction, netconvert and loading; a reuse times its cache hit instead."""
    # The selected highway runs north-south through the grid centre (the default scenario's fire coordinate).
    downloads = offline_providers(monkeypatch, [[-121.6219, 39.7496], [-121.6219, 39.7696]])
    monkeypatch.setattr(network.sumolib.net, "readNet", lambda path, **_: grid[1])
    scenario = Scenario(hazard_radius_m=0)
    built, reused = Timings(), Timings()
    assert network.prepare_network(scenario, lambda *_: None, lambda: None, timings=built)[4], "an exit gate"
    network.prepare_network(scenario, lambda *_: None, lambda: None, timings=reused)
    assert len(downloads) == 1
    assert set(built.public()) == {"discovery", "osm_extract", "netconvert", "network_load"}
    assert set(reused.public()) == {"discovery", "network_cache_hit", "network_load"}
    assert all(seconds >= 0 for seconds in [*built.public().values(), *reused.public().values()])


def test_network_cache_key_covers_the_area_conversion_and_sumo_version(monkeypatch):
    """The key changes with the area, the netconvert options or the SUMO version, so an upgrade never reuses."""
    key = network.network_key(Scenario())
    assert key == network.network_key(Scenario(vehicles=99, highway="US 99")), "Only the area selects a network."
    assert key != network.network_key(Scenario(radius_km=7))
    with monkeypatch.context() as patch:
        patch.setattr(network, "NETCONVERT_OPTIONS", (*network.NETCONVERT_OPTIONS, "--no-internal-links", "true"))
        assert network.network_key(Scenario()) != key
    with monkeypatch.context() as patch:
        patch.setattr(network.sumolib, "__version__", "99.0.0")
        assert network.network_key(Scenario()) != key
    assert network.network_key(Scenario()) == key


def cache_entry(root, name, size, used):
    """Write one local network cache entry of `size` network bytes whose last use is the time `used`."""
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "network.net.xml").write_bytes(b"x" * size)
    (folder / "metadata.json").write_text("{}")
    os.utime(folder / "metadata.json", (used, used))
    return folder


def test_local_network_cache_evicts_least_recently_used_by_count_and_size(monkeypatch):
    """Eviction removes the least recently used networks past either bound; reuse renews; the one in use stays."""
    monkeypatch.setenv("CACHED_NETWORKS_PER_WORKER", "3")
    monkeypatch.setenv("CACHED_NETWORK_MIB_PER_WORKER", "1")
    root = settings.data_dir("networks")
    for index, name in enumerate("abcd"):
        cache_entry(root, name, 300_000, 1_000_000 + index)
    assert network_cache.reuse(root / "a") == {}, "Reusing the oldest network marks it as just used."
    network_cache.evict(keep="d")
    assert sorted(p.name for p in root.iterdir()) == ["a", "c", "d"], "Four networks: the oldest unused one goes."
    cache_entry(root, "e", 700_000, time.time() + 10)
    network_cache.evict(keep="e")
    assert sorted(p.name for p in root.iterdir()) == ["a", "e"], "Over 1 MiB: the two oldest go."
    cache_entry(root, "f", 2_000_000, time.time() + 20)
    network_cache.evict(keep="f")
    assert sorted(p.name for p in root.iterdir()) == ["f"], "The network in use stays, even alone over the bound."


class SharedBucket:
    """Stub S3 client for the shared network cache: put_object and get_object against an in-memory bucket."""

    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body, **_):
        """Store one object's bytes."""
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket, Key):
        """Return one object's body, or raise S3's NoSuchKey error."""
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}


@pytest.fixture
def shared_bucket(monkeypatch):
    """Run the network cache in AWS mode against a stub S3 client, with the worker settings the stack sets."""
    bucket = SharedBucket()
    monkeypatch.setattr(settings, "CLOUD", True)
    monkeypatch.setattr(settings, "BUCKET", "artifacts-bucket")
    monkeypatch.setattr(network_cache, "client", lambda: bucket)
    for variable in ("MAX_NETWORK_EDGES", "OVERPASS_RESPONSE_MAX_MB", "CACHED_NETWORKS_PER_WORKER",
                     "CACHED_NETWORK_MIB_PER_WORKER"):
        monkeypatch.setenv(variable, str(settings.operational_settings()[variable]["value"]))
    return bucket


def test_aws_workers_share_built_networks_and_reuse_them_only_after_a_hash_check(monkeypatch, tmp_path,
                                                                                  shared_bucket):
    """A full miss builds and uploads under network-cache/; another worker downloads it; a bad copy is rebuilt."""
    downloads = offline_providers(monkeypatch, [[-121.63, 39.75], [-121.61, 39.75]])
    stop_at_network_load(monkeypatch)
    key = network.network_key(Scenario(), source_manifest())
    shared_network = ("artifacts-bucket", f"network-cache/{key}/network.net.xml.gz")
    shared_metadata = ("artifacts-bucket", f"network-cache/{key}/metadata.json")

    def prepare(worker):
        """Prepare the default area on a worker with its own scratch disk; return the network and its phases."""
        monkeypatch.setattr(settings, "DATA", tmp_path / worker)
        timings = Timings()
        with pytest.raises(Published) as published:
            network.prepare_network(Scenario(), lambda *_: None, lambda: None, timings=timings)
        return Path(published.value.args[0]), set(timings.public())

    built, phases = prepare("first-worker")
    assert len(downloads) == 1 and phases == {"discovery", "osm_extract", "netconvert", "network_cache_upload"}
    assert set(shared_bucket.objects) == {shared_network, shared_metadata}
    assert gzip.decompress(shared_bucket.objects[shared_network]) == built.read_bytes()
    assert json.loads(shared_bucket.objects[shared_metadata])["net_sha256"] == hashlib.sha256(
        built.read_bytes()).hexdigest()
    # A new worker (empty scratch disk) copies the shared network instead of downloading and converting it.
    copied, phases = prepare("second-worker")
    assert len(downloads) == 1 and phases == {"discovery", "network_cache_download"}
    assert copied.read_bytes() == built.read_bytes()
    assert prepare("second-worker")[1] == {"discovery", "network_cache_hit"}
    # A shared copy that fails its SHA-256 check, or has no metadata, is a miss: the worker builds and re-shares.
    shared_bucket.objects[shared_network] = gzip.compress(b"<net>tampered</net>")
    rebuilt, phases = prepare("third-worker")
    assert len(downloads) == 2 and "osm_extract" in phases
    assert gzip.decompress(shared_bucket.objects[shared_network]) == rebuilt.read_bytes()
    del shared_bucket.objects[shared_metadata]
    prepare("fourth-worker")
    assert len(downloads) == 3 and shared_metadata in shared_bucket.objects


def test_local_mode_keeps_networks_on_the_filesystem_only(monkeypatch):
    """Without AWS storage the cache never creates an S3 client; a second preparation reuses the local network."""
    downloads = offline_providers(monkeypatch, [[-121.63, 39.75], [-121.61, 39.75]])
    monkeypatch.setattr(network_cache, "client", lambda: pytest.fail("Local mode must not use S3."))
    stop_at_network_load(monkeypatch)
    for _ in range(2):
        with pytest.raises(Published):
            network.prepare_network(Scenario(), lambda *_: None, lambda: None)
    assert len(downloads) == 1


@pytest.mark.integration
def test_a_cached_network_gives_byte_identical_results_to_a_freshly_built_one(monkeypatch, tmp_path, grid,
                                                                              shared_bucket):
    """Simulation results must not change: runs on the shared and local cached copies equal the fresh build's."""
    # netconvert's stand-in writes the real test grid, so every copy is a real SUMO network; the highway runs
    # north-south through the grid centre, the scenario's fire coordinate.
    downloads = offline_providers(monkeypatch, [[-121.6219, 39.7496], [-121.6219, 39.7696]], grid[0].read_bytes())
    scenario = Scenario(vehicles=20, crews=0, radius_km=3, duration_minutes=5, departure_minutes=1,
                        hazard_radius_m=0, hazard_growth_m_min=0, policies=["baseline", "phased"])

    def run(worker):
        """Prepare the area on one worker's scratch disk and simulate it; return the network and the result bytes."""
        monkeypatch.setattr(settings, "DATA", tmp_path / worker)
        prepared = network.prepare_network(scenario, lambda *_: None, lambda: None)
        result = simulate(scenario, prepared, lambda *_: None, lambda: None, parallelism=1)
        # Encoded as the job store writes the result artifact.
        return prepared, json.dumps(result, separators=(",", ":"), allow_nan=False).encode()

    built, built_result = run("first-worker")
    shared, shared_result = run("second-worker")
    reused, reused_result = run("second-worker")
    assert len(downloads) == 1, "The second worker used the shared copy, then its own local copy."
    assert shared[0].read_bytes() == reused[0].read_bytes() == built[0].read_bytes() == grid[0].read_bytes()
    assert shared[7] == reused[7] == built[7]
    assert shared_result == reused_result == built_result
    assert json.loads(built_result)["policies"][0]["summary"]["exited_vehicles"] > 0
