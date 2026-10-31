"""SQS handler: records engagement, saves it for retraining and requests the next decision."""
import json
import os
import random

import boto3
from botocore.exceptions import ClientError

from cdp.nba_scan import profile_message

MAX_NEXT_DELAY_SECONDS = 60
_resource = None
_firehose = None
_sqs = None


def _clients():
    global _resource, _firehose, _sqs
    if _resource is None:
        _resource = boto3.resource("dynamodb")
        _firehose = boto3.client("firehose")
        _sqs = boto3.client("sqs")
    return _resource, _firehose, _sqs


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
    except ClientError as exc:
        # On a retry the counters are already saved, so the remaining steps should still run.
        if exc.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        reasons = exc.response.get("CancellationReasons") or []
        if not reasons or reasons[0].get("Code") != "ConditionalCheckFailed":
            raise


def training_record(feedback):
    return {
        "message_id": feedback["message_id"],
        "customer_id": feedback["customer_id"],
        "asset_id": feedback["asset_id"],
        "channel": feedback["channel"],
        "features": feedback["features"],
        "model_version": feedback.get("model_version"),
        "selection": feedback.get("selection"),
        **feedback["outcome"],
    }


def _process(feedback, resource, firehose, sqs):
    table_name = os.environ["TABLE_NAME"]
    profile_key = {"pk": f"CUSTOMER#{feedback['customer_id']}", "sk": "PROFILE"}
    ledger_key = {"pk": profile_key["pk"], "sk": f"NBA#{feedback['touch']}"}

    _apply(resource, table_name, feedback, profile_key, ledger_key)
    firehose.put_record(
        DeliveryStreamName=os.environ["FEEDBACK_STREAM"],
        Record={"Data": (json.dumps(training_record(feedback)) + "\n").encode()},
    )
    profile = resource.Table(table_name).get_item(Key=profile_key, ConsistentRead=True)["Item"]
    sqs.send_message(
        QueueUrl=os.environ["DECISION_QUEUE_URL"],
        MessageBody=json.dumps(profile_message(profile, "feedback")),
        DelaySeconds=random.randint(0, MAX_NEXT_DELAY_SECONDS),
    )


def handler(event, _context):
    resource, firehose, sqs = _clients()
    failures = []
    for record in event["Records"]:
        try:
            _process(json.loads(record["body"]), resource, firehose, sqs)
        except Exception as exc:
            print(f"feedback failed for message {record['messageId']}: {type(exc).__name__}")
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
