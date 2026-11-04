"""Immutable regional files are verified and published atomically under a bounded source cache."""
import hashlib
import io
import json
import threading

import pytest
from botocore.exceptions import ClientError
from traffic_simulator import osm_source_cache, settings
from traffic_simulator.errors import IntegrationError


def declared(content=b"source-pbf-fixture", name="washington"):
    return {"region_id": "us/"+name, "url": f"https://download.geofabrik.de/us/{name}-261025.osm.pbf",
            "bytes": len(content), "md5": hashlib.md5(content, usedforsecurity=False).hexdigest(), "etag": None}


def header(_):
    return {"header": {"with_history": False, "option": {
        "osmosis_replication_timestamp": "2026-10-25T21:21:21Z"}}}


def test_cold_download_publishes_exact_hash_then_warm_uses_verified_same_bytes():
    content, calls = b"source-pbf-fixture", []
    source = declared(content)

    def download(source, target, check, progress):
        check()
        calls.append(source["url"])
        target.write_bytes(content)

    target, manifest = osm_source_cache.obtain(source, download, header, lambda: None, lambda *_: None)
    assert manifest["sha256"] == hashlib.sha256(content).hexdigest()
    assert json.loads((target.parent/"manifest.json").read_text()) == manifest
    warm, again = osm_source_cache.obtain(source, download, header, lambda: None, lambda *_: None)
    assert warm == target and again == manifest and len(calls) == 1
    target.write_bytes(b"tampered-content!")
    osm_source_cache.obtain(source, download, header, lambda: None, lambda *_: None)
    assert len(calls) == 2 and target.read_bytes() == content


def test_bad_source_bytes_never_publish_metadata_or_replace_another_snapshot():
    source = declared()
    with pytest.raises(IntegrationError, match="verification"):
        osm_source_cache.obtain(source, lambda source, path, *_: path.write_bytes(b"wrong"),
                               header, lambda: None, lambda *_: None)
    location = osm_source_cache.folder(source)
    assert not (location/"manifest.json").exists() and not (location/"source.osm.pbf").exists()
    assert not list(location.glob("*.part"))


def test_metadata_cannot_change_checksum_under_immutable_selection():
    source = declared()
    value = {**source, "sha256": "a"*64, "snapshot_timestamp": "2026-10-25T21:21:21Z", "header": header(None)["header"]}
    assert osm_source_cache.valid_manifest(value, source)
    assert not osm_source_cache.valid_manifest({**value, "md5": "b"*32}, source)
    assert not osm_source_cache.valid_manifest({**value, "bytes": 999}, source)
    assert not osm_source_cache.valid_manifest({**value, "url": source["url"].replace("261025", "261024")}, source)
    assert osm_source_cache.source_key(source) != osm_source_cache.source_key({**source, "md5": "b"*32})


def test_capacity_checked_before_public_download(monkeypatch):
    monkeypatch.setenv("OSM_SOURCE_CACHE_MIB_PER_WORKER", "512")
    source = declared()
    source["bytes"] = 513*osm_source_cache.MIB
    with pytest.raises(IntegrationError) as raised:
        osm_source_cache.obtain(source, lambda *_: pytest.fail("Capacity rejection must precede download"),
                               header, lambda: None, lambda *_: None)
    assert raised.value.code == "OSM_SOURCE_CAPACITY"


def test_explicitly_missing_snapshot_timestamp_cannot_be_cached():
    content = b"source-pbf-fixture"
    with pytest.raises(IntegrationError, match="snapshot identity"):
        osm_source_cache.obtain(declared(content), lambda source, target, *_: target.write_bytes(content),
                               lambda _: {"header": {}}, lambda: None, lambda *_: None)
    assert not list(settings.data_dir("osm-extracts").glob("*/manifest.json"))


def test_two_local_workers_publish_one_verified_source_not_partial_versions():
    calls, started, release = [], threading.Event(), threading.Event()
    content, source = b"source-pbf-fixture", declared()
    results, errors = [], []

    def download(source, target, check, progress):
        calls.append(source["url"])
        started.set()
        assert release.wait(5), "test synchronization"
        check()
        target.write_bytes(content)

    def work():
        try:
            results.append(osm_source_cache.obtain(source, download, header, lambda: None, lambda *_: None))
        except Exception as exc:  # noqa: BLE001 - the test records both concurrent outcomes
            errors.append(exc)
    first, second = threading.Thread(target=work), threading.Thread(target=work)
    first.start()
    assert started.wait(5)
    second.start()
    release.set()
    first.join(5)
    second.join(5)
    assert not first.is_alive() and not second.is_alive() and not errors
    assert len(calls) == 1 and results[0] == results[1]


class CloudCache:
    def __init__(self):
        self.objects, self.calls, self.stolen = {}, [], False

    def update_item(self, **request):
        self.calls.append(("lease", request))
        if self.stolen and request["ConditionExpression"] == "leaseToken=:token":
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")

    def get_object(self, **request):
        key = request["Key"]
        if key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[key])}

    def upload_fileobj(self, handle, bucket, key, *, Callback):
        content = handle.read()
        Callback(len(content))
        self.objects[key] = content
        self.calls.append(("source", key))

    def put_object(self, **request):
        self.calls.append(("manifest", request))
        assert request["IfNoneMatch"] == "*"
        if request["Key"] in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[request["Key"]] = request["Body"]


def cloud_cache(monkeypatch):
    cache = CloudCache()
    monkeypatch.setattr(settings, "CLOUD", True)
    monkeypatch.setattr(settings, "BUCKET", "fixture-road-artifacts")
    monkeypatch.setattr(osm_source_cache, "s3", lambda: cache)
    monkeypatch.setattr(osm_source_cache, "table", lambda: cache)
    for variable in ("OSM_SOURCE_CACHE_MIB_PER_WORKER", "OSM_ROAD_XML_MAX_MB", "DEMO_SESSION_DAYS"):
        monkeypatch.setenv(variable, str(settings.operational_settings()[variable]["value"]))
    return cache


def test_cloud_publication_fences_then_writes_source_before_create_only_manifest(monkeypatch):
    cache = cloud_cache(monkeypatch)
    content, source = b"source-pbf-fixture", declared()
    _, value = osm_source_cache.obtain(source, lambda source, target, *_: target.write_bytes(content),
                                     header, lambda: None, lambda *_: None)
    actions = [kind for kind, _ in cache.calls]
    assert actions.index("source") < actions.index("manifest")
    assert value["sha256"] == hashlib.sha256(content).hexdigest()
    assert all(key.startswith("osm-extracts/") for key in cache.objects)
    assert any(request.get("ConditionExpression") == "leaseToken=:token"
               for kind, request in cache.calls if kind == "lease")


def test_lost_source_lease_cannot_publish_metadata(monkeypatch):
    cache = cloud_cache(monkeypatch)
    source = declared()

    def inspect(path):
        cache.stolen = True
        return header(path)

    with pytest.raises(InterruptedError, match="lease was lost"):
        osm_source_cache.obtain(source, lambda source, target, *_: target.write_bytes(b"source-pbf-fixture"),
                               inspect, lambda: None, lambda *_: None)
    assert not cache.objects
    assert not list(settings.data_dir("osm-extracts").glob("*/manifest.json"))
