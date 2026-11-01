"""Generates a repeatable synthetic population. None of these people are real."""
import random

FIRST_NAMES = ["Alex", "Blake", "Casey", "Dana", "Elliot", "Frankie", "Gray", "Harper", "Indy", "Jordan", "Kai", "Logan"]
LAST_NAMES = ["Sample", "Example", "Tester", "Demo", "Placeholder", "Fixture", "Mockley", "Dummyson"]
STREETS = ["Sample St", "Example Ave", "Test Rd", "Demo Blvd", "Fixture Ln"]
STATES = ["CA", "NY", "TX", "FL", "IL", "WA", "OH", "CO"]


EMPLOYER_FIRST = ["Alder", "Birch", "Cedar", "Delta", "Ember", "Fable", "Grove", "Harbor", "Iris", "Juniper", "Kestrel", "Linden", "Meadow", "Nimbus", "Orchard", "Prairie", "Quartz", "Ridge", "Sable", "Tamarack"]
EMPLOYER_SECOND = ["Field", "Stone", "Brook", "Crest", "Vale", "Point", "Hollow", "Bay", "Peak", "Works", "Bridge", "Lake", "Wood", "Haven", "Gate", "Mill", "Shore", "Heights", "Glen", "Forge", "Pine", "Hill", "Marsh", "Rock", "Spring"]
EMPLOYER_SUFFIX = {
    "technology": "Software",
    "healthcare": "Care Group",
    "manufacturing": "Manufacturing",
    "finance": "Financial",
    "education": "Learning",
    "retail": "Retail",
    "logistics": "Logistics",
    "public_sector": "Services",
}
INDUSTRY_REMOTE = {
    "technology": 0.65,
    "healthcare": 0.08,
    "manufacturing": 0.06,
    "finance": 0.5,
    "education": 0.2,
    "retail": 0.07,
    "logistics": 0.1,
    "public_sector": 0.3,
}


def generate_employers(count=500, seed=2024):
    """Return made-up employers. Any resemblance to a real company is accidental."""
    rng = random.Random(seed)
    names = [f"{first} {second}" for first in EMPLOYER_FIRST for second in EMPLOYER_SECOND]
    rng.shuffle(names)
    employers = []
    for index in range(count):
        industry = rng.choice(list(EMPLOYER_SUFFIX))
        employees = int(min(rng.lognormvariate(6.2, 1.5), 100_000)) + 10
        tier = rng.choices([0, 1, 2, 3], weights=[35, 35, 20, 10])[0]
        subsidy = int(rng.random() < 0.1 + 0.2 * tier)
        employers.append(
            {
                "employer_id": f"emp-{index + 1:04d}",
                "employer_name": f"{names[index]} {EMPLOYER_SUFFIX[industry]} (fictional)",
                "employer_industry": industry,
                "employer_employees": employees,
                "employer_state": rng.choice(STATES),
                "employer_wellness_tier": tier,
                "employer_fitness_subsidy": subsidy,
                "employer_participation_rate": round(min(0.6, rng.betavariate(2, 8) + 0.04 * tier), 3),
                "employer_remote_share": round(max(0.0, min(1.0, rng.gauss(INDUSTRY_REMOTE[industry], 0.12))), 3),
            }
        )
    return employers


def generate(count=100_000, seed=42):
    """Return a list of {person, consent} entries."""
    rng = random.Random(seed)
    employers = generate_employers()
    # Larger employers have more people, with a limit so one employer cannot dominate.
    employer_weights = [min(employer["employer_employees"], 5000) for employer in employers]
    assigned = rng.choices(employers, weights=employer_weights, k=count)
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
            **assigned[index - 1],
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
