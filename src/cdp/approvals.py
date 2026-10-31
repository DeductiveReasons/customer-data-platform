"""Approval status for every deliverable variation: one asset, one channel and one image.

Variations are auto-approved for now. The table only stores those a person has changed,
so a variation with no record is approved, and a pending one is never sent.
"""
import os
import re
import time

import boto3

from cdp import assets, content

CHANNELS = ("email", "direct_mail")
APPROVED = "approved"
PENDING = "pending_approval"
AUTO_APPROVER = "auto-approval"
CACHE_SECONDS = 60
ID_RE = re.compile(r"^[a-z_]+~[a-z_]+~[a-z_]+-[0-9]{2}$")
_table = None
_pending = None
_loaded_at = 0.0


def _get_table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(os.environ["APPROVALS_TABLE"])
    return _table


def variation_id(asset_id, channel, image_id):
    return f"{asset_id}~{channel}~{image_id}"


def parse(value):
    """Return (asset_id, channel, image_id), or None if this is not a real variation."""
    if not ID_RE.match(value or ""):
        return None
    asset_id, channel, image_id = value.split("~")
    if asset_id in assets.ASSETS and channel in CHANNELS and content.image_theme(image_id):
        return asset_id, channel, image_id
    return None


def all_variations():
    return [
        (asset_id, channel, image)
        for asset_id in assets.ASSETS
        for channel in CHANNELS
        for image in content.all_images()
    ]


def stored_records():
    records = {}
    kwargs = {}
    table = _get_table()
    while True:
        page = table.scan(**kwargs)
        records.update({item["variation_id"]: item for item in page["Items"]})
        if "LastEvaluatedKey" not in page:
            return records
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def pending_ids():
    """Return pending variation IDs, cached briefly. A read failure stops sends instead of ignoring approvals."""
    global _pending, _loaded_at
    if _pending is None or time.time() - _loaded_at >= CACHE_SECONDS:
        _pending = {key for key, item in stored_records().items() if item.get("status") == PENDING}
        _loaded_at = time.time()
    return _pending


def set_status(value, status, changed_by="console"):
    _get_table().put_item(
        Item={"variation_id": value, "status": status, "changed_by": changed_by, "changed_at": int(time.time())}
    )
