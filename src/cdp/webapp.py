"""Password-protected web app for performance, assets, people and monitoring."""
import hmac
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import boto3

from cdp import assets

with open(os.path.join(os.path.dirname(__file__), "webapp.html"), encoding="utf-8") as page_file:
    PAGE = page_file.read()

CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
    "connect-src 'self'; frame-src 'self' about:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
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
PEOPLE_COLUMNS = (
    "customer_id, first_name, last_name, status, touches, opens, clicks, registered, "
    "next_best_asset, next_best_channel, next_best_propensity, next_best_expected_value"
)
HISTORY_SQL = """
SELECT DISTINCT h.decided_at, h.touch, h.asset_id, h.channel, h.selection, h.propensity, h.trigger,
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


def _response(status, body, content_type="application/json"):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": content_type,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": CSP,
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


def _person(customer_id):
    if not CUSTOMER_ID_RE.match(customer_id):
        raise BadRequest("invalid customer id")
    return {"history": run_query(HISTORY_SQL, [f"'{customer_id}'"])}


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


def _route(path, query):
    if path == "/api/overview":
        return _cached("overview", _overview)
    if path == "/api/performance":
        return _cached("performance", _performance)
    if path == "/api/assets":
        return _assets()
    if path == "/api/people":
        return _people(query)
    if path == "/api/monitoring":
        return _cached("monitoring", _monitoring)
    match = re.match(r"^/api/people/([^/]+)$", path)
    if match:
        return _person(match.group(1))
    return None


def handler(event, _context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/")
    if method != "GET":
        return _response(405, {"error": "method not allowed"})
    if path == "/":
        return _response(200, PAGE, "text/html; charset=utf-8")
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
    return _response(200, data)
