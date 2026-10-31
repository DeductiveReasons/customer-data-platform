"""Generates a repeatable synthetic population. None of these people are real."""
import math
import random

FIRST_NAMES = ["Alex", "Blake", "Casey", "Dana", "Elliot", "Frankie", "Gray", "Harper", "Indy", "Jordan", "Kai", "Logan"]
LAST_NAMES = ["Sample", "Example", "Tester", "Demo", "Placeholder", "Fixture", "Mockley", "Dummyson"]
STREETS = ["Sample St", "Example Ave", "Test Rd", "Demo Blvd", "Fixture Ln"]
STATES = ["CA", "NY", "TX", "FL", "IL", "WA", "OH", "CO"]


def _sigmoid(value):
    return 1 / (1 + math.exp(-value))


def _outcomes(rng, person):
    """Hidden response behavior that the model has to learn. It is invented for this demo."""
    recency = person["days_since_purchase"] / 180
    orders = person["orders_12m"] / 6
    spend = person["spend_12m"] / 500
    tenure = person["tenure_months"] / 48
    opens = person["email_open_rate"]
    loyal = person["loyalty_member"]
    chances = {
        "win_back": _sigmoid(-2.6 + 1.5 * recency + 0.6 * tenure + 0.7 * opens - 0.8 * orders),
        "replenish": _sigmoid(-2.9 + 1.5 * orders - 0.9 * recency + 0.9 * opens + 0.3 * loyal),
        "loyalty_upgrade": _sigmoid(-3.3 + 1.4 * spend + 0.8 * tenure + 0.8 * orders - 0.8 * loyal),
    }
    return {action: int(rng.random() < chance) for action, chance in chances.items()}


def generate(count=10_000, seed=42):
    """Return (person, outcomes) pairs. Outcomes are for training only and are never ingested."""
    rng = random.Random(seed)
    people = []
    for index in range(1, count + 1):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        orders = min(rng.randint(0, 3) + int(rng.expovariate(0.4)), 30)
        person = {
            "user_id": f"sample-{index:05d}",
            "first_name": first,
            "last_name": last,
            "email": f"{first}.{last}.{index:05d}@example.com".lower(),
            "address": f"{rng.randint(100, 9999)} {rng.choice(STREETS)}",
            "city": "Anytown",
            "state": rng.choice(STATES),
            "postal_code": f"{rng.randint(10000, 99999)}",
            "age": rng.randint(18, 85),
            "tenure_months": rng.randint(1, 120),
            "orders_12m": orders,
            "spend_12m": round(orders * rng.uniform(15, 120), 2),
            "days_since_purchase": rng.randint(1, 60) if orders > 3 else rng.randint(1, 365),
            "email_open_rate": round(rng.betavariate(2, 5), 3),
            "loyalty_member": int(rng.random() < 0.35),
        }
        consent = {
            "marketing": rng.random() < 0.8,
            "email": rng.random() < 0.75,
            "direct_mail": rng.random() < 0.45,
        }
        people.append(({"person": person, "consent": consent}, _outcomes(rng, person)))
    return people


def to_events(entry):
    """Return the identify and consent API events for one person."""
    person = entry["person"]
    traits = {k: v for k, v in person.items() if k != "user_id"}
    return [
        {
            "type": "identify",
            "event_id": f"{person['user_id']}-identify",
            "user_id": person["user_id"],
            "email": person["email"],
            "traits": traits,
        },
        {
            "type": "consent",
            "event_id": f"{person['user_id']}-consent",
            "user_id": person["user_id"],
            "consent": entry["consent"],
        },
    ]
