"""Validation and normalization of incoming customer events."""
import json
import re
import uuid
from datetime import datetime, timezone

ALLOWED_TYPES = {"track", "identify", "page", "screen", "consent"}
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_EVENT_BYTES = 200_000  # EventBridge entries are limited to 256 KB including the envelope.
MAX_BATCH = 100


class ValidationError(ValueError):
    pass


def _optional_string(event, name):
    value = event.get(name)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValidationError(f"{name} must be a non-empty string")


def normalize(event, now=None):
    if not isinstance(event, dict):
        raise ValidationError("event must be an object")

    event_type = event.get("type")
    if event_type not in ALLOWED_TYPES:
        raise ValidationError(f"type must be one of {sorted(ALLOWED_TYPES)}")

    _optional_string(event, "user_id")
    _optional_string(event, "anonymous_id")
    _optional_string(event, "email")
    if not (event.get("user_id") or event.get("anonymous_id") or event.get("email")):
        raise ValidationError("one of user_id, anonymous_id or email is required")

    if event_type == "track" and not (isinstance(event.get("event"), str) and event["event"].strip()):
        raise ValidationError("track events require an event name")
    if event_type == "consent":
        consent = event.get("consent")
        if not isinstance(consent, dict) or not consent or not all(isinstance(v, bool) for v in consent.values()):
            raise ValidationError("consent events require a consent object of booleans")

    for name in ("traits", "properties"):
        if name in event and not isinstance(event[name], dict):
            raise ValidationError(f"{name} must be an object")

    event_id = event.get("event_id") or uuid.uuid4().hex
    if not isinstance(event_id, str) or not EVENT_ID_RE.match(event_id):
        raise ValidationError("event_id must match [A-Za-z0-9_-]{1,64}")

    received_at = (now or datetime.now(timezone.utc)).isoformat()
    normalized = {**event, "event_id": event_id, "received_at": received_at}
    if len(json.dumps(normalized).encode()) > MAX_EVENT_BYTES:
        raise ValidationError("event is too large")
    return normalized


def parse_body(body):
    """Return a list of normalized events from a single event or {"batch": [...]} body."""
    try:
        payload = json.loads(body or "")
    except (TypeError, ValueError) as exc:
        raise ValidationError("body must be valid JSON") from exc

    if isinstance(payload, dict) and "batch" in payload:
        events = payload["batch"]
        if not isinstance(events, list) or not 1 <= len(events) <= MAX_BATCH:
            raise ValidationError(f"batch must contain 1 to {MAX_BATCH} events")
    else:
        events = [payload]
    return [normalize(event) for event in events]
