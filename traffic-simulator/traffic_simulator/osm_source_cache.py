"""Immutable Geofabrik source files: local bounded cache and worker-only shared S3 storage.

The publisher's dated URL/checksum selects the source; a cached older source never substitutes for it. Metadata is
published last. Files are verified before use, and a distributed renewable lease fences first publication.
"""
import hashlib
import json
import logging
import os
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from functools import cache
from uuid import uuid4

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from traffic_simulator import settings
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import digest, expires_at

PREFIX = "osm-extracts"
MIB = 1024 * 1024
MISSING = {"NoSuchKey", "404", "NotFound"}
logger = logging.getLogger(__name__)


def source_key(source):
    """A dated publisher file and its declared checksum/size have one immutable cache identity."""
    return digest({name: source[name] for name in ("url", "md5", "bytes")})


@cache
def s3():
    return boto3.client("s3", region_name=settings.REGION)


@cache
def table():
    return boto3.resource("dynamodb", region_name=settings.REGION).Table(settings.TABLE)


def folder(source):
    return settings.data_dir("osm-extracts", source_key(source))


def valid_manifest(value, source):
    """A source manifest cannot silently change its publisher selection or content identity."""
    if not isinstance(value, dict) or any(value.get(key) != source[key]
                                          for key in ("region_id", "url", "md5", "bytes")):
        return False
    checksum = value.get("sha256")
    header = value.get("header")
    snapshot = value.get("snapshot_timestamp")
    if (not isinstance(checksum, str) or len(checksum) != 64
            or not all(char in "0123456789abcdef" for char in checksum)
            or not isinstance(header, dict) or header.get("with_history")
            or not isinstance(snapshot, str)
            or header.get("option", {}).get("osmosis_replication_timestamp") != snapshot):
        return False
    try:
        return datetime.fromisoformat(snapshot).utcoffset() is not None
    except ValueError:
        return False


def manifest(source):
    """Read small exact-source metadata without downloading a regional file for a derived network cache hit."""
    path = folder(source) / "manifest.json"
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        value = None
    if valid_manifest(value, source):
        return value
    if not settings.CLOUD:
        return None
    try:
        body = s3().get_object(Bucket=settings.BUCKET, Key=f"{PREFIX}/{source_key(source)}/manifest.json")["Body"]
        try:
            content = body.read(64 * 1024 + 1)
        finally:
            body.close()
        try:
            value = json.loads(content) if len(content) <= 64 * 1024 else None
        except ValueError:
            value = None
    except ClientError as exc:
        if exc.response["Error"]["Code"] in MISSING:
            return None
        raise
    if not valid_manifest(value, source):
        raise IntegrationError("OSM_SOURCE_INVALID", "The selected road-source cache failed its identity check.", 502)
    install_json(path, value)
    return value


def install_json(path, value):
    temporary = path.with_name(path.name + "." + uuid4().hex + ".part")
    try:
        temporary.write_text(json.dumps(value, sort_keys=True))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def checksums(path):
    sha, md5, size = hashlib.sha256(), hashlib.md5(usedforsecurity=False), 0
    with path.open("rb") as handle:
        while chunk := handle.read(MIB):
            sha.update(chunk)
            md5.update(chunk)
            size += len(chunk)
    return sha.hexdigest(), md5.hexdigest(), size


def verified_file(path, source, metadata=None):
    if not path.is_file() or path.stat().st_size != source["bytes"]:
        return False
    sha, md5, size = checksums(path)
    return (size == source["bytes"] and md5 == source["md5"]
            and (metadata is None or sha == metadata["sha256"]))


@contextmanager
def publication_lease(source, check):
    """Wait for one source publisher, renewing its existing worker-style lease while downloading.

    This is a separate namespaced metadata record, with no job-index attributes. Duplicate preparation jobs do not
    download/publish different files for one source identity. File bytes themselves are immutable and verified.
    """
    import fcntl

    from traffic_simulator.store import LEASE_SECONDS
    lock = folder(source) / ".lock"
    with lock.open("a") as handle:
        while True:
            check()
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(1)
        token, key = uuid4().hex, "osm_source_download:" + source_key(source)
        done, lost = threading.Event(), threading.Event()
        if settings.CLOUD:
            while True:
                check()
                try:
                    table().update_item(
                        Key={"id": key},
                        UpdateExpression="SET leaseToken=:token, leaseUntil=:until, expires=:expires, #k=:kind",
                        ConditionExpression="attribute_not_exists(leaseUntil) OR leaseUntil < :now",
                        ExpressionAttributeNames={"#k": "kind"},
                        ExpressionAttributeValues={":token": token, ":until": int(time.time()) + LEASE_SECONDS,
                                                   ":now": int(time.time()), ":expires": expires_at(),
                                                   ":kind": "osm_source_download"})
                    break
                except ClientError as exc:
                    if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                        raise
                    time.sleep(1)

        def ownership(*, fence=False):
            check()
            if lost.is_set():
                raise InterruptedError("Road-source publication lease was lost")
            if settings.CLOUD and fence:
                try:
                    table().update_item(
                        Key={"id": key}, UpdateExpression="SET leaseUntil=:until",
                        ConditionExpression="leaseToken=:token",
                        ExpressionAttributeValues={":token": token, ":until": int(time.time()) + LEASE_SECONDS})
                except ClientError as exc:
                    if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                        lost.set()
                        raise InterruptedError("Road-source publication lease was lost") from None
                    raise

        def renew():
            while not done.wait(30):
                try:
                    table().update_item(
                        Key={"id": key}, UpdateExpression="SET leaseUntil=:until",
                        ConditionExpression="leaseToken=:token",
                        ExpressionAttributeValues={":token": token, ":until": int(time.time()) + LEASE_SECONDS})
                except Exception:
                    logger.exception("Road-source lease renewal failed")
                    lost.set()
                    return

        thread = threading.Thread(target=renew, daemon=True) if settings.CLOUD else None
        if thread:
            thread.start()
        try:
            yield ownership
        finally:
            done.set()
            if thread:
                thread.join(timeout=2)
                try:
                    table().update_item(Key={"id": key}, UpdateExpression="SET leaseUntil=:zero",
                                        ConditionExpression="leaseToken=:token",
                                        ExpressionAttributeValues={":token": token, ":zero": 0})
                except (BotoCoreError, ClientError):
                    logger.warning("Road-source lease release deferred to expiry")
            fcntl.flock(handle, fcntl.LOCK_UN)


def evict(keep, incoming=0):
    """Remove inactive oldest regional files before a download would exceed the separate source-cache bound."""
    import fcntl
    root = settings.data_dir("osm-extracts")
    budget = settings.osm_source_cache_mib_per_worker() * MIB
    entries = [(path.stat().st_mtime, path.stat().st_size, path.parent)
               for path in root.glob("*/source.osm.pbf")]
    total = sum(size for _, size, _ in entries)
    for _, size, entry in sorted(entries):
        if total + incoming <= budget:
            break
        if entry == keep:
            continue
        with (entry / ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            (entry / "source.osm.pbf").unlink(missing_ok=True)
            total -= size
    if total + incoming > budget:
        raise IntegrationError("OSM_SOURCE_CAPACITY", "The selected regional road source exceeds cache capacity.", 503)


def obtain(source, download, inspect, check, progress):
    """Return verified source bytes plus manifest, fetching only this exact publisher selection on a miss."""
    location = folder(source)
    target = location / "source.osm.pbf"
    with publication_lease(source, check) as ownership:
        metadata = manifest(source)
        publish_source = metadata is None
        if verified_file(target, source, metadata):
            if metadata:
                os.utime(target)
                return target, metadata
        else:
            target.unlink(missing_ok=True)
            evict(location, incoming=source["bytes"])
            # The source download and the temporary geographic extraction can coexist with cached networks.
            free = shutil.disk_usage(location).free
            required = source["bytes"] * 2 + settings.osm_road_xml_max_mb() * 1_000_000
            if free < required:
                raise IntegrationError("OSM_SOURCE_CAPACITY", "Insufficient scratch space for this road source.", 503)
            temporary = location / (uuid4().hex + ".part")
            try:
                shared = False
                if settings.CLOUD and metadata:
                    try:
                        response = s3().get_object(Bucket=settings.BUCKET,
                                                  Key=f"{PREFIX}/{source_key(source)}/source.osm.pbf")
                    except ClientError as exc:
                        if exc.response["Error"]["Code"] not in MISSING:
                            raise
                    else:
                        body = response["Body"]
                        try:
                            progress(f"Restoring exact regional source {source['region_id']}", 0.04)
                            with temporary.open("wb") as handle:
                                received, reported = 0, time.monotonic()
                                for chunk in body.iter_chunks(chunk_size=MIB):
                                    ownership()
                                    received += len(chunk)
                                    if received > source["bytes"]:
                                        raise ValueError("Regional road cache exceeds its recorded size")
                                    handle.write(chunk)
                                    if time.monotonic()-reported >= 1:
                                        progress(f"Restoring {source['region_id']}: "
                                                 f"{received:,} / {source['bytes']:,} bytes", 0.04)
                                        reported = time.monotonic()
                            shared = True
                        finally:
                            body.close()
                if not shared:
                    download(source, temporary, ownership, progress)
                    publish_source = True
                if not verified_file(temporary, source, metadata):
                    raise IntegrationError("OSM_SOURCE_INVALID", "The selected regional road file failed verification.",
                                           502)
                ownership()
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        sha, _, _ = checksums(target)
        header = inspect(target)
        snapshot = header.get("header", {}).get("option", {}).get("osmosis_replication_timestamp")
        if not isinstance(snapshot, str) or header.get("header", {}).get("with_history"):
            raise IntegrationError("OSM_SOURCE_INVALID", "The regional road file has no supported snapshot identity.",
                                   502)
        value = {**source, "sha256": sha, "snapshot_timestamp": snapshot, "header": header["header"],
                 "publisher": "Geofabrik", "cached_at": time.time()}
        if not valid_manifest(value, source):
            raise IntegrationError("OSM_SOURCE_INVALID", "The regional road file has an invalid snapshot identity.",
                                   502)
        if metadata and (metadata["sha256"] != sha or metadata["snapshot_timestamp"] != snapshot):
            raise IntegrationError("OSM_SOURCE_INVALID", "The road snapshot changed under an immutable source key.", 502)
        ownership(fence=True)
        if settings.CLOUD and publish_source:
            progress(f"Caching verified regional source {source['region_id']}", 0.05)
            with target.open("rb") as handle:
                s3().upload_fileobj(handle, settings.BUCKET, f"{PREFIX}/{source_key(source)}/source.osm.pbf",
                                    Callback=lambda _: ownership())
            ownership(fence=True)
        if settings.CLOUD and metadata is None:
            try:
                s3().put_object(Bucket=settings.BUCKET, Key=f"{PREFIX}/{source_key(source)}/manifest.json",
                                Body=json.dumps(value, sort_keys=True).encode(), ContentType="application/json",
                                IfNoneMatch="*")
            except ClientError as exc:
                if exc.response["Error"]["Code"] not in ("PreconditionFailed", "412"):
                    raise
                recorded = manifest(source)
                if recorded is None or (recorded["sha256"], recorded["snapshot_timestamp"]) != (sha, snapshot):
                    raise IntegrationError("OSM_SOURCE_INVALID",
                                           "A different road snapshot already owns this immutable source key.",
                                           502) from None
                value = recorded
        install_json(location / "manifest.json", value)
        return target, value
