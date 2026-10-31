"""Step Functions task: applies consent and routing rules to choose destinations."""
import json
import os

# Each destination needs consent for its purpose. Missing consent means no delivery.
DESTINATION_PURPOSE = {
    "braze": "marketing",
    "crm": "crm",
    "other_apis": "analytics",
}


def evaluate(resolved, enabled, queues):
    consent = resolved["profile"]["consent"]
    deliveries = []
    for destination in enabled:
        if consent.get(DESTINATION_PURPOSE[destination]) is True:
            deliveries.append({"destination": destination, "queue_url": queues[destination], **resolved})
    return deliveries


def handler(event, _context):
    enabled = json.loads(os.environ["ENABLED_DESTINATIONS"])
    queues = json.loads(os.environ["DESTINATION_QUEUES"])
    return {"deliveries": evaluate(event, enabled, queues)}
