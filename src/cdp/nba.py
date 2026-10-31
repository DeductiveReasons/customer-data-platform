"""Next best action: scores each customer's offers and picks the best offer and channel."""
import html
import json
import math
import os

with open(os.path.join(os.path.dirname(__file__), "nba_model.json")) as model_file:
    MODEL = json.load(model_file)

FEATURES = [
    "age",
    "tenure_months",
    "orders_12m",
    "spend_12m",
    "days_since_purchase",
    "email_open_rate",
    "loyalty_member",
]

# Illustrative business assumptions: replace these with real values before using them for decisions.
ACTIONS = {
    "win_back": {
        "value": 40.0,
        "subject": "We saved something for you",
        "body": "It has been a while. Here is 20% off your next order.",
    },
    "replenish": {
        "value": 25.0,
        "subject": "Time to restock?",
        "body": "Your favorites may be running low. Reorder in one step.",
    },
    "loyalty_upgrade": {
        "value": 60.0,
        "subject": "You have earned an upgrade",
        "body": "Join our top loyalty tier and get free shipping all year.",
    },
}
CHANNELS = {
    "email": {"lift": 1.0, "cost": 0.02},
    "direct_mail": {"lift": 1.8, "cost": 1.25},
}


def feature_vector(traits):
    return [float(traits[name]) if isinstance(traits.get(name), (int, float)) else None for name in FEATURES]


def propensity(action, vector):
    model = MODEL["actions"][action]
    score = model["intercept"]
    for value, mean, std, weight in zip(vector, MODEL["means"], MODEL["stds"], model["weights"]):
        if value is not None:  # A missing feature stays at the mean, which contributes zero.
            score += weight * (value - mean) / std
    return 1 / (1 + math.exp(-max(min(score, 30), -30)))


def eligible_channels(traits, consent):
    # Every channel needs marketing consent and its own consent. Missing consent means no contact.
    if consent.get("marketing") is not True:
        return []
    channels = []
    if consent.get("email") is True and traits.get("email"):
        channels.append("email")
    if consent.get("direct_mail") is True and all(traits.get(k) for k in ("address", "city", "state", "postal_code")):
        channels.append("direct_mail")
    return channels


def render_html(action, channel, traits):
    content = ACTIONS[action]
    name = html.escape(str(traits.get("first_name") or "there"))
    footer = {
        "email": "You can unsubscribe at any time.",
        "direct_mail": "To stop receiving mail, contact customer care.",
    }[channel]
    return (
        f"<html><body><h1>{html.escape(content['subject'])}</h1>"
        f"<p>Hi {name},</p><p>{html.escape(content['body'])}</p>"
        f"<p><small>{footer}</small></p></body></html>"
    )


def _recipient(channel, traits):
    if channel == "email":
        return {"email": traits["email"]}
    name = " ".join(str(traits[k]) for k in ("first_name", "last_name") if traits.get(k))
    return {
        "name": name,
        "address": traits["address"],
        "city": traits["city"],
        "state": traits["state"],
        "postal_code": traits["postal_code"],
    }


def decide(customer_id, traits, consent):
    """Return the highest expected-value message, or None if no contact is worthwhile or allowed."""
    channels = eligible_channels(traits, consent)
    vector = feature_vector(traits)
    best = None
    for action, config in ACTIONS.items():
        probability = propensity(action, vector)
        for channel in channels:
            settings = CHANNELS[channel]
            value = min(probability * settings["lift"], 1.0) * config["value"] - settings["cost"]
            if value > 0 and (best is None or value > best["expected_value"]):
                best = {
                    "customer_id": customer_id,
                    "action": action,
                    "channel": channel,
                    "propensity": round(probability, 4),
                    "expected_value": round(value, 2),
                }
    if best is None:
        return None
    return {
        **best,
        "model_version": MODEL["version"],
        "subject": ACTIONS[best["action"]]["subject"],
        "content_html": render_html(best["action"], best["channel"], traits),
        "recipient": _recipient(best["channel"], traits),
    }
