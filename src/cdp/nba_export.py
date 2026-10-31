"""Scheduled or manual handler: writes the people table and publishes dashboard totals."""
import gzip
import json
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr

from cdp import nba
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


def person_row(item, snapshot_at, model):
    profile = profile_message(item, "export")
    traits, consent, state = profile["traits"], profile["consent"], profile["state"]
    decision = nba.decide(profile["customer_id"], traits, consent, state, model, _NeverExplore())
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
        "next_best_action_time": nba.next_action_time(traits, state, decision["channel"]) if decision else None,
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


def handler(_event, _context):
    table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    s3 = boto3.client("s3")
    model = _load_model(s3)
    snapshot_at = datetime.now(timezone.utc).isoformat()

    rows = []
    kwargs = {"FilterExpression": Attr("sk").eq("PROFILE")}
    while True:
        page = table.scan(**kwargs)
        rows.extend(person_row(item, snapshot_at, model) for item in page["Items"])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    body = "".join(json.dumps(row) + "\n" for row in rows).encode()
    s3.put_object(Bucket=os.environ["BUCKET"], Key=os.environ["PEOPLE_KEY"], Body=gzip.compress(body))
    boto3.client("cloudwatch").put_metric_data(Namespace=os.environ["METRIC_NAMESPACE"], MetricData=_metrics(rows))
    return {"people": len(rows), "snapshot_at": snapshot_at}
