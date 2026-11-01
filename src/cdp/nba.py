"""Next best action: scores Eric Health assets and picks the best asset and channel."""
import json
import math
import os
import random
from datetime import datetime, timedelta, timezone

from cdp import assets, content

with open(os.path.join(os.path.dirname(__file__), "nba_model.json")) as model_file:
    BUNDLED_MODEL = json.load(model_file)

PROFILE_FEATURES = [
    "age",
    "tenure_months",
    "app_sessions_30d",
    "days_since_active",
    "email_open_rate",
    "wellness_member",
    "employer_employees",
    "employer_fitness_subsidy",
    "employer_wellness_tier",
    "employer_participation_rate",
    "employer_remote_share",
]
INDUSTRIES = [
    "technology",
    "healthcare",
    "manufacturing",
    "finance",
    "education",
    "retail",
    "logistics",
    "public_sector",
]
FEATURES = (
    PROFILE_FEATURES
    + [f"industry_{industry}" for industry in INDUSTRIES]
    + ["touches", "opens", "clicks", "is_direct_mail"]
)
GBM_FEATURES = FEATURES + [f"asset_{asset_id}" for asset_id in assets.SIGNUP_ASSETS]

# Illustrative business assumptions: replace these with real values before using them for decisions.
REGISTRATION_VALUE = 120.0
CHANNEL_COST = {"email": 0.02, "direct_mail": 1.25}
MAX_TOUCHES = 4
MIN_DAYS_BETWEEN_SCANS = 7
DIRECT_MAIL_LEAD_DAYS = 7
EMAIL_GAP_DAYS = 3  # Days before the next decision after an email.
MAIL_RESPONSE_DAYS = 7  # Days to allow for a response after direct mail is delivered.
NO_CONTACT_RECHECK_DAYS = 30
NEVER_DUE = 9999
# Standard-time UTC offsets, so these times can be an hour off during daylight saving time.
STATE_UTC_OFFSET = {"CA": -8, "WA": -8, "CO": -7, "TX": -6, "IL": -6, "NY": -5, "FL": -5, "OH": -5}
DEFAULT_UTC_OFFSET = -5
EXPLORATION_RATE = 0.1  # Keeps collecting evidence on assets the model does not currently favor.


def features_for(traits, state, channel):
    values = {}
    for name in PROFILE_FEATURES:
        value = traits.get(name)
        values[name] = float(value) if isinstance(value, (int, float)) else None
    for industry in INDUSTRIES:
        values[f"industry_{industry}"] = 1.0 if traits.get("employer_industry") == industry else 0.0
    values["touches"] = float(state.get("touches", 0))
    values["opens"] = float(state.get("opens", 0))
    values["clicks"] = float(state.get("clicks", 0))
    values["is_direct_mail"] = 1.0 if channel == "direct_mail" else 0.0
    return values


def send_hour(traits, channel):
    """Rule for the best local hour. This is a starting rule, not a learned timing model."""
    if channel == "direct_mail":
        return 10
    if isinstance(traits.get("email_open_rate"), (int, float)) and traits["email_open_rate"] >= 0.4:
        return 8
    if isinstance(traits.get("app_sessions_30d"), (int, float)) and traits["app_sessions_30d"] >= 5:
        return 18
    return 12


def next_action_time(traits, state, channel, now=None):
    """Return the next allowed send time in UTC: after the contact gap, at the person's best local hour."""
    now = now or datetime.now(timezone.utc)
    earliest = now
    if state.get("last_contact"):
        last_contact = datetime.fromtimestamp(float(state["last_contact"]), timezone.utc)
        earliest = max(now, last_contact + timedelta(days=MIN_DAYS_BETWEEN_SCANS))
    local_zone = timezone(timedelta(hours=STATE_UTC_OFFSET.get(traits.get("state"), DEFAULT_UTC_OFFSET)))
    local = earliest.astimezone(local_zone)
    candidate = local.replace(hour=send_hour(traits, channel), minute=0, second=0, microsecond=0)
    if candidate < local:
        candidate += timedelta(days=1)
    # Mail goes out on weekdays only.
    while channel == "direct_mail" and candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc).isoformat()


def gbm_row(features, asset_id):
    """Return the model's inputs in order. None means the value is missing."""
    row = [features.get(name) for name in FEATURES]
    row.extend(1.0 if other == asset_id else 0.0 for other in assets.SIGNUP_ASSETS)
    return row


def score_gbm(model, row):
    """Score gradient-boosted trees with no dependencies, so it runs in a small Lambda."""
    total = 0.0
    for tree in model["trees"]:
        features, thresholds, lefts, rights, default_left = tree["f"], tree["t"], tree["l"], tree["r"], tree["d"]
        node = 0
        while features[node] >= 0:
            value = row[features[node]]
            if value is None:
                node = lefts[node] if default_left[node] else rights[node]
            elif value <= thresholds[node]:
                node = lefts[node]
            else:
                node = rights[node]
        total += tree["v"][node]
    return 1 / (1 + math.exp(-max(min(total, 30), -30)))


def send_time_on(traits, channel, send_date):
    """Return the send time in UTC for an ISO date, at the person's best local hour."""
    local_zone = timezone(timedelta(hours=STATE_UTC_OFFSET.get(traits.get("state"), DEFAULT_UTC_OFFSET)))
    year, month, day = (int(part) for part in send_date.split("-"))
    local = datetime(year, month, day, send_hour(traits, channel), tzinfo=local_zone)
    return local.astimezone(timezone.utc).isoformat()


def next_due_after_send(channel, day):
    if channel == "direct_mail":
        return day + DIRECT_MAIL_LEAD_DAYS + MAIL_RESPONSE_DAYS
    return day + EMAIL_GAP_DAYS


def propensity(model, asset_id, features):
    """Return the modeled chance of registering, or None if the asset has no model yet."""
    if model.get("type") == "gbm":
        return score_gbm(model, gbm_row(features, asset_id))
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


def _message(customer_id, asset_id, channel, traits, state, model, choice, rng, pending=frozenset()):
    touch = "onboarding" if asset_id == assets.ONBOARDING else str(state.get("touches", 0) + 1)
    message_id = f"{customer_id}-{touch}"
    features = {k: (0.0 if v is None else v) for k, v in features_for(traits, state, channel).items()}
    image = content.choose(
        customer_id,
        touch,
        features,
        state,
        model.get("content") or content.BUNDLED_MODEL,
        rng,
        lambda image_id: f"{asset_id}~{channel}~{image_id}" in pending,
    )
    if image is None:
        return None
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


def decide(customer_id, traits, consent, state, model=None, rng=random, pending=frozenset()):
    """Return the next message, or None if no contact is allowed or worthwhile."""
    model = model or BUNDLED_MODEL
    channels = eligible_channels(traits, consent)
    if not channels:
        return None

    if state.get("registered"):
        if state.get("onboarded"):
            return None
        return _message(
            customer_id, assets.ONBOARDING, channels[0], traits, state, model, {"selection": "onboarding"}, rng, pending
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

    ranked = sorted(scored, key=lambda item: item[0], reverse=True)
    if rng.random() < EXPLORATION_RATE:
        first = rng.choice(scored)
        ranked = [first] + [item for item in ranked if item is not first]
        selection = "explore"
    else:
        ranked = [item for item in ranked if item[0] > 0]
        selection = "best"

    # If a variation is pending approval, move on to the next best approved option.
    for value, probability, asset_id, channel in ranked:
        choice = {"selection": selection, "propensity": round(probability, 4), "expected_value": round(value, 2)}
        message = _message(customer_id, asset_id, channel, traits, state, model, choice, rng, pending)
        if message is not None:
            return message
    return None
