"""Password-protected web app for performance, assets, people and monitoring."""
import base64
import hmac
import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone

import boto3

from cdp import approvals, assets, content, daily, nba

with open(os.path.join(os.path.dirname(__file__), "webapp.html"), encoding="utf-8") as page_file:
    PAGE = page_file.read()
with open(os.path.join(os.path.dirname(__file__), "model_card.json"), encoding="utf-8") as card_file:
    MODEL_CARD = json.load(card_file)

CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data: {host}; "
    "connect-src 'self'; frame-src 'self' about:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
IMAGE_RE = re.compile(r"^/images/([a-z_]+-[0-9]{2})\.jpg$")
CACHE_SECONDS = 60
ATHENA_WAIT_SECONDS = 22
CUSTOMER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
STATUSES = {"active", "onboarding_due", "registered", "journey_complete", "no_marketing_consent", "no_contact_option"}
PEOPLE_LIMIT = 100

FUNNEL_SQL = """
SELECT channel, asset_id,
       COUNT(DISTINCT message_id) AS delivered,
       COUNT(DISTINCT CASE WHEN opened THEN message_id END) AS opened,
       COUNT(DISTINCT CASE WHEN clicked THEN message_id END) AS clicked,
       COUNT(DISTINCT CASE WHEN registered THEN message_id END) AS registered,
       COUNT(DISTINCT CASE WHEN unsubscribed THEN message_id END) AS unsubscribed
FROM (SELECT * FROM email_engagement UNION ALL SELECT * FROM direct_mail_engagement)
GROUP BY channel, asset_id ORDER BY registered DESC
"""
ACTION_SQL = "SELECT next_best_action AS action, COUNT(*) AS people FROM people GROUP BY next_best_action"
STATUS_SQL = "SELECT status, COUNT(*) AS people FROM people GROUP BY status ORDER BY people DESC"
NEXT_ACTION_SQL = """
SELECT next_best_asset AS asset_id, next_best_action AS channel, COUNT(*) AS people,
       ROUND(AVG(next_best_propensity), 4) AS avg_propensity,
       ROUND(AVG(next_best_expected_value), 2) AS avg_expected_value
FROM people WHERE next_best_asset IS NOT NULL
GROUP BY next_best_asset, next_best_action ORDER BY people DESC
"""
TOTALS_SQL = """
SELECT COUNT(*) AS people,
       COUNT_IF(touches > 0) AS contacted,
       COUNT_IF(registered) AS registered,
       MAX(snapshot_at) AS snapshot_at,
       MAX(model_version) AS model_version
FROM people
"""
CONTENT_SQL = """
SELECT image_theme, image_id,
       COUNT(DISTINCT message_id) AS delivered,
       COUNT(DISTINCT CASE WHEN clicked THEN message_id END) AS clicked,
       COUNT(DISTINCT CASE WHEN registered THEN message_id END) AS registered
FROM (SELECT * FROM email_engagement UNION ALL SELECT * FROM direct_mail_engagement)
WHERE image_id IS NOT NULL
GROUP BY image_theme, image_id
"""
PEOPLE_COLUMNS = (
    "customer_id, first_name, last_name, employer_name, status, touches, opens, clicks, registered, "
    "next_best_action, next_best_action_time, no_contact_reason, next_best_asset, next_best_channel, "
    "next_best_propensity, next_best_expected_value, next_best_image"
)
HISTORY_SQL = """
SELECT DISTINCT h.message_id, h.decided_at, h.touch, h.asset_id, h.channel, h.selection, h.propensity,
       h.expected_value, h.trigger, h.content_score,
       h.image_id, h.image_theme,
       COALESCE(e.opened, d.opened) AS opened,
       COALESCE(e.clicked, d.clicked) AS clicked,
       COALESCE(e.registered, d.registered) AS registered,
       COALESCE(e.unsubscribed, d.unsubscribed) AS unsubscribed
FROM nba_history h
LEFT JOIN email_engagement e ON h.message_id = e.message_id
LEFT JOIN direct_mail_engagement d ON h.message_id = d.message_id
WHERE h.customer_id = ?
ORDER BY h.decided_at
"""

JOURNEY_MIN_PEOPLE = 30
JOURNEY_LIMIT = 300
JOURNEY_BASE_SQL = """
WITH sends AS (
  SELECT DISTINCT customer_id, message_id, TRY_CAST(touch AS integer) AS step, asset_id, channel
  FROM nba_history WHERE touch <> 'onboarding'
),
outcomes AS (
  SELECT message_id, BOOL_OR(registered) AS registered
  FROM (SELECT message_id, registered FROM email_engagement
        UNION ALL SELECT message_id, registered FROM direct_mail_engagement)
  GROUP BY message_id
),
steps AS (
  SELECT s.customer_id, s.step,
         COALESCE(o.registered, false) AS did_register,
         ARRAY_AGG(s.asset_id || ':' || s.channel) OVER (
           PARTITION BY s.customer_id ORDER BY s.step ROWS UNBOUNDED PRECEDING
         ) AS sequence
  FROM sends s LEFT JOIN outcomes o ON s.message_id = o.message_id
  WHERE s.step IS NOT NULL
)
"""
JOURNEY_SQL = (
    JOURNEY_BASE_SQL
    + f"""
SELECT ARRAY_JOIN(sequence, '>') AS journey, step AS steps,
       COUNT(*) AS reached, COUNT_IF(did_register) AS registered
FROM steps
GROUP BY sequence, step
HAVING COUNT(*) >= {JOURNEY_MIN_PEOPLE}
ORDER BY CAST(COUNT_IF(did_register) AS double) / COUNT(*) DESC
LIMIT {JOURNEY_LIMIT}
"""
)
JOURNEY_STEP_SQL = (
    JOURNEY_BASE_SQL
    + """
SELECT step AS steps, COUNT(*) AS reached, COUNT_IF(did_register) AS registered
FROM steps GROUP BY step ORDER BY step
"""
)
BANDIT_SQL = """
SELECT content_selection AS selection,
       COUNT(DISTINCT message_id) AS messages,
       COUNT(DISTINCT CASE WHEN clicked THEN message_id END) AS clicked,
       COUNT(DISTINCT CASE WHEN registered THEN message_id END) AS registered
FROM (SELECT * FROM email_engagement UNION ALL SELECT * FROM direct_mail_engagement)
WHERE content_selection IS NOT NULL
GROUP BY content_selection
"""
DAILY_SENDS_SQL = """
SELECT send_day AS day, channel, COUNT(DISTINCT message_id) AS sends
FROM nba_history
WHERE send_day IS NOT NULL
GROUP BY send_day, channel
ORDER BY send_day, channel
"""
ENGAGEMENT_SQL = "(SELECT * FROM email_engagement UNION ALL SELECT * FROM direct_mail_engagement)"
# Winners use only randomized rows. The model's own choices favor people likely to register anyway.
INSIGHT_QUERIES = {
    "offers": f"""
SELECT asset_id AS name, COUNT(DISTINCT message_id) AS messages,
       COUNT(DISTINCT CASE WHEN clicked THEN message_id END) AS clicked,
       COUNT(DISTINCT CASE WHEN registered THEN message_id END) AS registered
FROM {ENGAGEMENT_SQL} WHERE selection = 'explore' GROUP BY asset_id""",
    "channels": f"""
SELECT channel AS name, COUNT(DISTINCT message_id) AS messages,
       COUNT(DISTINCT CASE WHEN clicked THEN message_id END) AS clicked,
       COUNT(DISTINCT CASE WHEN registered THEN message_id END) AS registered
FROM {ENGAGEMENT_SQL} WHERE selection = 'explore' GROUP BY channel""",
    "themes": f"""
SELECT image_theme AS name, COUNT(DISTINCT message_id) AS messages,
       COUNT(DISTINCT CASE WHEN clicked THEN message_id END) AS clicked,
       COUNT(DISTINCT CASE WHEN registered THEN message_id END) AS registered
FROM {ENGAGEMENT_SQL} WHERE content_selection = 'holdout' GROUP BY image_theme""",
    "bandit": f"""
SELECT content_selection AS name, COUNT(DISTINCT message_id) AS messages,
       COUNT(DISTINCT CASE WHEN clicked THEN message_id END) AS clicked,
       COUNT(DISTINCT CASE WHEN registered THEN message_id END) AS registered
FROM {ENGAGEMENT_SQL} WHERE content_selection IN ('bandit', 'holdout') GROUP BY content_selection""",
    "images": f"""
SELECT image_id AS name, COUNT(DISTINCT message_id) AS messages,
       COUNT(DISTINCT CASE WHEN clicked THEN message_id END) AS clicked,
       COUNT(DISTINCT CASE WHEN registered THEN message_id END) AS registered
FROM {ENGAGEMENT_SQL} WHERE content_selection = 'holdout' GROUP BY image_id""",
    "subsidy_offers": f"""
SELECT CONCAT(CASE WHEN p.employer_fitness_subsidy = 1 THEN 'Subsidy' ELSE 'No subsidy' END, '|', e.asset_id) AS name,
       COUNT(DISTINCT e.message_id) AS messages,
       COUNT(DISTINCT CASE WHEN e.clicked THEN e.message_id END) AS clicked,
       COUNT(DISTINCT CASE WHEN e.registered THEN e.message_id END) AS registered
FROM {ENGAGEMENT_SQL} e JOIN people p ON e.customer_id = p.customer_id
WHERE e.selection = 'explore' GROUP BY 1""",
    "industries": f"""
SELECT CONCAT(p.employer_industry, '|', e.image_theme) AS name,
       COUNT(DISTINCT e.message_id) AS messages,
       COUNT(DISTINCT CASE WHEN e.clicked THEN e.message_id END) AS clicked,
       COUNT(DISTINCT CASE WHEN e.registered THEN e.message_id END) AS registered
FROM {ENGAGEMENT_SQL} e JOIN people p ON e.customer_id = p.customer_id
WHERE e.content_selection = 'holdout' GROUP BY 1""",
    "activity": f"""
SELECT CONCAT(CASE WHEN p.app_sessions_30d >= 5 THEN 'Active app users' ELSE 'Less active users' END, '|', e.image_theme) AS name,
       COUNT(DISTINCT e.message_id) AS messages,
       COUNT(DISTINCT CASE WHEN e.clicked THEN e.message_id END) AS clicked,
       COUNT(DISTINCT CASE WHEN e.registered THEN e.message_id END) AS registered
FROM {ENGAGEMENT_SQL} e JOIN people p ON e.customer_id = p.customer_id
WHERE e.content_selection = 'holdout' GROUP BY 1""",
}
TEST_MIN_MESSAGES = 200
BANDIT_BY_DAY_SQL = f"""
SELECT send_day AS day, content_selection AS selection,
       COUNT(DISTINCT message_id) AS messages,
       COUNT(DISTINCT CASE WHEN clicked THEN message_id END) AS clicked,
       COUNT(DISTINCT CASE WHEN registered THEN message_id END) AS registered
FROM {ENGAGEMENT_SQL}
WHERE content_selection IN ('bandit', 'holdout') AND send_day IS NOT NULL
GROUP BY send_day, content_selection ORDER BY send_day
"""
BANDIT_THEME_BY_DAY_SQL = f"""
SELECT send_day AS day, image_theme AS theme, COUNT(DISTINCT message_id) AS messages
FROM {ENGAGEMENT_SQL}
WHERE content_selection = 'bandit' AND send_day IS NOT NULL
GROUP BY send_day, image_theme ORDER BY send_day
"""
BANDIT_THEME_SQL = f"""
SELECT image_theme AS theme, content_selection AS selection,
       COUNT(DISTINCT message_id) AS messages,
       COUNT(DISTINCT CASE WHEN clicked THEN message_id END) AS clicked,
       COUNT(DISTINCT CASE WHEN registered THEN message_id END) AS registered
FROM {ENGAGEMENT_SQL}
WHERE content_selection IN ('bandit', 'holdout')
GROUP BY image_theme, content_selection
"""
COST_CACHE_SECONDS = 3600  # Each Cost Explorer request costs about one cent.
USAGE_DAYS = 30
VARIATION_PAGE_SIZE = 12
RECORDS_SQL = """
SELECT
  (SELECT COUNT(*) FROM people) AS people,
  (SELECT COUNT(DISTINCT message_id) FROM nba_history) AS messages_sent,
  (SELECT COUNT(DISTINCT message_id) FROM email_engagement) AS email_outcomes,
  (SELECT COUNT(DISTINCT message_id) FROM direct_mail_engagement) AS direct_mail_outcomes
"""
S3_AREAS = {
    "events/": "Raw customer events",
    "history/": "Message history",
    "feedback/": "Engagement feedback",
    "analytics/": "People table",
    "content-library/": "Image library",
    "models/": "Published models",
    "athena-results/": "Query results",
}
# Each service, what it does here, and the name Cost Explorer reports it under.
SERVICES = [
    ("API Gateway", "Ingestion, receiver and web app APIs", ("API Gateway",)),
    ("Lambda", "Validation, decisions, feedback, delivery and web app code", ("Lambda",)),
    ("EventBridge", "Routes events and keeps the replay archive", ("EventBridge", "CloudWatch Events")),
    ("SQS", "Queues between every step, with failed-message queues", ("Simple Queue Service",)),
    ("Step Functions", "Identity, consent and routing workflow", ("Step Functions",)),
    ("DynamoDB", "Customer profiles, identities and received messages", ("DynamoDB",)),
    ("S3", "Raw events, history, feedback, images and models", ("Simple Storage Service",)),
    ("Athena", "SQL queries over the four tables", ("Athena",)),
    ("Glue Data Catalog", "Table definitions for Athena", ("Glue",)),
    ("Bedrock", "Generates the AI image library (us-west-2)", ("Bedrock", "Stable Image")),
    ("CloudWatch", "Logs, metrics, alarms and dashboard", ("CloudWatch",)),
    ("SNS", "Alarm notifications", ("Simple Notification Service",)),
    ("Secrets Manager", "Passwords and destination credentials", ("Secrets Manager",)),
    ("IAM", "Permissions for every function", ()),
    ("Budgets", "$50 monthly billing alert", ("Budgets",)),
]

_cache = {}
_token = None


class BadRequest(ValueError):
    pass


def _get_token():
    global _token
    if _token is None:
        secret = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["SECRET_ARN"])
        _token = secret["SecretString"]
    return _token


def _authorized(event):
    header = (event.get("headers") or {}).get("authorization", "")
    return hmac.compare_digest(header.encode(), f"Bearer {_get_token()}".encode())


def _response(status, body, content_type="application/json", host=""):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": content_type,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": CSP.format(host=f"https://{host}" if host else ""),
        },
        "body": body if isinstance(body, str) else json.dumps(body, default=str),
    }


def _cached(key, loader):
    entry = _cache.get(key)
    if entry and time.time() - entry[0] < CACHE_SECONDS:
        return entry[1]
    value = loader()
    _cache[key] = (time.time(), value)
    return value


def run_query(sql, parameters=None):
    athena = boto3.client("athena")
    kwargs = {
        "QueryString": sql,
        "WorkGroup": os.environ["ATHENA_WORKGROUP"],
        "QueryExecutionContext": {"Database": os.environ["ATHENA_DATABASE"]},
    }
    if parameters:
        kwargs["ExecutionParameters"] = parameters
    query_id = athena.start_query_execution(**kwargs)["QueryExecutionId"]

    deadline = time.time() + ATHENA_WAIT_SECONDS
    while True:
        status = athena.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]["Status"]
        if status["State"] == "SUCCEEDED":
            break
        if status["State"] in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"query {status['State'].lower()}: {status.get('StateChangeReason', '')[:200]}")
        if time.time() > deadline:
            raise TimeoutError("query is still running")
        time.sleep(0.4)

    rows = athena.get_query_results(QueryExecutionId=query_id, MaxResults=1000)["ResultSet"]["Rows"]
    if not rows:
        return []
    header = [cell.get("VarCharValue") for cell in rows[0]["Data"]]
    return [dict(zip(header, (cell.get("VarCharValue") for cell in row["Data"]))) for row in rows[1:]]


def _endpoints():
    events_url = os.environ.get("EVENTS_URL", "")
    receiver = os.environ.get("RECEIVER_URL", "").rstrip("/")
    return [
        {"group": "Customer data", "method": "POST", "url": events_url, "auth": "API key", "purpose": "Send one customer event or a batch of up to 100"},
        {"group": "Dummy receiver", "method": "POST", "url": f"{receiver}/email", "auth": "Bearer token", "purpose": "Receives email requests with HTML. Sends nothing"},
        {"group": "Dummy receiver", "method": "POST", "url": f"{receiver}/direct-mail", "auth": "Bearer token", "purpose": "Receives direct mail requests with HTML. Sends nothing"},
        {"group": "Dummy receiver", "method": "GET", "url": f"{receiver}/messages", "auth": "Bearer token", "purpose": "Lists a sample of the requests received"},
        {"group": "Console", "method": "GET", "url": "/api/overview", "auth": "Password", "purpose": "Totals, registration rate and endpoint library"},
        {"group": "Console", "method": "GET", "url": "/api/performance", "auth": "Password", "purpose": "Funnels and current next best actions"},
        {"group": "Console", "method": "GET", "url": "/api/assets", "auth": "Password", "purpose": "Campaign asset names and previews"},
        {"group": "Console", "method": "GET", "url": "/api/variations", "auth": "Password", "purpose": "Browse every deliverable variation and its approval status"},
        {"group": "Console", "method": "POST", "url": "/api/variations/{id}/status", "auth": "Password", "purpose": "Approve a variation or set it to pending approval"},
        {"group": "Console", "method": "GET", "url": "/api/bandit", "auth": "Password", "purpose": "Bandit test outcomes against the random holdout"},
        {"group": "Console", "method": "GET", "url": "/api/insights", "auth": "Password", "purpose": "Test winners and segment insights"},
        {"group": "Console", "method": "GET", "url": "/api/daily", "auth": "Password", "purpose": "Daily results and send forecast"},
        {"group": "Console", "method": "GET", "url": "/api/model", "auth": "Password", "purpose": "Next best action model results and top features"},
        {"group": "Console", "method": "GET", "url": "/api/journeys", "auth": "Password", "purpose": "Most successful message sequences"},
        {"group": "Console", "method": "GET", "url": "/api/content", "auth": "Password", "purpose": "Image library results"},
        {"group": "Console", "method": "GET", "url": "/api/people", "auth": "Password", "purpose": "Search and filter people"},
        {"group": "Console", "method": "GET", "url": "/api/people/{id}", "auth": "Password", "purpose": "One person's profile, scores and messages"},
        {"group": "Console", "method": "GET", "url": "/api/monitoring", "auth": "Password", "purpose": "Queues, functions and alarms"},
        {"group": "Console", "method": "GET", "url": "/api/usage", "auth": "Password", "purpose": "Services, costs, records and API calls"},
        {"group": "Console", "method": "GET", "url": "/images/{id}.jpg", "auth": "Public", "purpose": "Library images used in messages"},
    ]


def _overview():
    totals = run_query(TOTALS_SQL)
    return {
        "totals": totals[0] if totals else {},
        "statuses": run_query(STATUS_SQL),
        "actions": run_query(ACTION_SQL),
        "funnel": run_query(FUNNEL_SQL),
        "endpoints": _endpoints(),
    }


def _performance():
    return {"funnel": run_query(FUNNEL_SQL), "next_actions": run_query(NEXT_ACTION_SQL)}


def _assets():
    library = []
    for asset_id, asset in assets.ASSETS.items():
        library.append(
            {
                "id": asset_id,
                **asset,
                "email_html": assets.render(asset_id, "email", "Alex", "preview"),
                "direct_mail_html": assets.render(asset_id, "direct_mail", "Alex", "preview"),
            }
        )
    return {"brand": assets.BRAND, "assets": library, "funnel": _cached("funnel", lambda: run_query(FUNNEL_SQL))}


def _content():
    results = {row["image_id"]: row for row in _cached("content", lambda: run_query(CONTENT_SQL))}
    images = []
    for image in content.all_images():
        row = results.get(image["id"], {})
        images.append(
            {
                **image,
                "delivered": int(row.get("delivered") or 0),
                "clicked": int(row.get("clicked") or 0),
                "registered": int(row.get("registered") or 0),
            }
        )
    return {"images": images}


def _variations(query):
    records = approvals.stored_records()
    asset_filter = query.get("asset") or ""
    channel_filter = query.get("channel") or ""
    theme_filter = query.get("theme") or ""
    status_filter = query.get("status") or ""
    if asset_filter and asset_filter not in assets.ASSETS:
        raise BadRequest("unknown asset")
    if channel_filter and channel_filter not in approvals.CHANNELS:
        raise BadRequest("unknown channel")
    if theme_filter and theme_filter not in content.THEMES:
        raise BadRequest("unknown theme")
    if status_filter and status_filter not in (approvals.APPROVED, approvals.PENDING):
        raise BadRequest("unknown status")
    try:
        page = max(1, int(query.get("page") or 1))
    except ValueError as exc:
        raise BadRequest("invalid page") from exc

    totals = {approvals.APPROVED: 0, approvals.PENDING: 0}
    matches = []
    for asset_id, channel, image in approvals.all_variations():
        key = approvals.variation_id(asset_id, channel, image["id"])
        record = records.get(key, {})
        status = record.get("status", approvals.APPROVED)
        totals[status] = totals.get(status, 0) + 1
        if (
            (not asset_filter or asset_id == asset_filter)
            and (not channel_filter or channel == channel_filter)
            and (not theme_filter or image["theme"] == theme_filter)
            and (not status_filter or status == status_filter)
        ):
            matches.append(
                {
                    "id": key,
                    "asset_id": asset_id,
                    "asset": assets.ASSETS[asset_id]["name"],
                    "channel": channel,
                    "image_id": image["id"],
                    "theme": image["label"],
                    "subject": assets.ASSETS[asset_id]["subject"],
                    "cta": assets.ASSETS[asset_id]["cta"],
                    "status": status,
                    "changed_by": record.get("changed_by", approvals.AUTO_APPROVER),
                }
            )
    start = (page - 1) * VARIATION_PAGE_SIZE
    shown = matches[start : start + VARIATION_PAGE_SIZE]
    for item in shown:
        item["html"] = assets.render(
            item["asset_id"], item["channel"], "Alex", "preview", f"/images/{item['image_id']}.jpg", item["theme"]
        )
    return {
        "total": len(approvals.all_variations()),
        "totals": totals,
        "matching": len(matches),
        "page": page,
        "page_size": VARIATION_PAGE_SIZE,
        "variations": shown,
        "assets": {asset_id: asset["name"] for asset_id, asset in assets.ASSETS.items()},
        "themes": {theme: config["label"] for theme, config in content.THEMES.items()},
    }


def _set_variation_status(variation, body):
    if approvals.parse(variation) is None:
        raise BadRequest("unknown variation")
    try:
        status = json.loads(body or "{}").get("status")
    except (ValueError, AttributeError) as exc:
        raise BadRequest("invalid request") from exc
    if status not in (approvals.APPROVED, approvals.PENDING):
        raise BadRequest("unknown status")
    approvals.set_status(variation, status)
    return {"id": variation, "status": status}


def _model_page():
    """Add the bandit's live state and holdout comparison to the training results."""
    current = _current_model().get("content") or content.BUNDLED_MODEL
    arms = []
    if current.get("type") == "bandit":
        arms = [
            {
                "theme": theme,
                "label": content.THEMES[theme]["label"],
                "messages": entry["pulls"],
                "clicks": entry["rewards"],
            }
            for theme, entry in current["arms"].items()
            if theme in content.THEMES
        ]
        arms.sort(key=lambda item: item["messages"], reverse=True)
    try:
        selections = run_query(BANDIT_SQL)
    except Exception as exc:
        print(f"bandit comparison failed: {type(exc).__name__}")
        selections = []
    return {**MODEL_CARD, "bandit_version": current.get("version"), "bandit_arms": arms, "bandit_selections": selections}


def _daily():
    s3 = boto3.client("s3")
    bucket = os.environ["IMAGE_BUCKET"]
    snapshots = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="analytics/daily/"):
        for item in page.get("Contents", []):
            snapshots.append(json.loads(s3.get_object(Bucket=bucket, Key=item["Key"])["Body"].read()))
    snapshots.sort(key=lambda snapshot: snapshot["day"])
    return {
        "snapshots": snapshots,
        "actual_sends": run_query(DAILY_SENDS_SQL),
        "lead_days": nba.DIRECT_MAIL_LEAD_DAYS,
    }


def compare(rows, metric):
    """Rank a test and check whether the winner differs from the runner-up, using a two-proportion z-test."""
    ranked = []
    for row in rows:
        messages = int(row.get("messages") or 0)
        if messages:
            ranked.append(
                {
                    "name": row["name"],
                    "messages": messages,
                    "clicked": int(row.get("clicked") or 0),
                    "registered": int(row.get("registered") or 0),
                    "rate": int(row.get(metric) or 0) / messages,
                }
            )
    ranked.sort(key=lambda item: item["rate"], reverse=True)
    eligible = [item for item in ranked if item["messages"] >= TEST_MIN_MESSAGES]
    verdict = "not enough data"
    p_value = None
    lift = None
    if len(eligible) >= 2:
        first, second = eligible[0], eligible[1]
        pooled = (first["rate"] * first["messages"] + second["rate"] * second["messages"]) / (
            first["messages"] + second["messages"]
        )
        variance = pooled * (1 - pooled) * (1 / first["messages"] + 1 / second["messages"])
        if variance > 0:
            z = (first["rate"] - second["rate"]) / math.sqrt(variance)
            p_value = round(math.erfc(z / math.sqrt(2)), 5)
            verdict = "clear winner" if p_value < 0.01 else "likely winner" if p_value < 0.05 else "no clear winner"
        if second["rate"]:
            lift = round(first["rate"] / second["rate"] - 1, 4)
    return {
        "metric": metric,
        "winner": eligible[0]["name"] if eligible else None,
        "runner_up": eligible[1]["name"] if len(eligible) > 1 else None,
        "lift_over_runner_up": lift,
        "p_value": p_value,
        "verdict": verdict,
        "rows": ranked,
    }


def segment_winners(rows, metric):
    """Split rows named 'segment|option' and find each segment's winner."""
    segments = {}
    for row in rows:
        segment, _, option = (row.get("name") or "").partition("|")
        if segment and option:
            segments.setdefault(segment, []).append({**row, "name": option})
    return [{"segment": segment, **compare(items, metric)} for segment, items in sorted(segments.items())]


def _bandit_page():
    selections = run_query(INSIGHT_QUERIES["bandit"])
    by_day = run_query(BANDIT_BY_DAY_SQL)
    theme_days = run_query(BANDIT_THEME_BY_DAY_SQL)
    themes = run_query(BANDIT_THEME_SQL)
    rows = {}
    for row in themes:
        theme = row["theme"]
        entry = rows.setdefault(
            theme,
            {
                "theme": theme,
                "label": content.THEMES.get(theme, {}).get("label", theme),
                "bandit_messages": 0,
                "bandit_clicked": 0,
                "holdout_messages": 0,
                "holdout_clicked": 0,
            },
        )
        entry[f"{row['selection']}_messages"] = int(row["messages"])
        entry[f"{row['selection']}_clicked"] = int(row["clicked"])
    current = _current_model().get("content") or {}
    return {
        "comparison": compare(selections, "clicked"),
        "registration_comparison": compare(selections, "registered"),
        "by_day": by_day,
        "theme_days": theme_days,
        "themes": sorted(rows.values(), key=lambda item: item["bandit_messages"], reverse=True),
        "theme_labels": {theme: config["label"] for theme, config in content.THEMES.items()},
        "version": current.get("version"),
        "start_date": daily.DEFAULT_START,
    }


def _insights():
    data = {name: run_query(sql) for name, sql in INSIGHT_QUERIES.items()}
    images = compare(data["images"], "clicked")
    images["rows"] = images["rows"][:15]
    return {
        "offers": compare(data["offers"], "registered"),
        "channels": compare(data["channels"], "registered"),
        "themes": compare(data["themes"], "clicked"),
        "bandit": compare(data["bandit"], "clicked"),
        "images": images,
        "subsidy_offers": segment_winners(data["subsidy_offers"], "registered"),
        "industries": segment_winners(data["industries"], "clicked"),
        "activity": segment_winners(data["activity"], "clicked"),
        "min_messages": TEST_MIN_MESSAGES,
    }


def _journeys():
    return {
        "journeys": run_query(JOURNEY_SQL),
        "by_step": run_query(JOURNEY_STEP_SQL),
        "min_people": JOURNEY_MIN_PEOPLE,
        "channel_cost": nba.CHANNEL_COST,
    }


def _people(query):
    status = query.get("status") or ""
    search = query.get("customer_id") or ""
    if status and status not in STATUSES:
        raise BadRequest("unknown status")
    if search and not CUSTOMER_ID_RE.match(search):
        raise BadRequest("invalid customer id")

    conditions = []
    parameters = []
    if status:
        conditions.append("status = ?")
        parameters.append(f"'{status}'")
    if search:
        conditions.append("customer_id = ?")
        parameters.append(f"'{search}'")
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = (
        f"SELECT {PEOPLE_COLUMNS} FROM people{where} "
        f"ORDER BY next_best_expected_value DESC NULLS LAST LIMIT {PEOPLE_LIMIT}"
    )
    return {"people": run_query(sql, parameters or None), "limit": PEOPLE_LIMIT}


def _current_model():
    """Use the latest published model, or the packaged one until a retrain has published."""
    try:
        body = boto3.client("s3").get_object(Bucket=os.environ["IMAGE_BUCKET"], Key=os.environ["MODEL_KEY"])["Body"]
        return json.loads(body.read())
    except Exception:
        return nba.BUNDLED_MODEL


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _scores(row, model):
    """Score every asset and image theme for this person, using the same logic as live decisions."""
    traits = {name: _number(row.get(name)) for name in nba.PROFILE_FEATURES}
    traits["employer_industry"] = row.get("employer_industry")
    state = {name: _number(row.get(name)) or 0 for name in ("touches", "opens", "clicks")}
    channels = []
    if row.get("consent_marketing") == "true":
        if row.get("consent_email") == "true":
            channels.append("email")
        if row.get("consent_direct_mail") == "true":
            channels.append("direct_mail")

    offers = []
    for asset_id in assets.SIGNUP_ASSETS:
        for channel in channels:
            probability = nba.propensity(model, asset_id, nba.features_for(traits, state, channel))
            if probability is not None:
                offers.append(
                    {
                        "asset_id": asset_id,
                        "channel": channel,
                        "propensity": round(probability, 4),
                        "expected_value": round(probability * nba.REGISTRATION_VALUE - nba.CHANNEL_COST[channel], 2),
                    }
                )
    offers.sort(key=lambda item: item["expected_value"], reverse=True)

    content_model = model.get("content") or content.BUNDLED_MODEL
    # Theme scores use the person's best current channel, or email if they cannot be contacted.
    features = nba.features_for(traits, state, channels[0] if channels else "email")
    themes = []
    for theme, config in content.THEMES.items():
        probability = content.score(content_model, theme, features)
        if probability is not None:
            themes.append({"theme": theme, "label": config["label"], "click_score": round(probability, 4)})
    themes.sort(key=lambda item: item["click_score"], reverse=True)
    return offers, themes


def _person(customer_id):
    if not CUSTOMER_ID_RE.match(customer_id):
        raise BadRequest("invalid customer id")
    parameter = [f"'{customer_id}'"]
    rows = run_query("SELECT * FROM people WHERE customer_id = ? LIMIT 1", parameter)
    if not rows:
        return {"profile": None, "history": []}
    profile = rows[0]
    history = run_query(HISTORY_SQL, parameter)
    for message in history:
        # Rebuilds the message exactly as sent, from the asset, image and message ID in its history.
        if message.get("asset_id") in assets.ASSETS:
            image_id = message.get("image_id")
            alt = content.THEMES.get(message.get("image_theme"), {}).get("label", "")
            message["content_html"] = assets.render(
                message["asset_id"],
                message.get("channel"),
                profile.get("first_name"),
                message.get("message_id"),
                f"/images/{image_id}.jpg" if image_id else None,
                alt,
            )
    offers, themes = _scores(profile, _current_model())
    return {"profile": profile, "history": history, "offers": offers, "themes": themes}


def _monitoring():
    sqs = boto3.client("sqs")
    queues = []
    for name, url in json.loads(os.environ["QUEUES"]).items():
        attributes = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        queues.append(
            {
                "name": name,
                "failed_queue": name.endswith("-dlq"),
                "waiting": int(attributes["ApproximateNumberOfMessages"]),
                "in_progress": int(attributes["ApproximateNumberOfMessagesNotVisible"]),
            }
        )

    cloudwatch = boto3.client("cloudwatch")
    prefix = os.environ["RESOURCE_PREFIX"] + "-"
    alarms = []
    for page in cloudwatch.get_paginator("describe_alarms").paginate(AlarmNamePrefix=prefix):
        alarms.extend(
            {"name": alarm["AlarmName"], "state": alarm["StateValue"]} for alarm in page["MetricAlarms"]
        )

    now = datetime.now(timezone.utc)
    functions = json.loads(os.environ["FUNCTIONS"])
    queries = [
        {
            "Id": f"m{index}{kind.lower()}",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/Lambda",
                    "MetricName": kind,
                    "Dimensions": [{"Name": "FunctionName", "Value": function}],
                },
                "Period": 86400,
                "Stat": "Sum",
            },
        }
        for index, function in enumerate(functions)
        for kind in ("Invocations", "Errors")
    ]
    # GetMetricData accepts up to 500 queries, well above the two per function requested here.
    results = cloudwatch.get_metric_data(
        MetricDataQueries=queries, StartTime=now - timedelta(days=1), EndTime=now
    )["MetricDataResults"]
    values = {result["Id"]: sum(result["Values"]) for result in results}
    lambdas = [
        {
            "name": function,
            "invocations": int(values.get(f"m{index}invocations", 0)),
            "errors": int(values.get(f"m{index}errors", 0)),
        }
        for index, function in enumerate(functions)
    ]
    return {"queues": queues, "alarms": alarms, "lambdas": lambdas, "dashboard_url": os.environ["DASHBOARD_URL"]}


def _costs():
    now = datetime.now(timezone.utc)
    start = now.replace(day=1)
    if now.day == 1:  # Cost Explorer needs a start date earlier than its end date.
        start = (now - timedelta(days=1)).replace(day=1)
    response = boto3.client("ce", region_name="us-east-1").get_cost_and_usage(
        TimePeriod={"Start": start.strftime("%Y-%m-%d"), "End": (now + timedelta(days=1)).strftime("%Y-%m-%d")},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    amounts = {}
    for period in response["ResultsByTime"]:
        for group in period["Groups"]:
            name = group["Keys"][0]
            amounts[name] = amounts.get(name, 0.0) + float(group["Metrics"]["UnblendedCost"]["Amount"])
    return {"from": start.strftime("%Y-%m-%d"), "by_service": amounts}


def _service_rows(amounts):
    rows = []
    matched = set()
    for name, purpose, cost_names in SERVICES:
        hits = [key for key in amounts if any(part in key for part in cost_names)]
        matched.update(hits)
        rows.append({"name": name, "purpose": purpose, "cost": round(sum(amounts[key] for key in hits), 4)})
    other = [
        {"name": key, "purpose": "Other cost on this AWS account", "cost": round(value, 4)}
        for key, value in amounts.items()
        if key not in matched and abs(value) >= 0.0001
    ]
    return rows + other


def _storage():
    s3 = boto3.client("s3")
    areas = []
    for prefix, label in S3_AREAS.items():
        files = size = 0
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=os.environ["IMAGE_BUCKET"], Prefix=prefix):
            for item in page.get("Contents", []):
                files += 1
                size += item["Size"]
        areas.append({"area": label, "files": files, "bytes": size})
    return areas


def _metric_totals(definitions, days=USAGE_DAYS):
    """Sum CloudWatch metrics. definitions maps a label to (namespace, metric, dimensions)."""
    now = datetime.now(timezone.utc)
    ids = {f"m{index}": label for index, label in enumerate(definitions)}
    queries = [
        {
            "Id": query_id,
            "MetricStat": {
                "Metric": {
                    "Namespace": definitions[label][0],
                    "MetricName": definitions[label][1],
                    "Dimensions": [{"Name": k, "Value": v} for k, v in definitions[label][2].items()],
                },
                "Period": 86400,
                "Stat": "Sum",
            },
        }
        for query_id, label in ids.items()
    ]
    totals = {label: 0 for label in definitions}
    client = boto3.client("cloudwatch")
    for start in range(0, len(queries), 400):
        token = None
        while True:
            kwargs = {"NextToken": token} if token else {}
            response = client.get_metric_data(
                MetricDataQueries=queries[start : start + 400],
                StartTime=now - timedelta(days=days),
                EndTime=now,
                **kwargs,
            )
            for result in response["MetricDataResults"]:
                totals[ids[result["Id"]]] += int(sum(result["Values"]))
            token = response.get("NextToken")
            if not token:
                break
    return totals


def _activity():
    functions = json.loads(os.environ["FUNCTIONS"])
    queues = [name for name in json.loads(os.environ["QUEUES"]) if not name.endswith("-dlq")]
    definitions = {
        "Ingestion API calls": ("AWS/ApiGateway", "Count", {"ApiName": os.environ["REST_API_NAME"], "Stage": "prod"}),
        "Receiver API calls": ("AWS/ApiGateway", "Count", {"ApiId": os.environ["RECEIVER_API_ID"]}),
        "Web app API calls": ("AWS/ApiGateway", "Count", {"ApiId": os.environ["WEBAPP_API_ID"]}),
        "Workflow runs": ("AWS/States", "ExecutionsStarted", {"StateMachineArn": os.environ["STATE_MACHINE_ARN"]}),
    }
    for function in functions:
        definitions[f"lambda:{function}"] = ("AWS/Lambda", "Invocations", {"FunctionName": function})
    for queue in queues:
        definitions[f"queue:{queue}"] = ("AWS/SQS", "NumberOfMessagesSent", {"QueueName": queue})
    totals = _metric_totals(definitions)
    rows = [{"name": label, "count": totals[label]} for label in list(definitions)[:4]]
    rows.append({"name": "Function runs", "count": sum(v for k, v in totals.items() if k.startswith("lambda:"))})
    rows.append({"name": "Queue messages", "count": sum(v for k, v in totals.items() if k.startswith("queue:"))})
    return rows


def _attempt(loader, errors, name, fallback):
    """Keep the rest of the page working if one data source is unavailable."""
    try:
        return loader()
    except Exception as exc:
        print(f"usage section failed, {name}: {type(exc).__name__}: {exc}")
        errors.append(name)
        return fallback


def _usage():
    errors = []
    entry = _cache.get("costs")
    if entry and time.time() - entry[0] < COST_CACHE_SECONDS:
        costs = entry[1]
    else:
        costs = _attempt(_costs, errors, "costs", None)
        if costs is not None:
            _cache["costs"] = (time.time(), costs)
    records = _attempt(lambda: run_query(RECORDS_SQL), errors, "records", [])
    amounts = costs["by_service"] if costs else {}
    return {
        "costs_from": costs["from"] if costs else None,
        "total_cost": round(sum(amounts.values()), 4),
        "services": _service_rows(amounts),
        "records": records[0] if records else {},
        "storage": _attempt(_storage, errors, "storage", []),
        "activity": _attempt(_activity, errors, "activity", []),
        "activity_days": USAGE_DAYS,
        "unavailable": errors,
        "image_count": len(content.all_images()),
    }


def _route(path, query):
    if path == "/api/overview":
        return _cached("overview", _overview)
    if path == "/api/performance":
        return _cached("performance", _performance)
    if path == "/api/assets":
        return _assets()
    if path == "/api/content":
        return _content()
    if path == "/api/usage":
        return _cached("usage", _usage)
    if path == "/api/variations":
        return _variations(query)
    if path == "/api/bandit":
        return _cached("bandit", _bandit_page)
    if path == "/api/insights":
        return _cached("insights", _insights)
    if path == "/api/daily":
        return _cached("daily", _daily)
    if path == "/api/model":
        return _cached("model", _model_page)
    if path == "/api/journeys":
        return _cached("journeys", _journeys)
    if path == "/api/people":
        return _people(query)
    if path == "/api/monitoring":
        return _cached("monitoring", _monitoring)
    match = re.match(r"^/api/people/([^/]+)$", path)
    if match:
        return _person(match.group(1))
    return None


def _image_response(image_id):
    """Serve the stored image, falling back to the illustration if it is not in S3."""
    if content.image_theme(image_id) is None:
        return _response(404, {"error": "not found"})
    try:
        data = boto3.client("s3").get_object(
            Bucket=os.environ["IMAGE_BUCKET"], Key=f"{content.IMAGE_PREFIX}{image_id}.jpg"
        )["Body"].read()
        response = _response(200, base64.b64encode(data).decode(), "image/jpeg")
        response["isBase64Encoded"] = True
    except Exception as exc:
        print(f"image fallback for {image_id}: {type(exc).__name__}")
        response = _response(200, content.render_svg(image_id), "image/svg+xml")
    response["headers"]["Cache-Control"] = "public, max-age=86400"
    response["headers"]["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    return response


def handler(event, _context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")
    host = event.get("requestContext", {}).get("domainName", "")
    status_match = re.match(r"^/api/variations/([^/]+)/status$", path)
    if method == "POST" and status_match:
        if not _authorized(event):
            return _response(401, {"error": "unauthorized"})
        body = event.get("body")
        if event.get("isBase64Encoded") and body:
            body = base64.b64decode(body).decode()
        try:
            return _response(200, _set_variation_status(status_match.group(1), body), host=host)
        except BadRequest as exc:
            return _response(400, {"error": str(exc)})
        except Exception as exc:
            print(f"status change failed: {type(exc).__name__}")
            return _response(500, {"error": "Could not change the status."})
    if method != "GET":
        return _response(405, {"error": "method not allowed"})
    if path == "/":
        return _response(200, PAGE, "text/html; charset=utf-8", host)
    image = IMAGE_RE.match(path)
    if image:
        return _image_response(image.group(1))
    if not _authorized(event):
        return _response(401, {"error": "unauthorized"})
    try:
        data = _route(path, event.get("queryStringParameters") or {})
    except BadRequest as exc:
        return _response(400, {"error": str(exc)})
    except TimeoutError:
        return _response(503, {"error": "The query is taking a while. Try again in a moment."})
    except Exception as exc:
        print(f"request failed on {path}: {type(exc).__name__}: {exc}")
        return _response(500, {"error": "Something went wrong loading this view."})
    if data is None:
        return _response(404, {"error": "not found"})
    return _response(200, data, host=host)
