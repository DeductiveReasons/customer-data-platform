"""Dummy email and direct mail service. It stores requests and simulates engagement, and never sends anything."""
import base64
import hmac
import json
import os
import random
import time
from decimal import Decimal

import boto3

from cdp import simulator
from cdp.assets import ONBOARDING

ROUTE_CHANNELS = {"POST /email": "email", "POST /direct-mail": "direct_mail"}
REQUIRED_FIELDS = ("message_id", "customer_id", "touch", "channel", "asset_id", "content_html", "recipient", "features")
REQUIRED_RECIPIENT = {
    "email": ("email",),
    "direct_mail": ("name", "address", "city", "state", "postal_code"),
}
RETENTION_DAYS = 7
MAX_FEEDBACK_DELAY_SECONDS = 30
_table = None
_token = None
_sqs = None


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def _get_token():
    global _token
    if _token is None:
        secret = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["SECRET_ARN"])
        _token = secret["SecretString"]
    return _token


def _get_sqs():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs")
    return _sqs


def _response(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body, default=str)}


def _authorized(event):
    header = (event.get("headers") or {}).get("authorization", "")
    return hmac.compare_digest(header.encode(), f"Bearer {_get_token()}".encode())


def validate(channel, message):
    if not isinstance(message, dict):
        return "body must be an object"
    missing = [field for field in REQUIRED_FIELDS if not message.get(field)]
    if missing:
        return f"missing fields: {', '.join(missing)}"
    if message["channel"] != channel:
        return f"channel must be {channel}"
    recipient = message["recipient"]
    if not isinstance(recipient, dict):
        return "recipient must be an object"
    missing = [field for field in REQUIRED_RECIPIENT[channel] if not recipient.get(field)]
    if missing:
        return f"missing recipient fields: {', '.join(missing)}"
    if not isinstance(message["content_html"], str) or "<html" not in message["content_html"].lower():
        return "content_html must be an HTML document"
    if message["asset_id"] != ONBOARDING and message["asset_id"] not in simulator.ASSET_EFFECT:
        return "unknown asset_id"
    return None


def _send_feedback(message, outcome):
    fields = ("message_id", "customer_id", "touch", "channel", "asset_id", "features", "model_version", "selection")
    feedback = {field: message.get(field) for field in fields}
    _get_sqs().send_message(
        QueueUrl=os.environ["FEEDBACK_QUEUE_URL"],
        MessageBody=json.dumps({**feedback, "outcome": outcome}),
        DelaySeconds=random.randint(1, MAX_FEEDBACK_DELAY_SECONDS),
    )


def _receive(event, channel):
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    try:
        message = json.loads(body)
    except ValueError:
        return _response(400, {"error": "body must be valid JSON"})

    error = validate(channel, message)
    if error:
        return _response(400, {"error": error})

    # Onboarding is sent after the sign-up goal is met, so there is no outcome left to simulate.
    outcome = None
    if message["asset_id"] != ONBOARDING:
        outcome = simulator.outcome_for(message["message_id"], message["asset_id"], message["features"])

    stored = json.loads(json.dumps({**message, "outcome": outcome}), parse_float=Decimal)
    # message_id is the key, so a retried request overwrites its earlier copy.
    _get_table().put_item(Item={**stored, "expires_at": int(time.time()) + RETENTION_DAYS * 86400})
    if outcome is not None:
        _send_feedback(message, outcome)
    return _response(202, {"status": "accepted", "message_id": message["message_id"], "sent": False})


def _list_messages():
    items = _get_table().scan(Limit=20)["Items"]
    return _response(200, {"messages": items})


def handler(event, _context):
    if not _authorized(event):
        return _response(401, {"error": "unauthorized"})
    route = event.get("routeKey")
    if route in ROUTE_CHANNELS:
        return _receive(event, ROUTE_CHANNELS[route])
    if route == "GET /messages":
        return _list_messages()
    return _response(404, {"error": "not found"})
