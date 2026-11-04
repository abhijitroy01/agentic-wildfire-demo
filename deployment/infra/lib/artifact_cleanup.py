"""Bounded collection of artifacts after their authoritative metadata disappears.

Never expire current artifacts solely by age: a retained variant may still need
its parent. One day of grace covers failed uploads and concurrent commits.
"""
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import boto3

GRACE = timedelta(days=1)


def referenced_assessments(root, prefix, objects):
    """A risk receipt can outlive its preparation job; retain all inputs until both references disappear."""
    if root != "fire-risk/inputs/":
        return []
    identifiers = set()
    for item in objects:
        identifier = item["Key"].removeprefix(prefix).split("/")[0]
        try:
            UUID(identifier)
        except ValueError:
            continue
        identifiers.add(f"fire_exposure:{identifier}")
    return sorted(identifiers)


def handler(event, context):
    table = boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"])
    s3 = boto3.client("s3")
    bucket = os.environ["ARTIFACTS_BUCKET"]
    cutoff = datetime.now(UTC) - GRACE
    deleted = 0
    for root, key_prefix in (("runs/", ""), ("reports/", "firetruck_report:"), ("fire-risk/inputs/", "")):
        checkpoint = f"artifact_cleanup:cursor:{root}"
        cursor = table.get_item(Key={"id": checkpoint}, ConsistentRead=True).get("Item", {}).get("cursor")
        page = s3.list_objects_v2(Bucket=bucket, Prefix=root, Delimiter="/", MaxKeys=50,
                                 **({"ContinuationToken": cursor} if cursor else {}))
        for entry in page.get("CommonPrefixes", []):
            prefix = entry["Prefix"]
            identifier = prefix.removeprefix(root).removesuffix("/")
            try:
                UUID(identifier)
            except ValueError:
                continue
            key = {"id": f"{key_prefix}{identifier}"}
            if table.get_item(Key=key, ConsistentRead=True).get("Item"):
                continue
            objects = []
            for items in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
                objects.extend(items.get("Contents", []))
            if any(item["LastModified"] > cutoff for item in objects):
                continue
            # An upload may have committed while listing. Check again before deleting.
            if table.get_item(Key=key, ConsistentRead=True).get("Item"):
                continue
            if any(table.get_item(Key={"id": identifier}, ConsistentRead=True).get("Item")
                   for identifier in referenced_assessments(root, prefix, objects)):
                continue
            for offset in range(0, len(objects), 1000):
                response = s3.delete_objects(
                    Bucket=bucket, Delete={"Objects": [{"Key": item["Key"]} for item in
                                                      objects[offset:offset + 1000]], "Quiet": True})
                if response.get("Errors"):
                    raise RuntimeError("Artifact collection failed; retrying the same page.")
                deleted += len(objects[offset:offset + 1000])
        table.update_item(Key={"id": checkpoint}, UpdateExpression="SET #cursor=:cursor",
                          ExpressionAttributeNames={"#cursor": "cursor"},
                          ExpressionAttributeValues={":cursor": page.get("NextContinuationToken", "")})
    return {"deleted": deleted}
