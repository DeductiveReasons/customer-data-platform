"""Restarts everyone's journey and archives the previous run's results.

Profiles, traits and consent are kept, so anyone who unsubscribed stays unsubscribed.
Usage: python scripts/reset_campaign.py <profiles_table> <bucket> <run_name>
"""
import concurrent.futures
import re
import sys

import boto3
from boto3.dynamodb.conditions import Attr

STATE_ATTRIBUTES = ("touches", "opens", "clicks", "registered", "onboarded", "last_asset", "last_image", "last_contact")
RESULT_PREFIXES = ("history/", "feedback/")


def archive_results(bucket, run_name):
    s3 = boto3.client("s3")
    moved = 0
    for prefix in RESULT_PREFIXES:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                key = item["Key"]
                s3.copy_object(
                    Bucket=bucket, Key=f"previous-runs/{run_name}/{key}", CopySource={"Bucket": bucket, "Key": key}
                )
                s3.delete_object(Bucket=bucket, Key=key)
                moved += 1
    return moved


def reset_profiles(table_name):
    table = boto3.resource("dynamodb").Table(table_name)
    profiles = []
    ledger = []
    kwargs = {
        "FilterExpression": Attr("sk").eq("PROFILE") | Attr("sk").begins_with("NBA#"),
        "ProjectionExpression": "pk, sk",
    }
    while True:
        page = table.scan(**kwargs)
        for item in page["Items"]:
            (profiles if item["sk"] == "PROFILE" else ledger).append(item)
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    with table.batch_writer() as batch:
        for key in ledger:
            batch.delete_item(Key={"pk": key["pk"], "sk": key["sk"]})

    remove = "REMOVE " + ", ".join(f"s_{name}" for name in STATE_ATTRIBUTES)

    def clear(key):
        table.update_item(Key={"pk": key["pk"], "sk": key["sk"]}, UpdateExpression=remove)

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(clear, profiles))
    return len(profiles), len(ledger)


def main():
    if len(sys.argv) != 4 or not re.fullmatch(r"[a-z0-9-]+", sys.argv[3]):
        sys.exit(__doc__)
    table_name, bucket, run_name = sys.argv[1:]
    print(f"archived {archive_results(bucket, run_name)} result files")
    profiles, ledger = reset_profiles(table_name)
    print(f"reset {profiles} profiles and removed {ledger} message records")


if __name__ == "__main__":
    main()
