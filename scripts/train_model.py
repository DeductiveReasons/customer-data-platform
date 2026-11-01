"""Trains the next best action model (LightGBM) and image theme model on a randomized campaign.

Requires: pip install -r requirements-train.txt
Usage: python scripts/train_model.py [people]
"""
import json
import os
import random
import sys
from datetime import datetime, timezone

import lightgbm as lgb
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
from sklearn.model_selection import GroupShuffleSplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from cdp import assets, bandit, content, nba  # noqa: E402
from journey import RandomPolicyRng, run  # noqa: E402
from sample_data import generate  # noqa: E402

PARAMS = {
    "objective": "binary",
    "metric": "binary_logloss",
    "learning_rate": 0.05,
    "num_leaves": 15,
    "max_depth": 5,
    "min_data_in_leaf": 200,
    "feature_fraction": 0.85,
    "bagging_fraction": 0.85,
    "bagging_freq": 1,
    "lambda_l2": 5.0,
    "seed": 7,
    "verbosity": -1,
}
MAX_TREES = 300
MIN_AUC = 0.60


def uniform_model():
    # Scoring every asset the same lets the random policy try all of them.
    zeros = [0.0] * len(nba.FEATURES)
    return {
        "type": "uniform",
        "version": "untrained",
        "features": nba.FEATURES,
        "means": zeros,
        "stds": [1.0] * len(nba.FEATURES),
        "actions": {a: {"intercept": 0.0, "weights": list(zeros)} for a in assets.SIGNUP_ASSETS},
        "content": content.uniform_model(),
    }


def gbm_matrix(records):
    return np.array(
        [[nba.gbm_row(record["features"], record["asset_id"])[index] for index in range(len(nba.GBM_FEATURES))] for record in records],
        dtype=np.float32,
    )


def _flatten(tree):
    """Convert one LightGBM tree to flat arrays for fast scoring without any dependencies."""
    features, thresholds, lefts, rights, default_left, values = [], [], [], [], [], []

    def visit(node):
        index = len(features)
        features.append(-1)
        thresholds.append(0.0)
        lefts.append(-1)
        rights.append(-1)
        default_left.append(0)
        values.append(0.0)
        if "leaf_value" in node:
            values[index] = round(node["leaf_value"], 6)
            return index
        if node["decision_type"] != "<=":
            raise ValueError("only numeric splits are supported")
        features[index] = node["split_feature"]
        thresholds[index] = round(node["threshold"], 6)
        default_left[index] = 1 if node.get("default_left") else 0
        lefts[index] = visit(node["left_child"])
        rights[index] = visit(node["right_child"])
        return index

    visit(tree["tree_structure"])
    return {"f": features, "t": thresholds, "l": lefts, "r": rights, "d": default_left, "v": values}


def export_gbm(booster, version):
    dump = booster.dump_model(num_iteration=booster.best_iteration or -1)
    return {
        "type": "gbm",
        "version": version,
        "features": nba.GBM_FEATURES,
        "trees": [_flatten(tree) for tree in dump["tree_info"]],
    }


def fit_gbm(train_x, train_y, valid_x, valid_y):
    booster = lgb.train(
        PARAMS,
        lgb.Dataset(train_x, label=train_y, feature_name=nba.GBM_FEATURES),
        num_boost_round=MAX_TREES,
        valid_sets=[lgb.Dataset(valid_x, label=valid_y, feature_name=nba.GBM_FEATURES)],
        callbacks=[lgb.early_stopping(25, verbose=False)],
    )
    return booster


def calibration(labels, predicted, bands=10):
    order = np.argsort(predicted)
    rows = []
    for part in np.array_split(order, bands):
        rows.append(
            {
                "predicted": round(float(predicted[part].mean()), 4),
                "actual": round(float(labels[part].mean()), 4),
                "rows": int(len(part)),
            }
        )
    return rows


def metrics(labels, predicted):
    return {
        "auc": round(float(roc_auc_score(labels, predicted)), 4),
        "log_loss": round(float(log_loss(labels, predicted)), 5),
        "brier": round(float(brier_score_loss(labels, predicted)), 5),
    }


def split_by_person(records):
    """Keep each person's messages in one split, so test results reflect unseen people."""
    groups = np.array([record["customer_id"] for record in records])
    positions = np.arange(len(records))
    train_valid, test = next(GroupShuffleSplit(n_splits=1, test_size=0.2, random_state=7).split(positions, groups=groups))
    train, valid = next(
        GroupShuffleSplit(n_splits=1, test_size=0.125, random_state=8).split(train_valid, groups=groups[train_valid])
    )
    return train_valid[train], train_valid[valid], test


def fit_logistic(x, y):
    means = x.mean(axis=0)
    stds = x.std(axis=0)
    stds[stds == 0] = 1.0
    model = LogisticRegression(C=1.0, max_iter=400)
    model.fit((x - means) / stds, y)
    return means, stds, float(model.intercept_[0]), model.coef_[0]


def fit_baseline(records, indexes):
    """The previous approach: one logistic regression per asset. Kept to measure the improvement."""
    matrix = np.array([[record["features"].get(name, 0.0) or 0.0 for name in nba.FEATURES] for record in records], dtype=float)
    labels = np.array([int(record["registered"]) for record in records])
    asset_ids = np.array([record["asset_id"] for record in records])
    actions = {}
    for asset_id in assets.SIGNUP_ASSETS:
        selected = indexes[asset_ids[indexes] == asset_id]
        means, stds, intercept, weights = fit_logistic(matrix[selected], labels[selected])
        actions[asset_id] = {
            "intercept": round(intercept, 6),
            "weights": [round(float(w), 6) for w in weights],
            "means": [round(float(v), 6) for v in means],
            "stds": [round(float(v), 6) for v in stds],
        }
    return {
        "type": "logistic",
        "version": "baseline-logistic",
        "features": nba.FEATURES,
        "means": [0.0] * len(nba.FEATURES),
        "stds": [1.0] * len(nba.FEATURES),
        "actions": actions,
    }


def fit_content(records, indexes, version):
    matrix = np.array([[record["features"].get(name, 0.0) or 0.0 for name in content.FEATURES] for record in records], dtype=float)
    labels = np.array([int(record["clicked"]) for record in records])
    themes = np.array([record["image_theme"] for record in records])
    actions = {}
    report = {}
    for theme in content.THEMES:
        selected = indexes[themes[indexes] == theme]
        means, stds, intercept, weights = fit_logistic(matrix[selected], labels[selected])
        scored = (matrix[selected] - means) / stds @ weights + intercept
        report[theme] = round(float(roc_auc_score(labels[selected], scored)), 4)
        actions[theme] = {
            "intercept": round(intercept, 6),
            "weights": [round(float(w), 6) for w in weights],
            "means": [round(float(v), 6) for v in means],
            "stds": [round(float(v), 6) for v in stds],
        }
    model = {
        "version": version,
        "features": content.FEATURES,
        "means": [0.0] * len(content.FEATURES),
        "stds": [1.0] * len(content.FEATURES),
        "actions": actions,
    }
    return model, report


def save(name, data, indent=None):
    with open(os.path.join(HERE, "..", "src", "cdp", name), "w") as output:
        json.dump(data, output, indent=indent, separators=None if indent else (",", ":"))
        output.write("\n")


def main():
    people = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
    version = "gbm-" + datetime.now(timezone.utc).strftime("%Y%m%d")
    # This campaign picks offers and images at random, so the training data is not shaped by an earlier model.
    records, totals = run(generate(people, seed=7), uniform_model(), RandomPolicyRng(1))
    print(f"training campaign: {totals}")
    train, valid, test = split_by_person(records)
    print(f"rows: train {len(train)}, validation {len(valid)}, test {len(test)}")

    matrix = gbm_matrix(records)
    labels = np.array([int(record["registered"]) for record in records])
    booster = fit_gbm(matrix[train], labels[train], matrix[valid], labels[valid])
    predicted = booster.predict(matrix[test], num_iteration=booster.best_iteration)
    gbm_metrics = metrics(labels[test], predicted)

    model = export_gbm(booster, version)
    python_scores = np.array(
        [nba.score_gbm(model, nba.gbm_row(records[index]["features"], records[index]["asset_id"])) for index in test[:2000]]
    )
    largest_gap = float(np.abs(python_scores - predicted[:2000]).max())
    if largest_gap > 0.002:
        raise SystemExit(f"exported model does not match LightGBM: gap {largest_gap}")

    baseline = fit_baseline(records, np.concatenate([train, valid]))
    baseline_scores = np.array(
        [nba.propensity(baseline, records[index]["asset_id"], records[index]["features"]) for index in test]
    )
    baseline_metrics = metrics(labels[test], baseline_scores)
    if gbm_metrics["auc"] < MIN_AUC:
        raise SystemExit(f"model did not reach the minimum AUC of {MIN_AUC}: {gbm_metrics}")

    content_model, content_report = fit_content(records, np.concatenate([train, valid]), version)
    gain = booster.feature_importance(importance_type="gain", iteration=booster.best_iteration)
    total_gain = float(gain.sum()) or 1.0
    importance = sorted(
        ({"feature": name, "share": round(float(value) / total_gain, 4)} for name, value in zip(nba.GBM_FEATURES, gain)),
        key=lambda item: item["share"],
        reverse=True,
    )
    card = {
        "version": version,
        "algorithm": "LightGBM gradient-boosted decision trees",
        "target": "Registered after receiving this message",
        "training_people": people,
        "training_rows": {"train": int(len(train)), "validation": int(len(valid)), "test": int(len(test))},
        "registration_rate": round(float(labels.mean()), 4),
        "trees": len(model["trees"]),
        "parameters": {k: PARAMS[k] for k in ("learning_rate", "num_leaves", "max_depth", "min_data_in_leaf")},
        "test_metrics": gbm_metrics,
        "baseline_metrics": baseline_metrics,
        "export_check_largest_gap": round(largest_gap, 6),
        "calibration": calibration(labels[test], predicted),
        "importance": importance[:15],
        "content_theme_auc": content_report,
        "training_data": "Simulated campaign with random offers and images. Not real customer data.",
    }
    print(json.dumps({k: card[k] for k in ("test_metrics", "baseline_metrics", "trees", "export_check_largest_gap")}))
    print("top features:", [(i["feature"], i["share"]) for i in importance[:8]])
    print("content AUC:", content_report)
    save("nba_model.json", model)
    # The live image choice is a bandit that starts with no knowledge and learns from its own results.
    save("content_model.json", bandit.to_model(bandit.new_stats(content.THEMES), "bandit-start"))
    save("model_card.json", card, indent=2)
    baseline_path = os.path.join(HERE, "baseline_model.json")
    with open(baseline_path, "w") as output:
        json.dump({**baseline, "content": content_model}, output)
    random.seed(0)


if __name__ == "__main__":
    main()
