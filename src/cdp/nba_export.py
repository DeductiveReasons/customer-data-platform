"""Scheduled or manual handler: writes the people table and publishes dashboard totals."""
import gzip
import json
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr

from cdp import approvals, daily, nba

FORECAST_DAYS = 14
DAILY_PREFIX = "analytics/daily/"
from cdp.nba_scan import profile_message


class _NeverExplore:
    def random(self):
        return 1.0


def _status(state, consent, decision):
    if state.get("registered"):
        return "onboarding_due" if decision else "registered"
    if decision:
        return "active"
    if consent.get("marketing") is not True:
        return "no_marketing_consent"
    if state.get("touches", 0) >= nba.MAX_TOUCHES:
        return "journey_complete"
    return "no_contact_option"


def person_row(item, snapshot_at, model, pending=frozenset(), clock=None):
    clock = clock or {"day": -1, "start_date": daily.DEFAULT_START}
    profile = profile_message(item, "export")
    traits, consent, state = profile["traits"], profile["consent"], profile["state"]
    decision = nba.decide(profile["customer_id"], traits, consent, state, model, _NeverExplore(), pending)
    send_day = max(clock["day"] + 1, int(state.get("next_due", 0))) if decision else None
    return {
        "customer_id": profile["customer_id"],
        "first_name": traits.get("first_name"),
        "last_name": traits.get("last_name"),
        "email": traits.get("email"),
        "address": traits.get("address"),
        "city": traits.get("city"),
        "postal_code": traits.get("postal_code"),
        "state": traits.get("state"),
        "age": traits.get("age"),
        "tenure_months": traits.get("tenure_months"),
        "app_sessions_30d": traits.get("app_sessions_30d"),
        "days_since_active": traits.get("days_since_active"),
        "email_open_rate": traits.get("email_open_rate"),
        "wellness_member": traits.get("wellness_member"),
        "employer_id": traits.get("employer_id"),
        "employer_name": traits.get("employer_name"),
        "employer_industry": traits.get("employer_industry"),
        "employer_employees": traits.get("employer_employees"),
        "employer_state": traits.get("employer_state"),
        "employer_wellness_tier": traits.get("employer_wellness_tier"),
        "employer_fitness_subsidy": traits.get("employer_fitness_subsidy"),
        "employer_participation_rate": traits.get("employer_participation_rate"),
        "employer_remote_share": traits.get("employer_remote_share"),
        "consent_marketing": consent.get("marketing") is True,
        "consent_email": consent.get("email") is True,
        "consent_direct_mail": consent.get("direct_mail") is True,
        "touches": state.get("touches", 0),
        "opens": state.get("opens", 0),
        "clicks": state.get("clicks", 0),
        "registered": state.get("registered") is True,
        "onboarded": state.get("onboarded") is True,
        "last_asset": state.get("last_asset"),
        "status": _status(state, consent, decision),
        "next_best_action": decision["channel"] if decision else "no_contact",
        "next_send_day": send_day,
        "next_best_action_time": (
            nba.send_time_on(traits, decision["channel"], daily.sim_date(clock["start_date"], send_day))
            if decision
            else None
        ),
        "lead_source": traits.get("lead_source"),
        "lead_received_date": traits.get("lead_received_date"),
        "last_channel": state.get("last_channel"),
        "last_send_day": state.get("last_send_day"),
        "mail_in_transit": (
            state.get("last_channel") == "direct_mail"
            and state.get("last_send_day") is not None
            and state["last_send_day"] + nba.DIRECT_MAIL_LEAD_DAYS > clock["day"]
        ),
        "no_contact_reason": None if decision else _status(state, consent, decision),
        "next_best_asset": decision["asset_id"] if decision else None,
        "next_best_channel": decision["channel"] if decision else None,
        "next_best_propensity": decision.get("propensity") if decision else None,
        "next_best_expected_value": decision.get("expected_value") if decision else None,
        "next_best_image": decision["image_id"] if decision else None,
        "next_best_image_theme": decision["image_theme"] if decision else None,
        "model_version": model["version"],
        "snapshot_at": snapshot_at,
    }


def _load_model(s3):
    try:
        body = s3.get_object(Bucket=os.environ["BUCKET"], Key=os.environ["MODEL_KEY"])["Body"]
        return json.loads(body.read())
    except s3.exceptions.ClientError:
        return nba.BUNDLED_MODEL


def _metrics(rows):
    def count(test):
        return sum(1 for row in rows if test(row))

    totals = {
        "People": len(rows),
        "PeopleContacted": count(lambda r: r["touches"] > 0),
        "PeopleRegistered": count(lambda r: r["registered"]),
        "PeopleWithNextAction": count(lambda r: r["next_best_asset"]),
    }
    return [{"MetricName": name, "Value": value, "Unit": "Count"} for name, value in totals.items()]


def daily_snapshot(rows, clock):
    """Summarize the day and forecast sends from each person's next best action at this moment."""
    day = clock["day"]
    actions = {"email": 0, "direct_mail": 0, "no_contact": 0}
    sends = {}
    deliveries = {}
    for row in rows:
        actions[row["next_best_action"]] = actions.get(row["next_best_action"], 0) + 1
        send_day = row["next_send_day"]
        if send_day is not None:
            bucket = sends.setdefault(send_day, {"email": 0, "direct_mail": 0})
            bucket[row["next_best_action"]] += 1
            if row["next_best_action"] == "direct_mail":
                arrive = send_day + nba.DIRECT_MAIL_LEAD_DAYS
                deliveries[arrive] = deliveries.get(arrive, 0) + 1
        # Mail already on its way is known for certain.
        if row["last_channel"] == "direct_mail" and row["last_send_day"] is not None:
            arrive = row["last_send_day"] + nba.DIRECT_MAIL_LEAD_DAYS
            if arrive > day:
                deliveries[arrive] = deliveries.get(arrive, 0) + 1
    forecast = []
    for ahead in range(1, FORECAST_DAYS + 1):
        target = day + ahead
        planned = sends.get(target, {})
        forecast.append(
            {
                "day": target,
                "date": daily.sim_date(clock["start_date"], target),
                "email_sends": planned.get("email", 0),
                "direct_mail_sends": planned.get("direct_mail", 0),
                "direct_mail_deliveries": deliveries.get(target, 0),
            }
        )
    people = len(rows)
    registered = sum(1 for row in rows if row["registered"])
    contacted = sum(1 for row in rows if row["touches"] > 0)
    return {
        "day": day,
        "date": daily.sim_date(clock["start_date"], day),
        "people": people,
        "never_contacted": people - contacted,
        "contacted": contacted,
        "registered": registered,
        "registration_rate": round(registered / people, 5) if people else 0,
        "registration_rate_contacted": round(registered / contacted, 5) if contacted else 0,
        "nba_email": actions.get("email", 0),
        "nba_direct_mail": actions.get("direct_mail", 0),
        "nba_no_contact": actions.get("no_contact", 0),
        "mail_in_transit": sum(1 for row in rows if row["mail_in_transit"]),
        "forecast": forecast,
    }


def handler(_event, _context):
    table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    s3 = boto3.client("s3")
    model = _load_model(s3)
    clock = daily.get_clock(table)
    snapshot_at = datetime.now(timezone.utc).isoformat()

    pending = approvals.pending_ids()
    rows = []
    kwargs = {"FilterExpression": Attr("sk").eq("PROFILE")}
    while True:
        page = table.scan(**kwargs)
        rows.extend(person_row(item, snapshot_at, model, pending, clock) for item in page["Items"])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    body = "".join(json.dumps(row) + "\n" for row in rows).encode()
    s3.put_object(Bucket=os.environ["BUCKET"], Key=os.environ["PEOPLE_KEY"], Body=gzip.compress(body))
    snapshot = daily_snapshot(rows, clock)
    s3.put_object(
        Bucket=os.environ["BUCKET"],
        Key=f"{DAILY_PREFIX}day={clock['day'] + 1:05d}.json",
        Body=json.dumps(snapshot).encode(),
        ContentType="application/json",
    )
    boto3.client("cloudwatch").put_metric_data(Namespace=os.environ["METRIC_NAMESPACE"], MetricData=_metrics(rows))
    return {"people": len(rows), "snapshot_at": snapshot_at, "day": clock["day"]}
