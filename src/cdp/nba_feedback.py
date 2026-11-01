"""SQS handler: records engagement and saves it for learning. The next decision waits for the daily run."""
import json
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

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
    lag = int(outcome.get("registration_lag_days") or 0)
    if outcome["registered"] and lag == 0:
        # The welcome message goes out in the next daily run.
        day = int(feedback.get("delivery_day") or 0)
        sets.extend(["s_registered = :yes", "s_registered_day = :day", "s_next_due = :due"])
        values[":yes"] = True
        values[":day"] = day
        values[":due"] = day + 1
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
        "image_id": feedback.get("image_id"),
        "image_theme": feedback.get("image_theme"),
        "content_selection": feedback.get("content_selection"),
        "send_day": feedback.get("send_day"),
        "delivery_day": feedback.get("delivery_day"),
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
        lag = int(feedback["outcome"].get("registration_lag_days") or 0)
        if feedback["outcome"]["registered"] and lag > 0:
            # People often register some time after engaging, so this stays hidden until that day.
            resource.Table(table_name).put_item(
                Item={
                    "pk": f"REGDUE#{int(feedback.get('delivery_day') or 0) + lag}",
                    "sk": feedback["customer_id"],
                    "customer_id": feedback["customer_id"],
                }
            )
    sqs.send_message(
        QueueUrl=os.environ["ARCHIVE_QUEUE_URL"],
        MessageBody=json.dumps({"dataset": f"feedback_{feedback['channel']}", "row": training_record(feedback)}),
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
