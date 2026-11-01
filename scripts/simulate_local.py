"""Compares decision policies through the full feedback loop on new people, without AWS.

Adds the results to src/cdp/model_card.json.
Usage: python scripts/simulate_local.py [people]
"""
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from cdp import bandit, content, nba  # noqa: E402
from journey import RandomPolicyRng, run  # noqa: E402
from sample_data import generate  # noqa: E402
from train_model import uniform_model  # noqa: E402


def summary(name, totals, people):
    registered = totals["registered"]
    return {
        "policy": name,
        "people": people,
        "registered": registered,
        "registration_rate": round(registered / people, 4),
        "messages": totals["messages"],
        "email": totals["email"],
        "direct_mail": totals["direct_mail"],
        "cost": round(totals["cost"], 2),
        "cost_per_registration": round(totals["cost"] / registered, 2) if registered else None,
    }


def run_bandit(population, rng, batch=2000):
    """Run the bandit from no knowledge, updating it between batches as the live system does."""
    stats = bandit.new_stats(content.THEMES)
    model = {**nba.BUNDLED_MODEL, "content": bandit.to_model(stats, "start")}
    totals = {}
    selections = {}
    for start in range(0, len(population), batch):
        records, part = run(population[start : start + batch], model, rng)
        for key, value in part.items():
            totals[key] = totals.get(key, 0) + value
        for record in records:
            bandit.add(stats, record["image_theme"], record["features"], record["clicked"])
            group = selections.setdefault(record["content_selection"], {"messages": 0, "clicked": 0, "registered": 0})
            group["messages"] += 1
            group["clicked"] += int(record["clicked"])
            group["registered"] += int(record["registered"])
        model = {**model, "content": bandit.to_model(stats, "learning")}
    arms = {arm: {"messages": e["pulls"], "clicked": e["rewards"]} for arm, e in stats.items()}
    return totals, selections, arms


def main():
    people = int(sys.argv[1]) if len(sys.argv) > 1 else 20_000
    # These people were not used for training, and every policy sees the same people.
    population = generate(people, seed=99)
    with open(os.path.join(HERE, "baseline_model.json")) as source:
        baseline = json.load(source)
    policies = [
        ("Random offer and image", uniform_model(), RandomPolicyRng(2)),
        ("Previous logistic model", baseline, random.Random(3)),
        ("LightGBM, no image model", {**nba.BUNDLED_MODEL, "content": content.uniform_model()}, random.Random(3)),
        ("LightGBM, fixed image model", {**nba.BUNDLED_MODEL, "content": baseline["content"]}, random.Random(3)),
    ]
    results = []
    for name, model, rng in policies:
        _, totals = run(population, model, rng)
        result = summary(name, totals, people)
        results.append(result)
        print(json.dumps(result), flush=True)

    totals, selections, arms = run_bandit(population, random.Random(3))
    result = summary("LightGBM with content bandit", totals, people)
    results.append(result)
    print(json.dumps(result), flush=True)
    print("bandit selections:", json.dumps(selections), flush=True)
    print("bandit arms:", json.dumps(arms), flush=True)

    path = os.path.join(HERE, "..", "src", "cdp", "model_card.json")
    with open(path) as source:
        card = json.load(source)
    card["policy_comparison"] = results
    card["bandit_simulation"] = {"selections": selections, "arms": arms}
    with open(path, "w") as output:
        json.dump(card, output, indent=2)
        output.write("\n")


if __name__ == "__main__":
    main()
