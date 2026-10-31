"""Generates a repeatable synthetic population. None of these people are real."""
import random

FIRST_NAMES = ["Alex", "Blake", "Casey", "Dana", "Elliot", "Frankie", "Gray", "Harper", "Indy", "Jordan", "Kai", "Logan"]
LAST_NAMES = ["Sample", "Example", "Tester", "Demo", "Placeholder", "Fixture", "Mockley", "Dummyson"]
STREETS = ["Sample St", "Example Ave", "Test Rd", "Demo Blvd", "Fixture Ln"]
STATES = ["CA", "NY", "TX", "FL", "IL", "WA", "OH", "CO"]


def generate(count=10_000, seed=42):
    """Return a list of {person, consent} entries."""
    rng = random.Random(seed)
    people = []
    for index in range(1, count + 1):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        person = {
            "user_id": f"sample-{seed}-{index:05d}",
            "first_name": first,
            "last_name": last,
            "email": f"{first}.{last}.{seed}.{index:05d}@example.com".lower(),
            "address": f"{rng.randint(100, 9999)} {rng.choice(STREETS)}",
            "city": "Anytown",
            "state": rng.choice(STATES),
            "postal_code": f"{rng.randint(10000, 99999)}",
            "age": rng.randint(18, 85),
            "tenure_months": rng.randint(1, 120),
            "app_sessions_30d": min(int(rng.expovariate(0.2)), 30),
            "days_since_active": rng.randint(0, 120),
            "email_open_rate": round(rng.betavariate(2, 5), 3),
            "wellness_member": int(rng.random() < 0.3),
        }
        consent = {
            "marketing": rng.random() < 0.8,
            "email": rng.random() < 0.75,
            "direct_mail": rng.random() < 0.45,
        }
        people.append({"person": person, "consent": consent})
    return people


def traits_of(entry):
    return {k: v for k, v in entry["person"].items() if k != "user_id"}


def to_events(entry):
    """Return the identify and consent API events for one person."""
    person = entry["person"]
    return [
        {
            "type": "identify",
            "event_id": f"{person['user_id']}-identify",
            "user_id": person["user_id"],
            "email": person["email"],
            "traits": traits_of(entry),
        },
        {
            "type": "consent",
            "event_id": f"{person['user_id']}-consent",
            "user_id": person["user_id"],
            "consent": entry["consent"],
        },
    ]
