import json
import os
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    import boto3  # noqa: F401
    from botocore.exceptions import ClientError
except ImportError:
    class ClientError(Exception):
        def __init__(self, response, operation):
            super().__init__(operation)
            self.response = response

    sys.modules["boto3"] = types.ModuleType("boto3")
    botocore = types.ModuleType("botocore")
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.ClientError = ClientError
    sys.modules["botocore"] = botocore
    sys.modules["botocore.exceptions"] = exceptions

from cdp import deliver, identity, ingest, rules, starter, validation  # noqa: E402

TRACK = {"type": "track", "event": "Signed Up", "user_id": "u1"}


class FakeTable:
    def __init__(self):
        self.items = {}

    def get_item(self, Key, ConsistentRead=False):
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": item} if item else {}

    def put_item(self, Item, ConditionExpression):
        key = (Item["pk"], Item["sk"])
        if key in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = Item

    def update_item(self, Key, UpdateExpression, ExpressionAttributeNames, ExpressionAttributeValues, ReturnValues):
        item = self.items.setdefault((Key["pk"], Key["sk"]), dict(Key))
        for placeholder, name in ExpressionAttributeNames.items():
            item[name] = ExpressionAttributeValues[":" + placeholder[1:]]
        return {"Attributes": item}


class ValidationTests(unittest.TestCase):
    def test_adds_id_and_timestamp(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = validation.normalize(TRACK, now)
        self.assertTrue(result["event_id"])
        self.assertEqual(result["received_at"], now.isoformat())

    def test_rejects_invalid_events(self):
        bad = [
            {"event": "x", "user_id": "u"},
            {"type": "other", "user_id": "u"},
            {"type": "track", "user_id": "u"},
            {"type": "track", "event": "x"},
            {"type": "consent", "user_id": "u", "consent": {"marketing": "yes"}},
            {**TRACK, "event_id": "../bad id"},
            {**TRACK, "traits": "nope"},
            "text",
        ]
        for event in bad:
            with self.subTest(event=event), self.assertRaises(validation.ValidationError):
                validation.normalize(event)

    def test_batch_limits(self):
        self.assertEqual(len(validation.parse_body(json.dumps({"batch": [TRACK, TRACK]}))), 2)
        for body in ("not json", None, json.dumps({"batch": []}), json.dumps({"batch": [TRACK] * 101})):
            with self.subTest(body=body), self.assertRaises(validation.ValidationError):
                validation.parse_body(body)


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.client = mock.Mock()
        self.client.put_events.return_value = {"FailedEntryCount": 0}
        patches = [
            mock.patch.object(ingest, "_client", return_value=self.client),
            mock.patch.dict(os.environ, {"EVENT_BUS_NAME": "bus"}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_chunks_batches_to_put_events_limit(self):
        response = ingest.handler({"body": json.dumps({"batch": [TRACK] * 25})}, None)
        self.assertEqual(response["statusCode"], 202)
        self.assertEqual([len(c.kwargs["Entries"]) for c in self.client.put_events.call_args_list], [10, 10, 5])

    def test_bad_event_returns_400_and_publishes_nothing(self):
        self.assertEqual(ingest.handler({"body": "{}"}, None)["statusCode"], 400)
        self.client.put_events.assert_not_called()

    def test_failed_entries_return_503(self):
        self.client.put_events.return_value = {"FailedEntryCount": 1}
        self.assertEqual(ingest.handler({"body": json.dumps(TRACK)}, None)["statusCode"], 503)


class StarterTests(unittest.TestCase):
    def test_duplicates_succeed_and_failures_are_reported(self):
        class AlreadyExists(Exception):
            pass

        client = mock.Mock()
        client.exceptions.ExecutionAlreadyExists = AlreadyExists
        client.start_execution.side_effect = [AlreadyExists(), RuntimeError(), None]
        records = [{"messageId": str(i), "body": json.dumps({"event_id": f"e{i}"})} for i in range(3)]
        with mock.patch.object(starter, "_client", return_value=client), mock.patch.dict(
            os.environ, {"STATE_MACHINE_ARN": "arn"}
        ):
            result = starter.handler({"Records": records}, None)
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "1"}]})


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.table = FakeTable()
        patch = mock.patch.object(identity, "_get_table", return_value=self.table)
        patch.start()
        self.addCleanup(patch.stop)

    def run_event(self, **fields):
        event = {"type": "identify", "event_id": "e", "received_at": "t", **fields}
        return identity.handler(event, None)

    def test_links_anonymous_activity_to_a_known_user(self):
        first = self.run_event(anonymous_id="a1")
        second = self.run_event(anonymous_id="a1", user_id="u1")
        third = self.run_event(user_id="u1")
        self.assertEqual({first["customer_id"], second["customer_id"], third["customer_id"]}, {first["customer_id"]})

    def test_email_is_normalized_and_not_stored_in_keys(self):
        first = self.run_event(email="A@Example.com")
        second = self.run_event(email="a@example.com ")
        self.assertEqual(first["customer_id"], second["customer_id"])
        self.assertFalse(any("example" in pk for pk, _ in self.table.items))

    def test_profile_merges_traits_with_floats_and_consent(self):
        self.run_event(user_id="u1", traits={"plan": "pro", "score": 1.5})
        result = self.run_event(user_id="u1", type="consent", consent={"marketing": True})
        self.assertEqual(result["profile"], {"traits": {"plan": "pro", "score": 1.5}, "consent": {"marketing": True}})
        json.dumps(result)

    def test_conflicting_customers_are_not_merged(self):
        a = self.run_event(user_id="u1")["customer_id"]
        self.run_event(user_id="u2")
        self.assertEqual(self.run_event(user_id="u1", anonymous_id="x")["customer_id"], a)
        b_link = self.table.items[("ID#user:u2", "LINK")]["customer_id"]
        self.assertEqual(self.run_event(user_id="u2", anonymous_id="x")["customer_id"], b_link)


class RulesTests(unittest.TestCase):
    QUEUES = {"braze": "qb", "crm": "qc", "other_apis": "qo"}

    def destinations(self, consent, enabled=("braze", "crm", "other_apis")):
        resolved = {"event": {}, "customer_id": "c", "profile": {"traits": {}, "consent": consent}}
        return [d["destination"] for d in rules.evaluate(resolved, list(enabled), self.QUEUES)]

    def test_missing_or_denied_consent_blocks_delivery(self):
        self.assertEqual(self.destinations({}), [])
        self.assertEqual(self.destinations({"marketing": False}), [])

    def test_consent_routes_only_to_enabled_destinations(self):
        self.assertEqual(self.destinations({"marketing": True, "crm": True}), ["braze", "crm"])
        self.assertEqual(self.destinations({"marketing": True}, enabled=("crm",)), [])


class DeliverTests(unittest.TestCase):
    MESSAGE = {
        "destination": "braze",
        "customer_id": "c1",
        "event": {"type": "track", "event": "Signed Up", "received_at": "t", "properties": {"a": 1}},
        "profile": {"traits": {"plan": "pro"}, "consent": {"marketing": True}},
    }

    def test_braze_payload(self):
        payload = deliver.braze_payload(self.MESSAGE)
        self.assertEqual(payload["attributes"], [{"external_id": "c1", "plan": "pro"}])
        self.assertEqual(payload["events"][0]["name"], "Signed Up")

    def test_identify_has_no_braze_event(self):
        message = {**self.MESSAGE, "event": {"type": "identify", "received_at": "t"}}
        self.assertNotIn("events", deliver.braze_payload(message))

    def test_rejects_plain_http(self):
        with self.assertRaises(ValueError):
            deliver._post("http://example.com", "t", {})

    def test_failed_delivery_is_reported_for_retry(self):
        records = [{"messageId": "m1", "body": json.dumps(self.MESSAGE)}]
        secret = {"rest_endpoint": "https://example.com", "api_key": "k"}
        with mock.patch.object(deliver, "_get_secret", return_value=secret), mock.patch.object(
            deliver, "_post", side_effect=OSError()
        ):
            result = deliver.handler({"Records": records}, None)
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "m1"}]})


if __name__ == "__main__":
    unittest.main()
