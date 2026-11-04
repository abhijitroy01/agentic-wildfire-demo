"""Persistent SUMO road-network cache: a bounded local folder per worker and, in AWS mode, a shared S3 copy.

An entry is DATA_DIR/networks/<key>/ with network.net.xml and metadata.json (which records net_sha256); the key comes
from network.network_key. Every reuse checks the network's SHA-256 first. The local folder keeps at most
CACHED_NETWORKS_PER_WORKER entries in CACHED_NETWORK_MIB_PER_WORKER of disk and deletes the least recently used
first. In AWS mode the artifacts bucket shares each built network under network-cache/<key>/ (the gzip network,
then metadata.json) until its lifecycle rule (cache.network_cache_days) deletes it. Local mode uses only the
filesystem.
"""
import gzip
import hashlib
import json
import logging
import os
import shutil
import tempfile
import zlib
from contextlib import contextmanager
from functools import cache
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from traffic_simulator import settings

NETWORK, METADATA = "network.net.xml", "metadata.json"
# The S3 prefix the worker role may read and write, and the artifacts bucket's lifecycle rule expires.
PREFIX = "network-cache"
# S3 answers for an object that is not there (NoSuchKey needs s3:ListBucket, which the worker has).
MISSING = {"NoSuchKey", "404", "NotFound"}
MIB = 1024 * 1024
logger = logging.getLogger(__name__)


def cached_network(netfile, meta_path):
    """Return the metadata of a cached road network, or None when it is missing or fails its SHA-256 check."""
    try:
        metadata = json.loads(meta_path.read_text())
        recorded = metadata.get("net_sha256")
        if recorded and hashlib.sha256(netfile.read_bytes()).hexdigest() != recorded:
            logger.warning("Cached road network %s failed its checksum; rebuilding", netfile.parent.name)
            return None
        return metadata if netfile.exists() else None
    except (OSError, ValueError):
        return None


def reuse(folder):
    """Return the metadata of a valid local entry and mark it as just used for eviction, or None on a miss."""
    metadata = cached_network(folder / NETWORK, folder / METADATA)
    if metadata is not None:
        os.utime(folder / METADATA)
    return metadata


@contextmanager
def scratch(folder):
    """Yield a private scratch folder inside an entry, deleted afterwards, so concurrent builds share no files."""
    path = Path(tempfile.mkdtemp(prefix="building-", dir=folder))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def install(folder, prepared, metadata):
    """Move the network in a scratch folder into its entry: the network first, then metadata, so readers verify."""
    (prepared / METADATA).write_text(json.dumps(metadata))
    os.replace(prepared / NETWORK, folder / NETWORK)
    os.replace(prepared / METADATA, folder / METADATA)


@cache
def client():
    """Return the S3 client for the shared cache in the artifacts bucket (AWS mode only)."""
    return boto3.client("s3", region_name=settings.REGION)


def fetch_shared(key, folder):
    """AWS mode: copy the S3 network-cache/ copy of a network into its local entry once its SHA-256 matches.

    Returns the metadata, or None in local mode and on a miss. An unreadable, partial or mismatched copy is logged
    and treated as a miss, so the caller builds the network.
    """
    if not settings.CLOUD:
        return None
    try:
        metadata = json.loads(client().get_object(Bucket=settings.BUCKET, Key=f"{PREFIX}/{key}/{METADATA}")[
            "Body"].read())
        network = gzip.decompress(client().get_object(Bucket=settings.BUCKET, Key=f"{PREFIX}/{key}/{NETWORK}.gz")[
            "Body"].read())
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code not in MISSING:
            logger.warning("Shared road network %s is unavailable (%s); building it", key, code)
        return None
    except (BotoCoreError, OSError, EOFError, ValueError, zlib.error) as exc:
        logger.warning("Shared road network %s is unreadable (%s); building it", key, type(exc).__name__)
        return None
    if not isinstance(metadata, dict) or hashlib.sha256(network).hexdigest() != metadata.get("net_sha256"):
        logger.warning("Shared road network %s failed its checksum; building it", key)
        return None
    with scratch(folder) as prepared:
        (prepared / NETWORK).write_bytes(network)
        install(folder, prepared, metadata)
    return metadata


def share(key, folder, metadata):
    """AWS mode: write a newly built network to S3 under network-cache/<key>/; True once both objects are written.

    The gzip network goes first and metadata.json last, so a reader never finds metadata for a missing network. A
    failed write is logged and the run continues with its local copy.
    """
    if not settings.CLOUD:
        return False
    try:
        client().put_object(Bucket=settings.BUCKET, Key=f"{PREFIX}/{key}/{NETWORK}.gz",
                            Body=gzip.compress((folder / NETWORK).read_bytes()),
                            ContentType="application/xml", ContentEncoding="gzip")
        client().put_object(Bucket=settings.BUCKET, Key=f"{PREFIX}/{key}/{METADATA}",
                            Body=json.dumps(metadata).encode(), ContentType="application/json")
    except (BotoCoreError, ClientError) as exc:
        logger.warning("Could not share road network %s (%s); this worker keeps its local copy", key,
                       type(exc).__name__)
        return False
    return True


def footprint(folder):
    """Return (last use time, bytes on disk) of a local entry; its metadata file's time is the last use."""
    size = sum(path.stat().st_size for path in folder.rglob("*") if path.is_file())
    marker = folder / METADATA
    return (marker if marker.exists() else folder).stat().st_mtime, size


def evict(keep):
    """Delete the least recently used local networks until both worker bounds hold, never the entry `keep`.

    Only the worker adds networks, one job at a time, so the only network in use is `keep`: the one just added for
    the current job. That entry stays even when it alone is larger than the size bound.
    """
    most, largest = settings.cached_networks_per_worker(), settings.cached_network_mib_per_worker() * MIB
    entries = sorted((*footprint(folder), folder) for folder in settings.data_dir("networks").iterdir()
                     if folder.is_dir())
    count, total = len(entries), sum(size for _, size, _ in entries)
    for _, size, folder in entries:
        if count <= most and total <= largest:
            break
        if folder.name == keep:
            continue
        shutil.rmtree(folder, ignore_errors=True)
        count, total = count - 1, total - size
        logger.info("Removed cached road network %s (least recently used)", folder.name)
