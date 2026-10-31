"""SQS handler: batches rows into compressed JSON files on S3 for Athena and retraining."""
import gzip
import json
import os
import uuid
from datetime import datetime, timezone

import boto3

_s3 = None


def _client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def object_key(prefix, now):
    return f"{prefix}year={now:%Y}/month={now:%m}/day={now:%d}/{uuid.uuid4().hex}.json.gz"


def handler(event, _context):
    prefixes = json.loads(os.environ["DATASET_PREFIXES"])
    now = datetime.now(timezone.utc)

    groups = {}
    failures = []
    for record in event["Records"]:
        try:
            body = json.loads(record["body"])
            prefix = prefixes[body["dataset"]]
            line = json.dumps(body["row"]) + "\n"
            groups.setdefault(prefix, []).append((record["messageId"], line))
        except (KeyError, TypeError, ValueError) as exc:
            print(f"bad archive message {record['messageId']}: {type(exc).__name__}")
            failures.append({"itemIdentifier": record["messageId"]})

    for prefix, rows in groups.items():
        try:
            _client().put_object(
                Bucket=os.environ["BUCKET"],
                Key=object_key(prefix, now),
                Body=gzip.compress("".join(line for _, line in rows).encode()),
            )
        except Exception as exc:
            print(f"archive write failed for {prefix}: {type(exc).__name__}")
            failures.extend({"itemIdentifier": message_id} for message_id, _ in rows)
    return {"batchItemFailures": failures}
