"""Next best action: scores Eric Health assets and picks the best asset and channel."""
import json
import math
import os
import random

from cdp import assets, content

with open(os.path.join(os.path.dirname(__file__), "nba_model.json")) as model_file:
    BUNDLED_MODEL = json.load(model_file)

FEATURES = [
    "age",
    "tenure_months",
    "app_sessions_30d",
    "days_since_active",
    "email_open_rate",
    "wellness_member",
    "touches",
    "opens",
    "clicks",
    "is_direct_mail",
]
PROFILE_FEATURES = FEATURES[:6]

# Illustrative business assumptions: replace these with real values before using them for decisions.
REGISTRATION_VALUE = 120.0
CHANNEL_COST = {"email": 0.02, "direct_mail": 1.25}
MAX_TOUCHES = 4
EXPLORATION_RATE = 0.1  # Keeps collecting evidence on assets the model does not currently favor.


def features_for(traits, state, channel):
    values = {}
    for name in PROFILE_FEATURES:
        value = traits.get(name)
        values[name] = float(value) if isinstance(value, (int, float)) else None
    values["touches"] = float(state.get("touches", 0))
    values["opens"] = float(state.get("opens", 0))
    values["clicks"] = float(state.get("clicks", 0))
    values["is_direct_mail"] = 1.0 if channel == "direct_mail" else 0.0
    return values


def propensity(model, asset_id, features):
    """Return the modeled chance of registering, or None if the asset has no model yet."""
    weights = model["actions"].get(asset_id)
    if weights is None:
        return None
    means = weights.get("means", model["means"])
    stds = weights.get("stds", model["stds"])
    score = weights["intercept"]
    for name, weight, mean, std in zip(model["features"], weights["weights"], means, stds):
        value = features.get(name)
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


def _message(customer_id, asset_id, channel, traits, state, model, choice, rng):
    touch = "onboarding" if asset_id == assets.ONBOARDING else str(state.get("touches", 0) + 1)
    message_id = f"{customer_id}-{touch}"
    features = {k: (0.0 if v is None else v) for k, v in features_for(traits, state, channel).items()}
    image = content.choose(customer_id, touch, features, state, model.get("content") or content.BUNDLED_MODEL, rng)
    alt = content.THEMES[image["image_theme"]]["label"]
    return {
        **choice,
        **image,
        "message_id": message_id,
        "customer_id": customer_id,
        "touch": touch,
        "asset_id": asset_id,
        "action": asset_id,
        "channel": channel,
        "model_version": model["version"],
        "subject": assets.ASSETS[asset_id]["subject"],
        "content_html": assets.render(
            asset_id, channel, traits.get("first_name"), message_id, content.image_url(image["image_id"]), alt
        ),
        "recipient": _recipient(channel, traits),
        "features": features,
    }


def decide(customer_id, traits, consent, state, model=None, rng=random):
    """Return the next message, or None if no contact is allowed or worthwhile."""
    model = model or BUNDLED_MODEL
    channels = eligible_channels(traits, consent)
    if not channels:
        return None

    if state.get("registered"):
        if state.get("onboarded"):
            return None
        return _message(
            customer_id, assets.ONBOARDING, channels[0], traits, state, model, {"selection": "onboarding"}, rng
        )

    if state.get("touches", 0) >= MAX_TOUCHES:
        return None

    scored = []
    for asset_id in assets.SIGNUP_ASSETS:
        if asset_id == state.get("last_asset"):
            continue
        for channel in channels:
            probability = propensity(model, asset_id, features_for(traits, state, channel))
            if probability is not None:
                scored.append((probability * REGISTRATION_VALUE - CHANNEL_COST[channel], probability, asset_id, channel))
    if not scored:
        return None

    if rng.random() < EXPLORATION_RATE:
        value, probability, asset_id, channel = rng.choice(scored)
        selection = "explore"
    else:
        value, probability, asset_id, channel = max(scored, key=lambda item: item[0])
        selection = "best"
        if value <= 0:
            return None

    choice = {"selection": selection, "propensity": round(probability, 4), "expected_value": round(value, 2)}
    return _message(customer_id, asset_id, channel, traits, state, model, choice, rng)
