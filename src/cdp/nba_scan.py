"""Scheduled or manual handler: scans every profile and queues it for a decision."""
import json
import os

import boto3
from boto3.dynamodb.conditions import Attr

from cdp.identity import _to_json

SEND_BATCH = 10


def profile_message(item, trigger):
    return {
        "customer_id": item["pk"].split("#", 1)[1],
        "traits": {k[2:]: _to_json(v) for k, v in item.items() if k.startswith("t_")},
        "consent": {k[2:]: v for k, v in item.items() if k.startswith("c_")},
        "state": {k[2:]: _to_json(v) for k, v in item.items() if k.startswith("s_")},
        "trigger": trigger,
    }


def _send(sqs, queue_url, messages):
    entries = [{"Id": str(i), "MessageBody": json.dumps(m)} for i, m in enumerate(messages)]
    while entries:
        failed = sqs.send_message_batch(QueueUrl=queue_url, Entries=entries).get("Failed", [])
        unrecoverable = [f for f in failed if f.get("SenderFault")]
        if unrecoverable:
            raise RuntimeError(f"{len(unrecoverable)} messages were rejected by SQS")
        failed_ids = {f["Id"] for f in failed}
        entries = [e for e in entries if e["Id"] in failed_ids]


def handler(_event, _context):
    table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    sqs = boto3.client("sqs")
    queue_url = os.environ["DECISION_QUEUE_URL"]

    scanned = 0
    pending = []
    kwargs = {"FilterExpression": Attr("sk").eq("PROFILE")}
    while True:
        page = table.scan(**kwargs)
        for item in page["Items"]:
            pending.append(profile_message(item, "scan"))
            scanned += 1
            if len(pending) == SEND_BATCH:
                _send(sqs, queue_url, pending)
                pending = []
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    if pending:
        _send(sqs, queue_url, pending)
    print(f"queued {scanned} profiles")
    return {"queued": scanned}
