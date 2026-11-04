"""DynamoDB stream fast path and scheduled repair for committed job outboxes."""
import json
import logging
import os

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def sequence_number(entry):
    """Return a stream record's sequence number (its partial batch failure identifier), or None if absent."""
    stream = entry.get("dynamodb") if isinstance(entry, dict) else None
    value = stream.get("SequenceNumber") if isinstance(stream, dict) else None
    return value if isinstance(value, str) and value else None


def handler(event, context):
    """Send committed job outboxes to SQS from DynamoDB stream records, or reconcile all pending jobs."""
    table = boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"])
    sqs = boto3.client("sqs")

    def dispatch(job_id):
        job = table.get_item(Key={"id": job_id}, ConsistentRead=True).get("Item", {})
        if not job.get("outbox_pending"):
            return
        variable = "FIRE_RISK_QUEUE_URL" if job.get("kind") == "fire_risk_prepare" else "QUEUE_URL"
        sqs.send_message(QueueUrl=os.environ[variable], MessageBody=json.dumps({"id": job_id}))
        table.update_item(Key={"id": job_id}, UpdateExpression="SET outbox_pending=:no REMOVE pending_outbox",
                          ExpressionAttributeValues={":no": False})

    if "Records" in event:
        failed = []
        for entry in event["Records"]:
            try:
                dispatch(entry["dynamodb"]["Keys"]["id"]["S"])
            except (BotoCoreError, ClientError, KeyError, TypeError):
                # Report a partial failure so only this record is retried; log without payload data.
                logger.exception("Outbox dispatch failed for one stream record")
                sequence = sequence_number(entry)
                if sequence:
                    failed.append({"itemIdentifier": sequence})
                else:
                    # A record without a sequence number cannot be retried on its own; the scheduled
                    # pending-index query still dispatches any job whose outbox remains pending.
                    logger.error("Stream record has no sequence number; leaving it to scheduled repair")
        return {"batchItemFailures": failed}
    start = {}
    while True:
        page = table.query(IndexName="pending-outbox", KeyConditionExpression="pending_outbox=:pending",
                           ExpressionAttributeValues={":pending": "pending"}, Limit=100, **start)
        for job in page["Items"]:
            dispatch(job["id"])
        if not page.get("LastEvaluatedKey"):
            break
        start = {"ExclusiveStartKey": page["LastEvaluatedKey"]}
    return {"status": "reconciled"}
