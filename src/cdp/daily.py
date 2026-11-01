"""Advances the campaign one simulated day: adds new leads, delivers due mail and queues who is due."""
import json
import os
import random
from datetime import date, timedelta

import boto3
from botocore.exceptions import ClientError

from cdp import nba_scan, sample_data

CLOCK_KEY = {"pk": "CONFIG#simulation", "sk": "CLOCK"}
DEFAULT_START = "2026-11-02"
NEW_LEAD_SEED = 1_000_000


def get_clock(table):
    item = table.get_item(Key=CLOCK_KEY, ConsistentRead=True).get("Item") or {}
    return {"day": int(item.get("day", -1)), "start_date": item.get("start_date", DEFAULT_START)}


def sim_date(start_date, day):
    return (date.fromisoformat(start_date) + timedelta(days=day)).isoformat()


def add_new_leads(table, day, received, average):
    """Add about the average number of new leads. The seed makes each day repeatable."""
    if average <= 0:
        return 0
    seed = NEW_LEAD_SEED + day
    count = random.Random(seed).randint(int(average * 0.85), int(average * 1.15))
    with table.batch_writer() as batch:
        for entry in sample_data.generate(count, seed=seed, received=received):
            for item in sample_data.lead_items(entry):
                batch.put_item(Item=item)
    return count


def release_mail(table, sqs, queue_url, day):
    """Direct mail outcomes become known only on the day the mail is delivered."""
    released = 0
    kwargs = {
        "KeyConditionExpression": "pk = :pk",
        "ExpressionAttributeValues": {":pk": f"MAILDUE#{day}"},
    }
    while True:
        page = table.query(**kwargs)
        for item in page["Items"]:
            sqs.send_message(QueueUrl=queue_url, MessageBody=item["feedback"])
            table.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
            released += 1
        if "LastEvaluatedKey" not in page:
            return released
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def release_registrations(table, day):
    """Mark people who register today after engaging earlier."""
    released = 0
    kwargs = {
        "KeyConditionExpression": "pk = :pk",
        "ExpressionAttributeValues": {":pk": f"REGDUE#{day}"},
    }
    while True:
        page = table.query(**kwargs)
        for item in page["Items"]:
            try:
                table.update_item(
                    Key={"pk": f"CUSTOMER#{item['customer_id']}", "sk": "PROFILE"},
                    UpdateExpression="SET s_registered = :yes, s_registered_day = :day, s_next_due = :due",
                    ConditionExpression="attribute_not_exists(s_registered)",
                    ExpressionAttributeValues={":yes": True, ":day": day, ":due": day + 1},
                )
                released += 1
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
            table.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
        if "LastEvaluatedKey" not in page:
            return released
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def handler(_event, _context):
    table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    sqs = boto3.client("sqs")
    clock = get_clock(table)
    day = clock["day"] + 1
    table.put_item(Item={**CLOCK_KEY, "day": day, "start_date": clock["start_date"]})
    today = sim_date(clock["start_date"], day)
    new_leads = add_new_leads(table, day, today, int(os.environ.get("DAILY_NEW_LEADS", "0")))
    released = release_mail(table, sqs, os.environ["FEEDBACK_QUEUE_URL"], day)
    registered = release_registrations(table, day)
    queued = nba_scan.queue_due(table, sqs, os.environ["DECISION_QUEUE_URL"], day)
    result = {"day": day, "date": today, "new_leads": new_leads, "mail_delivered": released, "registered": registered, "queued": queued}
    print(json.dumps(result))
    return result
