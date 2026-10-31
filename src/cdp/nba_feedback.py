"""SQS handler: records engagement, saves it for retraining and requests the next decision."""
import json
import os
import random
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from cdp.nba_scan import profile_message

MAX_NEXT_DELAY_SECONDS = 60
_resource = None
_sqs = None


def _clients():
    global _resource, _sqs
    if _resource is None:
        _resource = boto3.resource("dynamodb")
        _sqs = boto3.client("sqs")
    return _resource, _sqs


def _apply(resource, table_name, feedback, profile_key, ledger_key):
    """Mark the touch and update counters together, so a retry cannot count engagement twice."""
    outcome = feedback["outcome"]
    updates = ["ADD s_opens :opens, s_clicks :clicks"]
    values = {":opens": int(outcome["opened"]), ":clicks": int(outcome["clicked"])}
    sets = []
    if outcome["registered"]:
        sets.append("s_registered = :yes")
        values[":yes"] = True
    names = {}
    if outcome["unsubscribed"]:
        # Opting out removes that channel's consent, which later decisions enforce.
        names["#consent"] = f"c_{feedback['channel']}"
        sets.append("#consent = :no")
        values[":no"] = False
    if sets:
        updates.insert(0, "SET " + ", ".join(sets))

    profile_update = {
        "TableName": table_name,
        "Key": profile_key,
        "UpdateExpression": " ".join(updates),
        "ExpressionAttributeValues": values,
    }
    if names:
        profile_update["ExpressionAttributeNames"] = names
    try:
        # Returns True only the first time, so retries do not inflate dashboard totals.
        resource.meta.client.transact_write_items(
            TransactItems=[
                {
                    "Update": {
                        "TableName": table_name,
                        "Key": ledger_key,
                        "UpdateExpression": "SET feedback_received = :yes",
                        "ConditionExpression": "attribute_exists(pk) AND attribute_not_exists(feedback_received)",
                        "ExpressionAttributeValues": {":yes": True},
                    }
                },
                {"Update": profile_update},
            ]
        )
        return True
    except ClientError as exc:
        # On a retry the counters are already saved, so the remaining steps should still run.
        if exc.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        reasons = exc.response.get("CancellationReasons") or []
        if not reasons or reasons[0].get("Code") != "ConditionalCheckFailed":
            raise
        return False


def training_record(feedback):
    return {
        "message_id": feedback["message_id"],
        "customer_id": feedback["customer_id"],
        "touch": feedback["touch"],
        "asset_id": feedback["asset_id"],
        "channel": feedback["channel"],
        "features": feedback["features"],
        "model_version": feedback.get("model_version"),
        "selection": feedback.get("selection"),
        "feedback_at": datetime.now(timezone.utc).isoformat(),
        **feedback["outcome"],
    }


def _emit_metrics(feedback):
    # CloudWatch reads this log line as metrics. It contains no customer data.
    outcome = feedback["outcome"]
    values = {
        "Delivered": 1,
        "Opened": int(outcome["opened"]),
        "Clicked": int(outcome["clicked"]),
        "Registered": int(outcome["registered"]),
        "Unsubscribed": int(outcome["unsubscribed"]),
    }
    print(
        json.dumps(
            {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": os.environ["METRIC_NAMESPACE"],
                            "Dimensions": [["Channel"]],
                            "Metrics": [{"Name": name, "Unit": "Count"} for name in values],
                        }
                    ],
                },
                "Channel": feedback["channel"],
                **values,
            }
        )
    )


def _process(feedback, resource, sqs):
    table_name = os.environ["TABLE_NAME"]
    profile_key = {"pk": f"CUSTOMER#{feedback['customer_id']}", "sk": "PROFILE"}
    ledger_key = {"pk": profile_key["pk"], "sk": f"NBA#{feedback['touch']}"}

    if _apply(resource, table_name, feedback, profile_key, ledger_key):
        _emit_metrics(feedback)
    sqs.send_message(
        QueueUrl=os.environ["ARCHIVE_QUEUE_URL"],
        MessageBody=json.dumps({"dataset": f"feedback_{feedback['channel']}", "row": training_record(feedback)}),
    )
    profile = resource.Table(table_name).get_item(Key=profile_key, ConsistentRead=True)["Item"]
    sqs.send_message(
        QueueUrl=os.environ["DECISION_QUEUE_URL"],
        MessageBody=json.dumps(profile_message(profile, "feedback")),
        DelaySeconds=random.randint(0, MAX_NEXT_DELAY_SECONDS),
    )


def handler(event, _context):
    resource, sqs = _clients()
    failures = []
    for record in event["Records"]:
        try:
            _process(json.loads(record["body"]), resource, sqs)
        except Exception as exc:
            print(f"feedback failed for message {record['messageId']}: {type(exc).__name__}")
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
