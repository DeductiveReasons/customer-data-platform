"""Runs customers through the decision and feedback loop in memory, without AWS."""
import copy
import random
import types
import sys

sys.modules.setdefault("boto3", types.ModuleType("boto3"))

from cdp import assets, nba, receiver, simulator  # noqa: E402
from sample_data import traits_of  # noqa: E402


class RandomPolicyRng:
    """Makes nba.decide always take its exploration branch, which picks a candidate at random."""

    def __init__(self, seed):
        self._rng = random.Random(seed)

    def random(self):
        return 0.0

    def choice(self, items):
        return self._rng.choice(items)


def run(population, model, rng):
    """Run each person's journey to the end and return training records and totals."""
    records = []
    totals = {"messages": 0, "email": 0, "direct_mail": 0, "registered": 0, "onboarded": 0, "cost": 0.0, "no_contact": 0}
    for entry in population:
        person = entry["person"]
        traits = traits_of(entry)
        consent = copy.deepcopy(entry["consent"])
        state = {}
        contacted = False
        while True:
            message = nba.decide(person["user_id"], traits, consent, state, model, rng)
            if message is None:
                break
            error = receiver.validate(message["channel"], message)
            if error:
                raise SystemExit(f"receiver rejected {message['message_id']}: {error}")
            if not (consent["marketing"] and consent[message["channel"]]):
                raise SystemExit(f"consent violation on {message['message_id']}")

            contacted = True
            totals["messages"] += 1
            totals[message["channel"]] += 1
            totals["cost"] += nba.CHANNEL_COST[message["channel"]]
            if message["asset_id"] == assets.ONBOARDING:
                state["onboarded"] = True
                totals["onboarded"] += 1
                continue

            outcome = simulator.outcome_for(
                message["message_id"], message["asset_id"], message["features"], message["image_theme"]
            )
            records.append(
                {
                    "asset_id": message["asset_id"],
                    "image_theme": message["image_theme"],
                    "features": message["features"],
                    **outcome,
                }
            )
            state["touches"] = state.get("touches", 0) + 1
            state["last_asset"] = message["asset_id"]
            state["last_image"] = message["image_id"]
            state["opens"] = state.get("opens", 0) + int(outcome["opened"])
            state["clicks"] = state.get("clicks", 0) + int(outcome["clicked"])
            if outcome["registered"]:
                state["registered"] = True
                totals["registered"] += 1
            if outcome["unsubscribed"]:
                consent[message["channel"]] = False
        if not contacted:
            totals["no_contact"] += 1
    return records, totals
