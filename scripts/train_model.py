"""Trains one logistic regression per action on the synthetic data, using only the standard library."""
import json
import math
import os
import random
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sample_data import generate  # noqa: E402

FEATURES = [
    "age",
    "tenure_months",
    "orders_12m",
    "spend_12m",
    "days_since_purchase",
    "email_open_rate",
    "loyalty_member",
]
ACTIONS = ["win_back", "replenish", "loyalty_upgrade"]
EPOCHS = 400
LEARNING_RATE = 0.3


def sigmoid(value):
    return 1 / (1 + math.exp(-max(min(value, 30), -30)))


def fit(rows, labels):
    weights = [0.0] * len(rows[0])
    intercept = 0.0
    count = len(rows)
    for _ in range(EPOCHS):
        gradient = [0.0] * len(weights)
        gradient_intercept = 0.0
        for row, label in zip(rows, labels):
            error = sigmoid(intercept + sum(w * x for w, x in zip(weights, row))) - label
            gradient_intercept += error
            for i, x in enumerate(row):
                gradient[i] += error * x
        intercept -= LEARNING_RATE * gradient_intercept / count
        weights = [w - LEARNING_RATE * g / count for w, g in zip(weights, gradient)]
    return intercept, weights


def auc(labels, scores):
    ranked = sorted(zip(scores, labels))
    positives = sum(labels)
    negatives = len(labels) - positives
    rank_sum = sum(rank for rank, (_, label) in enumerate(ranked, start=1) if label)
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def main():
    data = generate()
    random.Random(7).shuffle(data)
    split = int(len(data) * 0.8)

    raw = [[float(entry["person"][name]) for name in FEATURES] for entry, _ in data]
    train_raw = raw[:split]
    means = [sum(column) / len(column) for column in zip(*train_raw)]
    stds = [
        math.sqrt(sum((x - mean) ** 2 for x in column) / len(column)) or 1.0
        for column, mean in zip(zip(*train_raw), means)
    ]
    scaled = [[(x - m) / s for x, m, s in zip(row, means, stds)] for row in raw]

    model = {
        "version": "synthetic-v1",
        "features": FEATURES,
        "means": [round(v, 6) for v in means],
        "stds": [round(v, 6) for v in stds],
        "actions": {},
    }
    for action in ACTIONS:
        labels = [outcome[action] for _, outcome in data]
        intercept, weights = fit(scaled[:split], labels[:split])
        scores = [intercept + sum(w * x for w, x in zip(weights, row)) for row in scaled[split:]]
        model["actions"][action] = {
            "intercept": round(intercept, 6),
            "weights": [round(w, 6) for w in weights],
            "holdout_auc": round(auc(labels[split:], scores), 4),
            "response_rate": round(sum(labels) / len(labels), 4),
        }
        print(action, model["actions"][action]["holdout_auc"], model["actions"][action]["response_rate"])

    path = os.path.join(ROOT, "src", "cdp", "nba_model.json")
    with open(path, "w") as output:
        json.dump(model, output, indent=2)
        output.write("\n")


if __name__ == "__main__":
    main()
