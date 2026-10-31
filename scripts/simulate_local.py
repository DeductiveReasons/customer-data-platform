"""Compares campaign policies through the full feedback loop, without AWS.

Each policy runs on the same people, who were not used to train the starting model.
"""
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from cdp import content, nba, training  # noqa: E402
from journey import RandomPolicyRng, run  # noqa: E402
from sample_data import generate  # noqa: E402
from train_model import uniform_model  # noqa: E402


def show(name, totals, people):
    cost = totals["cost"]
    registered = totals["registered"]
    per = f"${cost / registered:.2f}" if registered else "n/a"
    print(
        f"{name:<28} registered {registered:>5} ({registered / people:.1%})  "
        f"messages {totals['messages']:>6}  email {totals['email']:>6}  mail {totals['direct_mail']:>6}  "
        f"cost ${cost:>9,.2f}  cost per sign-up {per}"
    )


def main():
    people = 10_000
    population = generate(people, seed=99)

    _, random_totals = run(population, uniform_model(), RandomPolicyRng(2))
    show("Random asset and channel", random_totals, people)

    records, first = run(population, nba.BUNDLED_MODEL, random.Random(3))
    show("Starting model", first, people)

    random_content = {**nba.BUNDLED_MODEL, "content": content.uniform_model()}
    _, even = run(population, random_content, random.Random(3))
    show("Same assets, untrained images", even, people)

    # The next round trains on the feedback from the starting model's own campaign.
    retrained, report = training.train(records, nba.FEATURES, "feedback-demo", previous=nba.BUNDLED_MODEL)
    print("\nRetrain report:", json.dumps(report))
    if retrained is None:
        raise SystemExit("retrain produced no model")

    _, second = run(generate(people, seed=123), retrained, random.Random(4))
    show("Retrained, new people", second, people)
    _, baseline = run(generate(people, seed=123), nba.BUNDLED_MODEL, random.Random(4))
    show("Starting model, new people", baseline, people)


if __name__ == "__main__":
    main()
