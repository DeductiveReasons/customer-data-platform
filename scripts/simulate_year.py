"""Simulates the daily campaign for a full year, using the same decision, bandit and outcome code as AWS.

Running each day through the live pipeline would take many hours. This runs them in one process and
writes the results in the formats the AWS tables and dashboards read. AWS then continues from the last day.

Usage:
  python scripts/simulate_year.py --dry-run [people] [days]
  python scripts/simulate_year.py <profiles_table> <bucket> [days]
"""
import collections
import concurrent.futures
import gzip
import json
import os
import random
import sys
import time
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "src"))

DRY_RUN = "--dry-run" in sys.argv
if DRY_RUN:
    import types

    sys.modules.setdefault("boto3", types.ModuleType("boto3"))
    conditions = types.ModuleType("boto3.dynamodb.conditions")
    conditions.Attr = None
    sys.modules.setdefault("boto3.dynamodb", types.ModuleType("boto3.dynamodb"))
    sys.modules.setdefault("boto3.dynamodb.conditions", conditions)
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = Exception
    sys.modules.setdefault("botocore", types.ModuleType("botocore"))
    sys.modules.setdefault("botocore.exceptions", exceptions)

import boto3  # noqa: E402

from cdp import bandit, content, daily, identity, nba, nba_export, sample_data, simulator  # noqa: E402

START_DATE = daily.DEFAULT_START
NEW_LEADS_PER_DAY = 200
ROWS_PER_FILE = 25_000


class _Best:
    def random(self):
        return 1.0


class Lead:
    __slots__ = ("id", "traits", "consent", "state", "received_day", "row")

    def __init__(self, lead_id, traits, consent, received_day):
        self.id = lead_id
        self.traits = traits
        self.consent = consent
        self.state = {}
        self.received_day = received_day
        self.row = None


def from_profile(item):
    traits = {k[2:]: _plain(v) for k, v in item.items() if k.startswith("t_")}
    consent = {k[2:]: v for k, v in item.items() if k.startswith("c_")}
    return Lead(item["pk"].split("#", 1)[1], traits, consent, 0)


def _plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def read_leads(table_name):
    """Read the existing leads, so this run uses exactly those people."""

    def segment(number):
        table = boto3.resource("dynamodb").Table(table_name)
        found = []
        kwargs = {
            "Segment": number,
            "TotalSegments": 12,
            "FilterExpression": boto3.dynamodb.conditions.Attr("sk").eq("PROFILE"),
        }
        while True:
            page = table.scan(**kwargs)
            found.extend(from_profile(item) for item in page["Items"])
            if "LastEvaluatedKey" not in page:
                return found
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        return [lead for part in pool.map(segment, range(12)) for lead in part]


def new_leads(day):
    seed = daily.NEW_LEAD_SEED + day
    count = random.Random(seed).randint(int(NEW_LEADS_PER_DAY * 0.85), int(NEW_LEADS_PER_DAY * 1.15))
    received = daily.sim_date(START_DATE, day)
    leads = []
    for entry in sample_data.generate(count, seed=seed, received=received):
        leads.append(Lead(entry["person"]["user_id"], sample_data.traits_of(entry), entry["consent"], day))
    return leads


def report_row(lead, clock, model):
    """Build the fields the daily snapshot needs, using the same next best action logic as the export."""
    state = lead.state
    decision = nba.decide(lead.id, lead.traits, lead.consent, state, model, _Best())
    return {
        "next_best_action": decision["channel"] if decision else "no_contact",
        "next_due": int(state.get("next_due", 0)) if decision else None,
        "next_send_day": None,
        "last_channel": state.get("last_channel"),
        "last_send_day": state.get("last_send_day"),
        "mail_in_transit": False,
        "touches": state.get("touches", 0),
        "registered": state.get("registered") is True,
        "days_to_register": (
            state["registered_day"] - lead.received_day if state.get("registered_day") is not None else None
        ),
    }


def run(leads, days):
    by_id = {lead.id: lead for lead in leads}
    due = collections.defaultdict(list)
    for lead in leads:
        due[0].append(lead)
    mail_due = collections.defaultdict(list)
    registration_due = collections.defaultdict(list)
    stats = bandit.new_stats(content.THEMES)
    model = {**nba.BUNDLED_MODEL, "content": bandit.to_model(stats, "bandit-start")}
    rng = random.Random(2026)
    history, email_rows, mail_rows, snapshots = [], [], [], []
    changed = set(lead.id for lead in leads)
    started = time.time()

    def apply_feedback(lead, message, outcome, day):
        state = lead.state
        state["opens"] = state.get("opens", 0) + int(outcome["opened"])
        state["clicks"] = state.get("clicks", 0) + int(outcome["clicked"])
        if outcome["unsubscribed"]:
            lead.consent[message["channel"]] = False
        if outcome["registered"] and not state.get("registered"):
            registration_due[day + outcome["registration_lag_days"]].append(lead)
        bandit.add(stats, message["image_theme"], message["features"], outcome["clicked"])
        row = {
            field: message.get(field)
            for field in (
                "message_id",
                "customer_id",
                "touch",
                "asset_id",
                "channel",
                "features",
                "model_version",
                "selection",
                "image_id",
                "image_theme",
                "content_selection",
                "send_day",
                "delivery_day",
            )
        }
        row.update(outcome)
        (mail_rows if message["channel"] == "direct_mail" else email_rows).append(row)
        changed.add(lead.id)

    # Day -1 reports the leads as received, before any decision.
    for day in range(-1, days + 1):
        if day >= 0:
            for lead in new_leads(day):
                by_id[lead.id] = lead
                due[day].append(lead)
                changed.add(lead.id)

            for lead, message, outcome in mail_due.pop(day, []):
                apply_feedback(lead, message, outcome, day)

            for lead in registration_due.pop(day, []):
                if not lead.state.get("registered"):
                    lead.state["registered"] = True
                    lead.state["registered_day"] = day
                    # The welcome message goes out in the next daily run.
                    if not lead.state.get("onboarded"):
                        lead.state["next_due"] = day + 1
                        due[day + 1].append(lead)
                    changed.add(lead.id)

            for lead in due.pop(day, []):
                state = lead.state
                if state.get("next_due", 0) > day:
                    continue
                message = nba.decide(lead.id, lead.traits, lead.consent, state, model, rng)
                changed.add(lead.id)
                if message is None:
                    finished = state.get("touches", 0) >= nba.MAX_TOUCHES or state.get("onboarded")
                    state["next_due"] = nba.NEVER_DUE if finished else day + nba.NO_CONTACT_RECHECK_DAYS
                    if not finished:
                        due[state["next_due"]].append(lead)
                    continue
                is_mail = message["channel"] == "direct_mail"
                message["send_day"] = day
                message["delivery_day"] = day + (nba.DIRECT_MAIL_LEAD_DAYS if is_mail else 0)
                history.append(
                    {
                        field: message.get(field)
                        for field in (
                            "message_id",
                            "customer_id",
                            "touch",
                            "asset_id",
                            "channel",
                            "selection",
                            "propensity",
                            "expected_value",
                            "model_version",
                            "image_id",
                            "image_theme",
                            "content_selection",
                            "content_score",
                            "send_day",
                            "delivery_day",
                        )
                    }
                    | {"trigger": "daily", "decided_at": daily.sim_date(START_DATE, day)}
                )
                state["last_channel"] = message["channel"]
                state["last_send_day"] = day
                state["last_image"] = message["image_id"]
                if message["asset_id"] == nba.assets.ONBOARDING:
                    state["onboarded"] = True
                    state["next_due"] = nba.NEVER_DUE
                    continue
                state["last_asset"] = message["asset_id"]
                state["touches"] = state.get("touches", 0) + 1
                state["next_due"] = nba.next_due_after_send(message["channel"], day)
                due[state["next_due"]].append(lead)
                outcome = simulator.outcome_for(
                    message["message_id"], message["asset_id"], message["features"], message["image_theme"]
                )
                if is_mail:
                    mail_due[message["delivery_day"]].append((lead, message, outcome))
                else:
                    apply_feedback(lead, message, outcome, day)

            # The bandit updates once a day, as the daily run does.
            model = {**model, "content": bandit.to_model(stats, f"bandit-day-{day}")}

        clock = {"day": day, "start_date": START_DATE}
        for lead_id in changed:
            lead = by_id[lead_id]
            lead.row = report_row(lead, clock, model)
        changed = set()
        rows = []
        for lead in by_id.values():
            row = lead.row
            row["next_send_day"] = max(day + 1, row["next_due"]) if row["next_due"] is not None else None
            row["mail_in_transit"] = (
                row["last_channel"] == "direct_mail"
                and row["last_send_day"] is not None
                and row["last_send_day"] + nba.DIRECT_MAIL_LEAD_DAYS > day
            )
            rows.append(row)
        snapshots.append(nba_export.daily_snapshot(rows, clock))
        if day % 30 == 0:
            latest = snapshots[-1]
            print(
                f"day {day}: leads {latest['people']}, registered {latest['registered']} "
                f"({latest['registration_rate']:.1%}), messages {len(history)}, {time.time() - started:.0f}s",
                flush=True,
            )
    pending = {"mail": mail_due, "registrations": registration_due}
    return by_id, model, history, email_rows, mail_rows, snapshots, pending


def _put_rows(s3, bucket, prefix, rows):
    count = 0
    for start in range(0, len(rows), ROWS_PER_FILE):
        part = rows[start : start + ROWS_PER_FILE]
        body = "".join(json.dumps(row) + "\n" for row in part).encode()
        s3.put_object(Bucket=bucket, Key=f"{prefix}year-run-{count:05d}.json.gz", Body=gzip.compress(body))
        count += 1
    return count


def profile_item(lead):
    item = {"pk": f"CUSTOMER#{lead.id}", "sk": "PROFILE", "updated_at": "year-simulation"}
    item.update({f"t_{k}": v for k, v in lead.traits.items()})
    item.update({f"c_{k}": v for k, v in lead.consent.items()})
    item.update({f"s_{k}": v for k, v in lead.state.items()})
    return json.loads(json.dumps(item), parse_float=Decimal)


def link_items(lead):
    identifiers = identity.identifiers({"user_id": lead.id, "email": lead.traits.get("email")})
    return [{"pk": f"ID#{identifier}", "sk": "LINK", "customer_id": lead.id} for identifier in identifiers]


def write_table(table_name, leads, days, pending):
    items = []
    for lead in leads.values():
        items.append(profile_item(lead))
        if lead.received_day >= 0 and lead.id.startswith("lead-"):
            items.extend(link_items(lead))
    # Work still in progress on the last day carries on in AWS.
    for day, entries in pending["mail"].items():
        for _, message, outcome in entries:
            fields = {
                k: message.get(k)
                for k in (
                    "message_id",
                    "customer_id",
                    "touch",
                    "channel",
                    "asset_id",
                    "features",
                    "model_version",
                    "selection",
                    "image_id",
                    "image_theme",
                    "content_selection",
                    "send_day",
                    "delivery_day",
                )
            }
            fields["outcome"] = outcome
            items.append({"pk": f"MAILDUE#{day}", "sk": message["message_id"], "feedback": json.dumps(fields)})
    for day, entries in pending["registrations"].items():
        for lead in entries:
            items.append({"pk": f"REGDUE#{day}", "sk": lead.id, "customer_id": lead.id})

    def write(chunk):
        table = boto3.resource("dynamodb").Table(table_name)
        with table.batch_writer() as batch:
            for item in chunk:
                batch.put_item(Item=item)

    chunks = [items[start : start + 5000] for start in range(0, len(items), 5000)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(write, chunks))
    boto3.resource("dynamodb").Table(table_name).put_item(
        Item={**daily.CLOCK_KEY, "day": days, "start_date": START_DATE}
    )
    return len(items)


def summarize(snapshots, history, email_rows, mail_rows):
    final = snapshots[-1]
    print(json.dumps({key: final[key] for key in final if key != "forecast"}, indent=1))
    rows = email_rows + mail_rows
    print(f"messages {len(history)}, email outcomes {len(email_rows)}, mail outcomes {len(mail_rows)}")
    groups = collections.defaultdict(lambda: [0, 0, 0])
    for row in rows:
        group = groups[row["content_selection"]]
        group[0] += 1
        group[1] += int(row["clicked"])
        group[2] += int(row["registered"])
    print("image selection (messages, clicks, registered):", dict(groups))


def main():
    arguments = [a for a in sys.argv[1:] if a != "--dry-run"]
    if DRY_RUN:
        people = int(arguments[0]) if arguments else 1000
        days = int(arguments[1]) if len(arguments) > 1 else 60
        leads = []
        for entry in sample_data.generate(people, seed=42):
            leads.append(Lead(entry["person"]["user_id"], sample_data.traits_of(entry), entry["consent"], 0))
        result = run(leads, days)
        summarize(result[5], result[2], result[3], result[4])
        return

    if len(arguments) < 2:
        sys.exit(__doc__)
    table_name, bucket = arguments[0], arguments[1]
    days = int(arguments[2]) if len(arguments) > 2 else 365
    import boto3.dynamodb.conditions  # noqa: F401

    leads = read_leads(table_name)
    print(f"read {len(leads)} existing leads", flush=True)
    by_id, model, history, email_rows, mail_rows, snapshots, pending = run(leads, days)
    summarize(snapshots, history, email_rows, mail_rows)

    s3 = boto3.client("s3")
    for prefix, rows in (
        ("history/", history),
        ("feedback/email/", email_rows),
        ("feedback/direct_mail/", mail_rows),
    ):
        print(f"{prefix}: {_put_rows(s3, bucket, prefix, rows)} files", flush=True)
    for snapshot in snapshots:
        s3.put_object(
            Bucket=bucket,
            Key=f"{nba_export.DAILY_PREFIX}day={snapshot['day'] + 1:05d}.json",
            Body=json.dumps(snapshot).encode(),
            ContentType="application/json",
        )
    s3.put_object(
        Bucket=bucket,
        Key="models/nba/latest.json",
        Body=json.dumps(model).encode(),
        ContentType="application/json",
    )
    print(f"wrote {write_table(table_name, by_id, days, pending)} table records", flush=True)


if __name__ == "__main__":
    main()
