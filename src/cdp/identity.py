"""Step Functions task: resolves identifiers to a customer and updates the profile.

Table layout:
  pk=ID#<kind>:<value>   sk=LINK     -> customer_id
  pk=CUSTOMER#<id>       sk=PROFILE  -> t_<trait>, c_<consent purpose>
"""
import hashlib
import json
import os
import uuid
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

_table = None


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def identifiers(event):
    ids = []
    if event.get("user_id"):
        ids.append(f"user:{event['user_id']}")
    if event.get("email"):
        # Emails are hashed so they are not exposed in partition keys.
        digest = hashlib.sha256(event["email"].strip().lower().encode()).hexdigest()
        ids.append(f"email:{digest}")
    if event.get("anonymous_id"):
        ids.append(f"anon:{event['anonymous_id']}")
    return ids


def _link(table, identifier, customer_id):
    """Link an identifier to a customer and return whichever customer owns it."""
    key = {"pk": f"ID#{identifier}", "sk": "LINK"}
    try:
        table.put_item(Item={**key, "customer_id": customer_id}, ConditionExpression="attribute_not_exists(pk)")
        return customer_id
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    return table.get_item(Key=key, ConsistentRead=True)["Item"]["customer_id"]


def _resolve(table, event):
    ids = identifiers(event)
    # The list is ordered strongest first, so a known user_id wins over an anonymous_id.
    found = []
    for identifier in ids:
        item = table.get_item(Key={"pk": f"ID#{identifier}", "sk": "LINK"}, ConsistentRead=True).get("Item")
        if item:
            found.append(item["customer_id"])

    customer_id = found[0] if found else uuid.uuid4().hex
    if len(set(found)) > 1:
        # Merging customers is a business decision, so conflicts are surfaced and never auto-merged.
        print(f"identity conflict on event {event['event_id']}: keeping {customer_id}")
    for identifier in ids:
        owner = _link(table, identifier, customer_id)
        if not found:
            # Lost a creation race: adopt the winner for the remaining identifiers.
            customer_id = owner
    return customer_id


def _to_dynamo(value):
    return json.loads(json.dumps(value), parse_float=Decimal)


def _to_json(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _to_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_json(v) for v in value]
    return value


def _update_profile(table, customer_id, event):
    attrs = {"updated_at": event["received_at"]}
    attrs.update({f"t_{k}": _to_dynamo(v) for k, v in (event.get("traits") or {}).items()})
    if event["type"] == "consent":
        attrs.update({f"c_{k}": v for k, v in event["consent"].items()})

    names = {f"#a{i}": name for i, name in enumerate(attrs)}
    values = {f":a{i}": value for i, value in enumerate(attrs.values())}
    item = table.update_item(
        Key={"pk": f"CUSTOMER#{customer_id}", "sk": "PROFILE"},
        UpdateExpression="SET " + ", ".join(f"#a{i} = :a{i}" for i in range(len(attrs))),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )["Attributes"]
    return {
        "traits": {k[2:]: _to_json(v) for k, v in item.items() if k.startswith("t_")},
        "consent": {k[2:]: v for k, v in item.items() if k.startswith("c_")},
    }


def handler(event, _context):
    table = _get_table()
    customer_id = _resolve(table, event)
    profile = _update_profile(table, customer_id, event)
    return {"event": event, "customer_id": customer_id, "profile": profile}
