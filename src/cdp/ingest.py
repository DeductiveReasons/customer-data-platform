"""API Gateway handler: validates events and publishes them to EventBridge."""
import json
import os

import boto3

from cdp.validation import ValidationError, parse_body

PUT_EVENTS_LIMIT = 10
_events = None


def _client():
    global _events
    if _events is None:
        _events = boto3.client("events")
    return _events


def _response(status, body):
    return {"statusCode": status, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}


def handler(event, _context):
    try:
        events = parse_body(event.get("body"))
    except ValidationError as exc:
        return _response(400, {"error": str(exc)})

    failed = 0
    for start in range(0, len(events), PUT_EVENTS_LIMIT):
        entries = [
            {
                "Source": "cdp.ingest",
                "DetailType": item["type"],
                "Detail": json.dumps(item),
                "EventBusName": os.environ["EVENT_BUS_NAME"],
            }
            for item in events[start : start + PUT_EVENTS_LIMIT]
        ]
        failed += _client().put_events(Entries=entries).get("FailedEntryCount", 0)

    # Downstream processing is idempotent on event_id, so clients can safely resend the request.
    if failed:
        return _response(503, {"error": "some events were not accepted, retry the request", "failed": failed})
    return _response(202, {"accepted": len(events), "event_ids": [item["event_id"] for item in events]})
