"""SQS handler: decides the next best action and calls the email or direct mail endpoint."""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from cdp import nba

ENDPOINT_PATHS = {"email": "/email", "direct_mail": "/direct-mail"}
LEDGER_DAYS = 90
SCAN_MIN_DAYS_BETWEEN_CONTACTS = 7
MODEL_CACHE_SECONDS = 300
_table = None
_token = None
_sqs = None
_model = None
_model_loaded_at = 0.0


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def _get_sqs():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs")
    return _sqs


def _get_token():
    global _token
    if _token is None:
        secret = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["RECEIVER_SECRET_ARN"])
        _token = secret["SecretString"]
    return _token


def _get_model():
    """Use the latest retrained model, or the bundled one until a retrain has published."""
    global _model, _model_loaded_at
    if _model is not None and time.time() - _model_loaded_at < MODEL_CACHE_SECONDS:
        return _model
    try:
        body = boto3.client("s3").get_object(Bucket=os.environ["MODEL_BUCKET"], Key=os.environ["MODEL_KEY"])["Body"]
        _model = json.loads(body.read())
    except ClientError:
        _model = _model or nba.BUNDLED_MODEL
    _model_loaded_at = time.time()
    return _model


def _ledger_key(message):
    return {"pk": f"CUSTOMER#{message['customer_id']}", "sk": f"NBA#{message['touch']}"}


def _claim(table, message):
    """Claim this touch so a duplicate decision cannot send it twice."""
    try:
        table.put_item(
            Item={
                **_ledger_key(message),
                "asset_id": message["asset_id"],
                "channel": message["channel"],
                "expires_at": int(time.time()) + LEDGER_DAYS * 86400,
            },
            ConditionExpression="attribute_not_exists(pk)",
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _record_send(table, message, now):
    key = {"pk": f"CUSTOMER#{message['customer_id']}", "sk": "PROFILE"}
    if message["asset_id"] == nba.assets.ONBOARDING:
        update = "SET s_onboarded = :yes, s_last_contact = :now"
        values = {":yes": True, ":now": now}
    else:
        update = "SET s_last_asset = :asset, s_last_contact = :now ADD s_touches :one"
        values = {":asset": message["asset_id"], ":now": now, ":one": 1}
    table.update_item(Key=key, UpdateExpression=update, ExpressionAttributeValues=values)


def _release(table, message):
    table.delete_item(Key=_ledger_key(message))
    if message["asset_id"] != nba.assets.ONBOARDING:
        table.update_item(
            Key={"pk": f"CUSTOMER#{message['customer_id']}", "sk": "PROFILE"},
            UpdateExpression="ADD s_touches :minus",
            ExpressionAttributeValues={":minus": -1},
        )


def _call_endpoint(message):
    request = urllib.request.Request(
        os.environ["RECEIVER_URL"].rstrip("/") + ENDPOINT_PATHS[message["channel"]],
        data=json.dumps(message).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_get_token()}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10):
        pass


def _log_decision(message, trigger):
    """Write the history row. A logging failure must not resend a message that already went out."""
    fields = (
        "message_id",
        "customer_id",
        "touch",
        "asset_id",
        "channel",
        "selection",
        "propensity",
        "expected_value",
        "model_version",
    )
    row = {field: message.get(field) for field in fields}
    row.update({"trigger": trigger, "decided_at": datetime.now(timezone.utc).isoformat()})
    try:
        _get_sqs().send_message(
            QueueUrl=os.environ["ARCHIVE_QUEUE_URL"],
            MessageBody=json.dumps({"dataset": "history", "row": row}),
        )
    except Exception as exc:
        print(f"history write failed for {message['message_id']}: {type(exc).__name__}")


def _too_recent(profile, now):
    # Scans respect a contact gap. Feedback-triggered decisions are the next step in a journey.
    if profile.get("trigger") != "scan":
        return False
    last_contact = profile.get("state", {}).get("last_contact", 0)
    return now - last_contact < SCAN_MIN_DAYS_BETWEEN_CONTACTS * 86400


def _process(profile, table, model):
    now = int(time.time())
    if _too_recent(profile, now):
        return
    message = nba.decide(
        profile["customer_id"], profile["traits"], profile["consent"], profile.get("state", {}), model
    )
    if message is None or not _claim(table, message):
        return
    # The send is recorded first because feedback can arrive right after the call.
    _record_send(table, message, now)
    try:
        _call_endpoint(message)
    except Exception:
        _release(table, message)  # Let the SQS retry claim and send this message again.
        raise
    _log_decision(message, profile.get("trigger"))


def handler(event, _context):
    table = _get_table()
    model = _get_model()
    failures = []
    for record in event["Records"]:
        try:
            _process(json.loads(record["body"]), table, model)
        except Exception as exc:
            print(f"decision failed for message {record['messageId']}: {type(exc).__name__}")
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
