"""Sends the synthetic population to the deployed ingestion API.

Usage: python scripts/ingest_sample.py <events_url> <api_key> [count]
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sample_data import generate, to_events  # noqa: E402

BATCH_SIZE = 100
RETRIES = 6


def post_batch(url, api_key, events):
    request = urllib.request.Request(
        url,
        data=json.dumps({"batch": events}).encode(),
        headers={"Content-Type": "application/json", "x-api-key": api_key},
        method="POST",
    )
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=30):
                return
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == RETRIES - 1:
                raise
        except urllib.error.URLError:
            if attempt == RETRIES - 1:
                raise
        time.sleep(2**attempt)


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    url, api_key = sys.argv[1], sys.argv[2]
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 10_000

    pairs = [to_events(entry) for entry in generate(count)]
    # Profiles go first so identity links exist before consent events arrive.
    for kind, events in (("identify", [p[0] for p in pairs]), ("consent", [p[1] for p in pairs])):
        for start in range(0, len(events), BATCH_SIZE):
            post_batch(url, api_key, events[start : start + BATCH_SIZE])
        print(f"sent {len(events)} {kind} events")
        if kind == "identify":
            time.sleep(60)


if __name__ == "__main__":
    main()
