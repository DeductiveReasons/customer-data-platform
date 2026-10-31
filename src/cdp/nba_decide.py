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
_table = None
_token = None


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def _get_token():
    global _token
    if _token is None:
        secret = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["RECEIVER_SECRET_ARN"])
        _token = secret["SecretString"]
    return _token


def contact_period(now=None):
    year, week, _ = (now or datetime.now(timezone.utc)).isocalendar()
    return f"{year}-W{week:02d}"


def _claim(table, message):
    """Limit each customer to one message per week. Returns False if already contacted."""
    try:
        table.put_item(
            Item={
                "pk": f"CUSTOMER#{message['customer_id']}",
                "sk": f"NBA#{message['period']}",
                "action": message["action"],
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


def _release(table, message):
    table.delete_item(Key={"pk": f"CUSTOMER#{message['customer_id']}", "sk": f"NBA#{message['period']}"})


def _call_endpoint(message):
    request = urllib.request.Request(
        os.environ["RECEIVER_URL"].rstrip("/") + ENDPOINT_PATHS[message["channel"]],
        data=json.dumps(message).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_get_token()}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10):
        pass


def _process(profile, period, table):
    decision = nba.decide(profile["customer_id"], profile["traits"], profile["consent"])
    if decision is None:
        return
    message = {**decision, "period": period, "message_id": f"{decision['customer_id']}-{period}"}
    if not _claim(table, message):
        return
    try:
        _call_endpoint(message)
    except Exception:
        _release(table, message)  # Let the SQS retry claim and send this message again.
        raise


def handler(event, _context):
    table = _get_table()
    period = contact_period()
    failures = []
    for record in event["Records"]:
        try:
            _process(json.loads(record["body"]), period, table)
        except Exception as exc:
            print(f"decision failed for message {record['messageId']}: {type(exc).__name__}")
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
