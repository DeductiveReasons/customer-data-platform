"""Logistic regression training using only the standard library, so it can also run in Lambda."""
import math
import random

MIN_ROWS = 500
MIN_POSITIVES = 20
MIN_AUC = 0.55
EPOCHS = 150
LEARNING_RATE = 0.5


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
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        return 0.5
    # Tied scores share their average rank.
    ordered = sorted(zip(scores, labels))
    rank_sum = 0.0
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1][0] == ordered[index][0]:
            end += 1
        average_rank = (index + end) / 2 + 1
        rank_sum += average_rank * sum(label for _, label in ordered[index : end + 1])
        index = end + 1
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def _scaling(rows):
    columns = list(zip(*rows))
    means = [sum(column) / len(column) for column in columns]
    stds = [
        math.sqrt(sum((x - mean) ** 2 for x in column) / len(column)) or 1.0
        for column, mean in zip(columns, means)
    ]
    return means, stds


def train(records, features, version, previous=None, seed=7, key="asset_id", label="registered"):
    """Train one model per key value from records of {key, features, label}.

    Assets without enough data or signal keep their previous weights.
    Returns (model, report), where model is None if no asset could be trained.
    """
    records = [r for r in records if r.get(key)]
    random.Random(seed).shuffle(records)
    matrix = [[float(r["features"].get(name, 0)) for name in features] for r in records]
    if not matrix:
        return None, {}
    means, stds = _scaling(matrix)
    scaled = [[(x - m) / s for x, m, s in zip(row, means, stds)] for row in matrix]

    by_asset = {}
    for record, row in zip(records, scaled):
        by_asset.setdefault(record[key], []).append((row, int(bool(record[label]))))

    actions = {}
    report = {}
    for asset_id, data in by_asset.items():
        labels = [label for _, label in data]
        if len(data) < MIN_ROWS or sum(labels) < MIN_POSITIVES:
            report[asset_id] = {"status": "not enough data", "rows": len(data), "registered": sum(labels)}
            continue
        split = int(len(data) * 0.8)
        intercept, weights = fit([row for row, _ in data[:split]], labels[:split])
        scores = [intercept + sum(w * x for w, x in zip(weights, row)) for row, _ in data[split:]]
        holdout_auc = auc(labels[split:], scores)
        report[asset_id] = {
            "status": "trained" if holdout_auc >= MIN_AUC else "weak signal",
            "rows": len(data),
            "registered": sum(labels),
            "holdout_auc": round(holdout_auc, 4),
        }
        if holdout_auc >= MIN_AUC:
            actions[asset_id] = {
                "intercept": round(intercept, 6),
                "weights": [round(w, 6) for w in weights],
                "holdout_auc": round(holdout_auc, 4),
            }

    if not actions:
        return None, report
    model = {
        "version": version,
        "features": features,
        "means": [round(v, 6) for v in means],
        "stds": [round(v, 6) for v in stds],
        "actions": actions,
    }
    # Previous weights use the previous scaling, so they are carried with it.
    if previous:
        for asset_id, old in previous["actions"].items():
            if asset_id not in actions and previous["features"] == features:
                actions[asset_id] = {**old, "means": old.get("means", previous["means"]), "stds": old.get("stds", previous["stds"])}
    return model, report
