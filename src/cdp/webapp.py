"""Password-protected web app for performance, assets, people and monitoring."""
import base64
import hmac
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import boto3

from cdp import assets, content, nba

with open(os.path.join(os.path.dirname(__file__), "webapp.html"), encoding="utf-8") as page_file:
    PAGE = page_file.read()

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
STATUS_SQL = "SELECT status, COUNT(*) AS people FROM people GROUP BY status ORDER BY people DESC"
NEXT_ACTION_SQL = """
SELECT next_best_asset AS asset_id, next_best_channel AS channel, COUNT(*) AS people,
       ROUND(AVG(next_best_propensity), 4) AS avg_propensity,
       ROUND(AVG(next_best_expected_value), 2) AS avg_expected_value
FROM people WHERE next_best_asset IS NOT NULL
GROUP BY next_best_asset, next_best_channel ORDER BY people DESC
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
    "customer_id, first_name, last_name, status, touches, opens, clicks, registered, "
    "next_best_asset, next_best_channel, next_best_propensity, next_best_expected_value, next_best_image"
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

COST_CACHE_SECONDS = 3600  # Each Cost Explorer request costs about one cent.
USAGE_DAYS = 30
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


def _overview():
    totals = run_query(TOTALS_SQL)
    return {
        "totals": totals[0] if totals else {},
        "statuses": run_query(STATUS_SQL),
        "funnel": run_query(FUNNEL_SQL),
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
