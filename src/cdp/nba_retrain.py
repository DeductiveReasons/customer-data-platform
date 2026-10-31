"""Scheduled or manual handler: retrains models from feedback and publishes the result."""
import gzip
import json
import os
from datetime import datetime, timezone

import boto3

from cdp import content, nba, training

MAX_ROWS = 40_000


def load_records(s3, bucket, prefix):
    """Read gzipped feedback, keeping one row per message and skipping non-signup assets."""
    records = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            data = gzip.decompress(s3.get_object(Bucket=bucket, Key=item["Key"])["Body"].read())
            for line in data.decode().splitlines():
                if line.strip():
                    record = json.loads(line)
                    if record["asset_id"] in nba.assets.SIGNUP_ASSETS:
                        records[record["message_id"]] = record
    return list(records.values())[-MAX_ROWS:]


def _current_model(s3, bucket, key):
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    except s3.exceptions.NoSuchKey:
        return nba.BUNDLED_MODEL


def handler(_event, _context):
    s3 = boto3.client("s3")
    bucket = os.environ["MODEL_BUCKET"]
    key = os.environ["MODEL_KEY"]

    records = load_records(s3, bucket, os.environ["FEEDBACK_PREFIX"])
    version = "feedback-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    current = _current_model(s3, bucket, key)
    model, report = training.train(records, nba.FEATURES, version, previous=current)
    previous_content = current.get("content") or content.BUNDLED_MODEL
    content_model, content_report = training.train(
        records, content.FEATURES, version, previous=previous_content, key="image_theme", label="clicked"
    )
    report = {"assets": report, "content": content_report}
    print(json.dumps({"rows": len(records), "report": report}))
    if model is None and content_model is None:
        return {"published": False, "rows": len(records), "report": report}
    model = model or current
    model["content"] = content_model or previous_content
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(model).encode(), ContentType="application/json")
    return {"published": True, "version": version, "rows": len(records), "report": report}
