"""Runs all synthetic people through decisions and the dummy receiver's validation, without AWS."""
import collections
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.modules.setdefault("boto3", types.ModuleType("boto3"))

from cdp import nba, receiver  # noqa: E402
from sample_data import generate  # noqa: E402


def main():
    people = generate()
    totals = collections.Counter()
    pairs = collections.Counter()
    sample = {}
    for entry, _ in people:
        person = entry["person"]
        traits = {k: v for k, v in person.items() if k != "user_id"}
        decision = nba.decide(person["user_id"], traits, entry["consent"])
        if decision is None:
            totals["no_contact"] += 1
            continue
        message = {**decision, "message_id": f"{person['user_id']}-test"}
        error = receiver.validate(decision["channel"], json.loads(json.dumps(message)))
        if error:
            raise SystemExit(f"receiver rejected {person['user_id']}: {error}")
        if not entry["consent"]["marketing"] or not entry["consent"][decision["channel"]]:
            raise SystemExit(f"consent violation for {person['user_id']}")
        totals[decision["channel"]] += 1
        pairs[(decision["action"], decision["channel"])] += 1
        sample.setdefault(decision["channel"], message)

    print(f"people: {len(people)}")
    for name, count in totals.most_common():
        print(f"{name}: {count}")
    for (action, channel), count in sorted(pairs.items()):
        print(f"{action} via {channel}: {count}")
    for channel, message in sample.items():
        print(f"\nSample {channel} request:\n{json.dumps(message, indent=2)}")


if __name__ == "__main__":
    main()
