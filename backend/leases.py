"""Owner leases: one Runtime-backed investigation, radio relay or report of a kind at a time per visitor.

A lease is a namespaced record (kind:key) holding an attempt_id and lease_until. It is committed before the caller
spends the shared daily agent budget, so a concurrent duplicate that loses the commit spends nothing. The attempt that
holds it releases it when its work ends; an unreleased lease ends at lease_until.
"""
import logging
import sqlite3
import time
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from traffic_simulator.errors import IntegrationError
from traffic_simulator.persistence import commit, owned, record

logger = logging.getLogger(__name__)


def acquire(store, kind, key, owner, seconds, busy, *, spend=None, attempt_id=None):
    """Commit the owner's `kind` lease on `key` for `seconds`, then spend for it; raise `busy` while a lease holds it.

    A lease committed concurrently by another request makes this commit fail with 409 REVISION_CONFLICT. spend(store),
    when given, runs only after the commit (for example spending the daily agent budget); when it raises, the lease is
    released again. attempt_id, a new UUID unless given, fences release(). Returns the committed lease record.
    """
    previous = store.get(f"{kind}:{key}")
    if previous and previous.get("lease_until", 0) > time.time():
        raise busy
    lease = record(kind, key, owner, attempt_id=attempt_id or str(uuid4()), lease_until=time.time() + seconds)
    lease["revision"] = (previous or {}).get("revision", 0) + 1
    commit(store, [(lease, previous["revision"] if previous else None)])
    if spend:
        spend_or_release(store, kind, key, owner, lease["attempt_id"], spend)
    return lease


def spend_or_release(store, kind, key, owner, attempt_id, spend, *, ended=None):
    """Run spend(store) for a lease the attempt has committed; when it raises (such as 429), release the lease first.

    ended(exc), when given, returns the fields the release also writes, such as a refused turn's final status.
    """
    try:
        spend(store)
    except BaseException as exc:
        release(store, kind, key, owner, attempt_id, **(ended(exc) if ended else {}))
        raise


def release(store, kind, key, owner, attempt_id, **ended):
    """End the owner's `kind` lease on `key`, writing the `ended` fields, while attempt_id still holds it.

    Best effort, as the lease also expires.
    """
    try:
        current = owned(store, kind, key, owner)
        if current.get("attempt_id") != attempt_id:
            return
        revision = current["revision"]
        current.update(ended, revision=revision + 1, lease_until=0)
        commit(store, [(current, revision)])
    except (IntegrationError, BotoCoreError, ClientError, sqlite3.Error) as exc:
        # A newer attempt's write, a vanished record or a storage fault: the lease still ends at its lease_until.
        logger.warning("A %s lease was not released: reason=%s", kind, type(exc).__name__)
