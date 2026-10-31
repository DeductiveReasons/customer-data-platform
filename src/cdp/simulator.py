"""Invented customer behavior for the dummy receiver. Not based on any real data."""
import random

from cdp.training import sigmoid

ASSET_EFFECT = {
    "program_intro": lambda f: 0.3 - 0.6 * f["touches"],
    "success_story": lambda f: 0.1 + 0.5 * (f["age"] - 45) / 20 - 0.5 * (f["app_sessions_30d"] - 5) / 5,
    "free_trial": lambda f: 0.2 + 0.7 * (f["app_sessions_30d"] - 5) / 5 + 0.4 * f["clicks"],
    "coach_consult": lambda f: -0.4 + 1.0 * f["clicks"] + 0.7 * f["wellness_member"],
}


THEME_EFFECT = {
    "outdoor_walk": lambda f: 0.5 * (f["days_since_active"] - 60) / 60 + 0.2,
    "home_workout": lambda f: -0.5 * (f["app_sessions_30d"] - 5) / 5 + 0.1,
    "strength": lambda f: 0.9 * (f["app_sessions_30d"] - 5) / 5 - 0.3,
    "yoga": lambda f: 0.7 * (f["wellness_member"] - 0.3),
    "group_class": lambda f: 0.8 * (f["email_open_rate"] - 0.3) / 0.2 - 0.2,
    "coaching": lambda f: 0.5 * f["clicks"] + 0.4 * f["touches"] - 0.5,
    "cycling": lambda f: 0.5 * (f["tenure_months"] - 60) / 60 - 0.1,
    "swimming": lambda f: -0.5 * (f["tenure_months"] - 60) / 60 - 0.2,
    "running": lambda f: 0.7 * (f["app_sessions_30d"] - 5) / 5 - 0.4 * (f["days_since_active"] - 60) / 60 - 0.2,
    "daily_routine": lambda f: -0.4 * (f["email_open_rate"] - 0.3) / 0.2 + 0.05,
}


def outcome_for(message_id, asset_id, features, theme=None):
    """Return what this person does with a message. Repeatable for a given message_id."""
    rng = random.Random(message_id)
    is_mail = bool(features["is_direct_mail"])
    recency = features["days_since_active"] / 60

    read_chance = sigmoid(-0.5 + 3.0 * features["email_open_rate"] - 0.5 * recency - 0.15 * features["touches"])
    if is_mail:
        read_chance = sigmoid(0.3 - 0.3 * recency)
    read = rng.random() < read_chance

    image_effect = THEME_EFFECT[theme](features) if theme in THEME_EFFECT else 0
    click_chance = sigmoid(-1.7 + ASSET_EFFECT[asset_id](features) + image_effect - (0.5 if is_mail else 0))
    clicked = read and rng.random() < click_chance

    register_chance = sigmoid(-0.8 + 0.6 * ASSET_EFFECT[asset_id](features) + 0.3 * features["wellness_member"])
    registered = clicked and rng.random() < register_chance

    unsubscribed = not clicked and rng.random() < 0.01 + 0.01 * features["touches"]
    return {
        "opened": read and not is_mail,  # Mail reads cannot be observed, only QR code visits.
        "clicked": clicked,
        "registered": registered,
        "unsubscribed": unsubscribed,
    }
