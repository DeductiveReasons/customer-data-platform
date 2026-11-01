"""Replaces all customer profiles with a synthetic population, written directly to DynamoDB.

This is much cheaper than sending bulk events through the API and Step Functions.
Previous results are moved under previous-runs/ in S3.

Usage: python scripts/load_population.py <profiles_table> <bucket> <run_name> [people] [--replace]
"""
import concurrent.futures
import os
import re
import sys

import boto3

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from reset_campaign import archive_results  # noqa: E402
from cdp.daily import CLOCK_KEY  # noqa: E402
from sample_data import LEADS_RECEIVED, generate, lead_items  # noqa: E402

CHUNK = 5000
LEAD_SEED = 2026  # Not used to train or evaluate the model, so these are all new leads.
WORKERS = 12


def _write(table_name, items):
    table = boto3.resource("dynamodb").Table(table_name)
    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)
    return len(items)


def _delete_segment(table_name, segment, segments):
    table = boto3.resource("dynamodb").Table(table_name)
    removed = 0
    kwargs = {"ProjectionExpression": "pk, sk", "Segment": segment, "TotalSegments": segments}
    with table.batch_writer() as batch:
        while True:
            page = table.scan(**kwargs)
            for item in page["Items"]:
                batch.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
                removed += 1
            if "LastEvaluatedKey" not in page:
                return removed
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def main():
    arguments = [a for a in sys.argv[1:] if a != "--replace"]
    if len(arguments) < 3 or not re.fullmatch(r"[a-z0-9-]+", arguments[2]):
        sys.exit(__doc__)
    table_name, bucket, run_name = arguments[:3]
    count = int(arguments[3]) if len(arguments) > 3 else 100_000

    if "--replace" in sys.argv:
        print(f"archived {archive_results(bucket, run_name)} result files", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            removed = sum(pool.map(lambda s: _delete_segment(table_name, s, WORKERS), range(WORKERS)))
        print(f"removed {removed} existing records", flush=True)

    items = []
    for entry in generate(count, seed=LEAD_SEED):
        items.extend(lead_items(entry))
    chunks = [items[start : start + CHUNK] for start in range(0, len(items), CHUNK)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        written = sum(pool.map(lambda chunk: _write(table_name, chunk), chunks))
    # Day -1 means the leads have arrived and the first daily run has not started.
    boto3.resource("dynamodb").Table(table_name).put_item(
        Item={**CLOCK_KEY, "day": -1, "start_date": LEADS_RECEIVED}
    )
    print(f"wrote {written} records for {count} new leads", flush=True)


if __name__ == "__main__":
    main()
