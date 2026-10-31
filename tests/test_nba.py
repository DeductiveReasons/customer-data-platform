import json
import os
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import test_cdp  # noqa: E402,F401  Installs the boto3 stand-in when boto3 is unavailable.

try:
    from boto3.dynamodb.conditions import Attr  # noqa: F401
except ImportError:
    conditions = types.ModuleType("boto3.dynamodb.conditions")
    conditions.Attr = mock.Mock()
    sys.modules["boto3.dynamodb"] = types.ModuleType("boto3.dynamodb")
    sys.modules["boto3.dynamodb.conditions"] = conditions

from cdp import nba, nba_decide, nba_scan, receiver  # noqa: E402
from sample_data import generate, to_events  # noqa: E402
from test_cdp import ClientError  # noqa: E402

TRAITS = {
    "first_name": "Alex",
    "last_name": "Sample",
    "email": "alex@example.com",
    "address": "1 Sample St",
    "city": "Anytown",
    "state": "CA",
    "postal_code": "10000",
    "age": 40,
    "tenure_months": 60,
    "orders_12m": 8,
    "spend_12m": 900.0,
    "days_since_purchase": 10,
    "email_open_rate": 0.6,
    "loyalty_member": 0,
}
ALL_CONSENT = {"marketing": True, "email": True, "direct_mail": True}


class SampleDataTests(unittest.TestCase):
    def test_generates_unique_repeatable_fake_people(self):
        first = generate(200)
        self.assertEqual(first, generate(200))
        self.assertEqual(len({entry["person"]["user_id"] for entry, _ in first}), 200)
        self.assertTrue(all(entry["person"]["email"].endswith("@example.com") for entry, _ in first))

    def test_events_pass_validation_and_exclude_outcomes(self):
        from cdp import validation

        entry, _ = generate(1)[0]
        events = to_events(entry)
        for event in events:
            validation.normalize(event)
        self.assertNotIn("win_back", json.dumps(events))


class DecisionTests(unittest.TestCase):
    def test_consent_is_required(self):
        for consent in (
            {},
            {"email": True, "direct_mail": True},
            {"marketing": False, "email": True, "direct_mail": True},
            {"marketing": True},
        ):
            with self.subTest(consent=consent):
                self.assertIsNone(nba.decide("c", TRAITS, consent))

    def test_channel_follows_consent(self):
        email = nba.decide("c", TRAITS, {"marketing": True, "email": True})
        mail = nba.decide("c", TRAITS, {"marketing": True, "direct_mail": True})
        self.assertEqual(email["channel"], "email")
        self.assertEqual(mail["channel"], "direct_mail")
        self.assertEqual(email["recipient"], {"email": "alex@example.com"})
        self.assertEqual(mail["recipient"]["city"], "Anytown")

    def test_mail_needs_a_complete_address(self):
        traits = {k: v for k, v in TRAITS.items() if k != "postal_code"}
        self.assertEqual(nba.eligible_channels(traits, ALL_CONSENT), ["email"])

    def test_html_escapes_customer_data(self):
        traits = {**TRAITS, "first_name": "<script>alert(1)</script>"}
        content = nba.decide("c", traits, ALL_CONSENT)["content_html"]
        self.assertNotIn("<script>", content)
        self.assertIn("&lt;script&gt;", content)

    def test_missing_features_are_handled(self):
        traits = {k: TRAITS[k] for k in ("first_name", "email")}
        self.assertIsNotNone(nba.decide("c", traits, {"marketing": True, "email": True}))


class ScanTests(unittest.TestCase):
    def test_pages_through_all_profiles(self):
        table = mock.Mock()
        items = [{"pk": f"CUSTOMER#{i}", "sk": "PROFILE", "t_age": 30, "c_email": True} for i in range(23)]
        table.scan.side_effect = [{"Items": items[:15], "LastEvaluatedKey": {"pk": "x"}}, {"Items": items[15:]}]
        resource = mock.Mock()
        resource.Table.return_value = table
        sqs = mock.Mock()
        sqs.send_message_batch.return_value = {}
        boto = mock.Mock()
        boto.resource.return_value = resource
        boto.client.return_value = sqs
        with mock.patch.object(nba_scan, "boto3", boto), mock.patch.dict(
            os.environ, {"TABLE_NAME": "t", "DECISION_QUEUE_URL": "q"}
        ):
            result = nba_scan.handler({}, None)
        self.assertEqual(result, {"queued": 23})
        sent = [json.loads(e["MessageBody"]) for c in sqs.send_message_batch.call_args_list for e in c.kwargs["Entries"]]
        self.assertEqual(len(sent), 23)
        self.assertEqual(sent[0], {"customer_id": "0", "traits": {"age": 30}, "consent": {"email": True}})
        self.assertEqual(table.scan.call_args_list[1].kwargs["ExclusiveStartKey"], {"pk": "x"})


class DecideHandlerTests(unittest.TestCase):
    def setUp(self):
        self.table = mock.Mock()
        patches = [
            mock.patch.object(nba_decide, "_get_table", return_value=self.table),
            mock.patch.object(nba_decide, "_get_token", return_value="token"),
            mock.patch.dict(os.environ, {"RECEIVER_URL": "https://receiver.example.com/"}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        body = {"customer_id": "c1", "traits": TRAITS, "consent": ALL_CONSENT}
        self.event = {"Records": [{"messageId": "m1", "body": json.dumps(body)}]}

    def test_posts_html_to_the_chosen_endpoint(self):
        with mock.patch.object(nba_decide.urllib.request, "urlopen") as urlopen:
            result = nba_decide.handler(self.event, None)
        self.assertEqual(result, {"batchItemFailures": []})
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://receiver.example.com" + nba_decide.ENDPOINT_PATHS[payload["channel"]])
        self.assertEqual(request.get_header("Authorization"), "Bearer token")
        self.assertIn("<html>", payload["content_html"])
        self.assertIsNone(receiver.validate(payload["channel"], payload))

    def test_already_contacted_customer_is_skipped(self):
        self.table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
        )
        with mock.patch.object(nba_decide.urllib.request, "urlopen") as urlopen:
            result = nba_decide.handler(self.event, None)
        urlopen.assert_not_called()
        self.assertEqual(result, {"batchItemFailures": []})

    def test_failed_call_releases_claim_and_retries(self):
        with mock.patch.object(nba_decide.urllib.request, "urlopen", side_effect=OSError()):
            result = nba_decide.handler(self.event, None)
        self.table.delete_item.assert_called_once()
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "m1"}]})

    def test_contact_period_is_iso_week(self):
        self.assertEqual(nba_decide.contact_period(datetime(2026, 10, 31, tzinfo=timezone.utc)), "2026-W44")


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.table = mock.Mock()
        patches = [
            mock.patch.object(receiver, "_get_table", return_value=self.table),
            mock.patch.object(receiver, "_get_token", return_value="token"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.message = {
            "message_id": "m1",
            "customer_id": "c1",
            "channel": "email",
            "action": "replenish",
            "propensity": 0.25,
            "content_html": "<html><body>Hi</body></html>",
            "recipient": {"email": "a@example.com"},
        }

    def call(self, route="POST /email", body=None, token="token"):
        return receiver.handler(
            {
                "routeKey": route,
                "headers": {"authorization": f"Bearer {token}"},
                "body": json.dumps(self.message if body is None else body),
            },
            None,
        )

    def test_accepts_and_stores_without_sending(self):
        response = self.call()
        self.assertEqual(response["statusCode"], 202)
        self.assertFalse(json.loads(response["body"])["sent"])
        self.table.put_item.assert_called_once()

    def test_rejects_bad_token(self):
        self.assertEqual(self.call(token="wrong")["statusCode"], 401)
        self.table.put_item.assert_not_called()

    def test_rejects_invalid_requests(self):
        bad = [
            {**self.message, "content_html": "plain text"},
            {**self.message, "recipient": {}},
            {k: v for k, v in self.message.items() if k != "message_id"},
        ]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.call(body=body)["statusCode"], 400)
        self.assertEqual(self.call(route="POST /direct-mail")["statusCode"], 400)
        self.table.put_item.assert_not_called()


if __name__ == "__main__":
    unittest.main()
