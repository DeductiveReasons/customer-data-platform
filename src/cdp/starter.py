"""SQS handler: starts one Step Functions execution per event."""
import json
import os

import boto3

_sfn = None


def _client():
    global _sfn
    if _sfn is None:
        _sfn = boto3.client("stepfunctions")
    return _sfn


def handler(event, _context):
    failures = []
    client = _client()
    for record in event["Records"]:
        try:
            body = json.loads(record["body"])
            # Naming the execution after event_id makes duplicate deliveries a no-op.
            client.start_execution(
                stateMachineArn=os.environ["STATE_MACHINE_ARN"],
                name=body["event_id"],
                input=record["body"],
            )
        except client.exceptions.ExecutionAlreadyExists:
            continue
        except Exception as exc:
            print(f"start failed for message {record['messageId']}: {type(exc).__name__}")
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
