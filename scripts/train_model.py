"""Trains the starting model from a randomized synthetic campaign."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from cdp import nba, training  # noqa: E402
from journey import RandomPolicyRng, run  # noqa: E402
from sample_data import generate  # noqa: E402


def uniform_model():
    # Scoring every asset the same lets the random policy try all of them.
    return {
        "version": "untrained",
        "features": nba.FEATURES,
        "means": [0.0] * len(nba.FEATURES),
        "stds": [1.0] * len(nba.FEATURES),
        "actions": {a: {"intercept": 0.0, "weights": [0.0] * len(nba.FEATURES)} for a in nba.assets.SIGNUP_ASSETS},
    }


def main():
    records, totals = run(generate(10_000, seed=42), uniform_model(), RandomPolicyRng(1))
    model, report = training.train(records, nba.FEATURES, "synthetic-v1")
    print("random campaign:", totals)
    print(json.dumps(report, indent=2))
    if model is None:
        raise SystemExit("training produced no model")
    path = os.path.join(HERE, "..", "src", "cdp", "nba_model.json")
    with open(path, "w") as output:
        json.dump(model, output, indent=2)
        output.write("\n")


if __name__ == "__main__":
    main()
