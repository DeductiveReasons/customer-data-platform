"""SQS handler: delivers an event to one destination. Failures retry, then go to its DLQ."""
import json
import os
import urllib.request

import boto3

_secret = None


def _get_secret():
    global _secret
    if _secret is None:
        response = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["SECRET_ARN"])
        _secret = json.loads(response["SecretString"])
    return _secret


def _post(url, token, payload):
    if not url.startswith("https://"):
        raise ValueError("destination URL must use https")
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    # urlopen raises HTTPError on 4xx and 5xx responses, which fails the SQS message.
    with urllib.request.urlopen(request, timeout=10):
        pass


def braze_payload(message):
    event = message["event"]
    timestamp = event.get("timestamp") or event["received_at"]
    payload = {
        "attributes": [{"external_id": message["customer_id"], **message["profile"]["traits"]}],
    }
    if event["type"] == "track":
        payload["events"] = [
            {
                "external_id": message["customer_id"],
                "name": event["event"],
                "time": timestamp,
                "properties": event.get("properties", {}),
            }
        ]
    return payload


def _send(message, secret):
    if message["destination"] == "braze":
        endpoint = secret["rest_endpoint"].rstrip("/")
        _post(f"{endpoint}/users/track", secret["api_key"], braze_payload(message))
    else:
        body = {k: message[k] for k in ("customer_id", "event", "profile")}
        _post(secret["url"], secret["token"], body)


def handler(event, _context):
    failures = []
    for record in event["Records"]:
        try:
            _send(json.loads(record["body"]), _get_secret())
        except Exception as exc:
            print(f"delivery failed for message {record['messageId']}: {type(exc).__name__}")
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
