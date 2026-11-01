"""Replaces all customer profiles with a synthetic population, written directly to DynamoDB.

This is much cheaper than sending bulk events through the API and Step Functions.
Previous results are moved under previous-runs/ in S3.

Usage: python scripts/load_population.py <profiles_table> <bucket> <run_name> [people] [--replace]
"""
import concurrent.futures
import json
import os
import re
import sys
from decimal import Decimal

import boto3

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from cdp import identity  # noqa: E402
from reset_campaign import archive_results  # noqa: E402
from sample_data import generate, traits_of  # noqa: E402

CHUNK = 5000
WORKERS = 12


def items_for(entry):
    person = entry["person"]
    customer_id = person["user_id"]
    profile = {"pk": f"CUSTOMER#{customer_id}", "sk": "PROFILE", "updated_at": "bulk-load"}
    traits = json.loads(json.dumps(traits_of(entry)), parse_float=Decimal)
    profile.update({f"t_{name}": value for name, value in traits.items()})
    profile.update({f"c_{name}": value for name, value in entry["consent"].items()})
    links = [
        {"pk": f"ID#{identifier}", "sk": "LINK", "customer_id": customer_id}
        for identifier in identity.identifiers({"user_id": customer_id, "email": person["email"]})
    ]
    return [profile] + links


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
    for entry in generate(count, seed=42):
        items.extend(items_for(entry))
    chunks = [items[start : start + CHUNK] for start in range(0, len(items), CHUNK)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        written = sum(pool.map(lambda chunk: _write(table_name, chunk), chunks))
    print(f"wrote {written} records for {count} people", flush=True)


if __name__ == "__main__":
    main()
