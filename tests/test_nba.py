import gzip
import io
import json
import os
import random
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

from cdp import archive, assets, nba, nba_decide, nba_export, nba_feedback, nba_retrain, nba_scan, receiver, simulator, training  # noqa: E402
from journey import run  # noqa: E402
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
    "app_sessions_30d": 8,
    "days_since_active": 10,
    "email_open_rate": 0.6,
    "wellness_member": 0,
}
ALL_CONSENT = {"marketing": True, "email": True, "direct_mail": True}


class NoExplore:
    def random(self):
        return 0.99


def decide(state=None, consent=ALL_CONSENT, traits=TRAITS):
    return nba.decide("c1", traits, consent, state or {}, rng=NoExplore())


class SampleDataTests(unittest.TestCase):
    def test_generates_unique_repeatable_fake_people(self):
        first = generate(200)
        self.assertEqual(first, generate(200))
        self.assertEqual(len({entry["person"]["user_id"] for entry in first}), 200)
        self.assertTrue(all(entry["person"]["email"].endswith("@example.com") for entry in first))

    def test_events_pass_validation(self):
        from cdp import validation

        for event in to_events(generate(1)[0]):
            validation.normalize(event)


class AssetTests(unittest.TestCase):
    def test_every_asset_renders_for_each_channel_with_disclaimer(self):
        for asset_id in assets.ASSETS:
            for channel in ("email", "direct_mail"):
                content = assets.render(asset_id, channel, "Alex", "m1")
                self.assertIn("fictional company", content)
                self.assertIn("erichealth.example", content)

    def test_customer_data_is_escaped(self):
        content = assets.render("free_trial", "email", "<script>alert(1)</script>", "m&1")
        self.assertNotIn("<script>", content)
        self.assertIn("&lt;script&gt;", content)

    def test_email_has_unsubscribe_link(self):
        self.assertIn("/unsubscribe", assets.render("free_trial", "email", "Alex", "m1"))


class DecisionTests(unittest.TestCase):
    def test_consent_is_required(self):
        for consent in (
            {},
            {"email": True, "direct_mail": True},
            {"marketing": False, "email": True, "direct_mail": True},
            {"marketing": True},
        ):
            with self.subTest(consent=consent):
                self.assertIsNone(decide(consent=consent))

    def test_channel_follows_consent(self):
        email = decide(consent={"marketing": True, "email": True})
        mail = decide(consent={"marketing": True, "direct_mail": True})
        self.assertEqual(email["channel"], "email")
        self.assertEqual(mail["channel"], "direct_mail")
        self.assertEqual(email["recipient"], {"email": "alex@example.com"})
        self.assertEqual(mail["recipient"]["city"], "Anytown")

    def test_mail_needs_a_complete_address(self):
        traits = {k: v for k, v in TRAITS.items() if k != "postal_code"}
        self.assertEqual(nba.eligible_channels(traits, ALL_CONSENT), ["email"])

    def test_same_asset_is_not_sent_twice_in_a_row(self):
        first = decide()
        second = decide(state={"touches": 1, "last_asset": first["asset_id"]})
        self.assertNotEqual(first["asset_id"], second["asset_id"])

    def test_feedback_changes_the_features_used(self):
        message = decide(state={"touches": 2, "opens": 2, "clicks": 1, "last_asset": "program_intro"})
        self.assertEqual(message["features"]["clicks"], 1.0)
        self.assertEqual(message["message_id"], "c1-3")

    def test_contact_stops_at_the_touch_limit(self):
        self.assertIsNone(decide(state={"touches": nba.MAX_TOUCHES}))

    def test_registered_customer_gets_onboarding_once(self):
        message = decide(state={"touches": 2, "registered": True})
        self.assertEqual(message["asset_id"], assets.ONBOARDING)
        self.assertEqual(message["touch"], "onboarding")
        self.assertIsNone(decide(state={"registered": True, "onboarded": True}))

    def test_missing_features_are_handled(self):
        traits = {k: TRAITS[k] for k in ("first_name", "email")}
        self.assertIsNotNone(decide(traits=traits, consent={"marketing": True, "email": True}))

    def test_exploration_can_try_other_assets(self):
        picked = {
            nba.decide("c", TRAITS, ALL_CONSENT, {}, rng=random.Random(seed))["asset_id"]
            for seed in range(300)
        }
        self.assertEqual(picked, set(assets.SIGNUP_ASSETS))


class SimulatorTests(unittest.TestCase):
    def test_outcomes_are_repeatable_and_ordered(self):
        features = decide()["features"]
        self.assertEqual(
            simulator.outcome_for("m", "free_trial", features), simulator.outcome_for("m", "free_trial", features)
        )
        for index in range(500):
            outcome = simulator.outcome_for(f"m{index}", "free_trial", features)
            if outcome["registered"]:
                self.assertTrue(outcome["clicked"])

    def test_mail_opens_are_never_reported(self):
        features = decide(consent={"marketing": True, "direct_mail": True})["features"]
        for index in range(200):
            self.assertFalse(simulator.outcome_for(f"m{index}", "free_trial", features)["opened"])


class JourneyTests(unittest.TestCase):
    def test_journeys_end_and_respect_limits(self):
        records, totals = run(generate(300, seed=5), nba.BUNDLED_MODEL, random.Random(1))
        self.assertGreater(len(records), 0)
        self.assertEqual(totals["registered"], totals["onboarded"] + (totals["registered"] - totals["onboarded"]))
        self.assertLessEqual(totals["onboarded"], totals["registered"])
        self.assertLessEqual(totals["messages"], 300 * (nba.MAX_TOUCHES + 1))


class TrainingTests(unittest.TestCase):
    def records(self, count=2000):
        rng = random.Random(1)
        rows = []
        for _ in range(count):
            signal = rng.random()
            rows.append(
                {
                    "asset_id": "free_trial",
                    "features": {"signal": signal, "noise": rng.random()},
                    "registered": rng.random() < 0.05 + 0.5 * signal,
                }
            )
        return rows

    def test_learns_a_real_signal(self):
        model, report = training.train(self.records(), ["signal", "noise"], "v")
        self.assertEqual(report["free_trial"]["status"], "trained")
        weights = model["actions"]["free_trial"]["weights"]
        self.assertGreater(weights[0], abs(weights[1]))

    def test_small_samples_are_not_trained(self):
        model, report = training.train(self.records(50), ["signal", "noise"], "v")
        self.assertIsNone(model)
        self.assertEqual(report["free_trial"]["status"], "not enough data")

    def test_noise_is_not_published(self):
        rng = random.Random(2)
        rows = [
            {"asset_id": "a", "features": {"x": rng.random()}, "registered": rng.random() < 0.3}
            for _ in range(3000)
        ]
        model, report = training.train(rows, ["x"], "v")
        self.assertIsNone(model)
        self.assertEqual(report["a"]["status"], "weak signal")

    def test_untrained_asset_keeps_previous_model_scoring(self):
        previous = {
            "version": "old",
            "features": ["signal", "noise"],
            "means": [5.0, 5.0],
            "stds": [2.0, 2.0],
            "actions": {"coach_consult": {"intercept": -1.0, "weights": [1.0, 0.0]}},
        }
        model, _ = training.train(self.records(), ["signal", "noise"], "new", previous=previous)
        kept = model["actions"]["coach_consult"]
        self.assertEqual(kept["means"], [5.0, 5.0])
        expected = 1 / (1 + 2.718281828459045 ** -(-1.0 + (7.0 - 5.0) / 2.0))
        self.assertAlmostEqual(nba.propensity(model, "coach_consult", {"signal": 7.0, "noise": 1.0}), expected)

    def test_auc(self):
        self.assertEqual(training.auc([0, 0, 1, 1], [1, 2, 3, 4]), 1.0)
        self.assertEqual(training.auc([0, 1], [1, 1]), 0.5)


class ScanTests(unittest.TestCase):
    def test_pages_through_all_profiles(self):
        table = mock.Mock()
        items = [
            {"pk": f"CUSTOMER#{i}", "sk": "PROFILE", "t_age": 30, "c_email": True, "s_touches": 1}
            for i in range(23)
        ]
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
        self.assertEqual(
            sent[0],
            {
                "customer_id": "0",
                "traits": {"age": 30},
                "consent": {"email": True},
                "state": {"touches": 1},
                "trigger": "scan",
            },
        )
        self.assertEqual(table.scan.call_args_list[1].kwargs["ExclusiveStartKey"], {"pk": "x"})


class DecideHandlerTests(unittest.TestCase):
    def setUp(self):
        self.table = mock.Mock()
        self.sqs = mock.Mock()
        patches = [
            mock.patch.object(nba_decide, "_get_table", return_value=self.table),
            mock.patch.object(nba_decide, "_get_token", return_value="token"),
            mock.patch.object(nba_decide, "_get_model", return_value=nba.BUNDLED_MODEL),
            mock.patch.object(nba_decide, "_get_sqs", return_value=self.sqs),
            mock.patch.object(nba.random, "random", return_value=0.99),
            mock.patch.dict(os.environ, {"RECEIVER_URL": "https://receiver.example.com/", "ARCHIVE_QUEUE_URL": "a"}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def event(self, **overrides):
        body = {"customer_id": "c1", "traits": TRAITS, "consent": ALL_CONSENT, "state": {}, "trigger": "feedback"}
        return {"Records": [{"messageId": "m1", "body": json.dumps({**body, **overrides})}]}

    def test_posts_html_to_the_chosen_endpoint_and_records_the_touch(self):
        with mock.patch.object(nba_decide.urllib.request, "urlopen") as urlopen:
            result = nba_decide.handler(self.event(), None)
        self.assertEqual(result, {"batchItemFailures": []})
        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://receiver.example.com" + nba_decide.ENDPOINT_PATHS[payload["channel"]])
        self.assertEqual(request.get_header("Authorization"), "Bearer token")
        self.assertIn("<html>", payload["content_html"])
        self.assertIsNone(receiver.validate(payload["channel"], payload))
        self.assertIn("ADD s_touches", self.table.update_item.call_args.kwargs["UpdateExpression"])

    def test_history_row_has_no_contact_details_and_failure_does_not_retry(self):
        self.sqs.send_message.side_effect = OSError()
        with mock.patch.object(nba_decide.urllib.request, "urlopen"):
            result = nba_decide.handler(self.event(), None)
        self.assertEqual(result, {"batchItemFailures": []})
        body = json.loads(self.sqs.send_message.call_args.kwargs["MessageBody"])
        self.assertEqual(body["dataset"], "history")
        row = body["row"]
        self.assertEqual(row["trigger"], "feedback")
        self.assertNotIn("recipient", row)
        self.assertNotIn("content_html", row)

    def test_failed_send_writes_no_history(self):
        with mock.patch.object(nba_decide.urllib.request, "urlopen", side_effect=OSError()):
            nba_decide.handler(self.event(), None)
        self.sqs.send_message.assert_not_called()

    def test_duplicate_touch_is_skipped(self):
        self.table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
        )
        with mock.patch.object(nba_decide.urllib.request, "urlopen") as urlopen:
            result = nba_decide.handler(self.event(), None)
        urlopen.assert_not_called()
        self.assertEqual(result, {"batchItemFailures": []})

    def test_scan_respects_contact_gap_but_feedback_does_not(self):
        recent = {"state": {"last_contact": 4_000_000_000}}
        with mock.patch.object(nba_decide.urllib.request, "urlopen") as urlopen, mock.patch.object(
            nba_decide.time, "time", return_value=4_000_000_100
        ):
            nba_decide.handler(self.event(trigger="scan", **recent), None)
            urlopen.assert_not_called()
            nba_decide.handler(self.event(trigger="feedback", **recent), None)
            urlopen.assert_called_once()

    def test_failed_call_releases_claim_and_retries(self):
        with mock.patch.object(nba_decide.urllib.request, "urlopen", side_effect=OSError()):
            result = nba_decide.handler(self.event(), None)
        self.table.delete_item.assert_called_once()
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "m1"}]})


class FeedbackHandlerTests(unittest.TestCase):
    FEEDBACK = {
        "message_id": "c1-1",
        "customer_id": "c1",
        "touch": "1",
        "channel": "email",
        "asset_id": "free_trial",
        "features": {"touches": 0.0},
        "model_version": "v",
        "selection": "best",
        "outcome": {"opened": True, "clicked": True, "registered": True, "unsubscribed": False},
    }

    def setUp(self):
        self.resource = mock.Mock()
        self.table = self.resource.Table.return_value
        self.table.get_item.return_value = {
            "Item": {"pk": "CUSTOMER#c1", "sk": "PROFILE", "s_registered": True, "s_clicks": 1, "c_email": True}
        }
        self.sqs = mock.Mock()
        patches = [
            mock.patch.object(nba_feedback, "_clients", return_value=(self.resource, self.sqs)),
            mock.patch.dict(
                os.environ,
                {
                    "TABLE_NAME": "t",
                    "ARCHIVE_QUEUE_URL": "a",
                    "DECISION_QUEUE_URL": "q",
                    "METRIC_NAMESPACE": "cdp/NBA",
                },
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_handler(self, **overrides):
        body = {**self.FEEDBACK, **overrides}
        return nba_feedback.handler({"Records": [{"messageId": "m1", "body": json.dumps(body)}]}, None)

    def test_updates_profile_saves_training_data_and_requests_next_decision(self):
        self.assertEqual(self.run_handler(), {"batchItemFailures": []})
        items = self.resource.meta.client.transact_write_items.call_args.kwargs["TransactItems"]
        self.assertIn("s_registered", items[1]["Update"]["UpdateExpression"])
        archived, decision = self.sqs.send_message.call_args_list
        body = json.loads(archived.kwargs["MessageBody"])
        self.assertEqual(body["dataset"], "feedback_email")
        record = body["row"]
        self.assertTrue(record["registered"])
        self.assertTrue(record["feedback_at"])
        self.assertEqual(record["asset_id"], "free_trial")
        next_decision = json.loads(decision.kwargs["MessageBody"])
        self.assertEqual(next_decision["trigger"], "feedback")
        self.assertTrue(next_decision["state"]["registered"])

    def test_unsubscribe_removes_channel_consent(self):
        self.run_handler(outcome={"opened": False, "clicked": False, "registered": False, "unsubscribed": True})
        update = self.resource.meta.client.transact_write_items.call_args.kwargs["TransactItems"][1]["Update"]
        self.assertEqual(update["ExpressionAttributeNames"]["#consent"], "c_email")
        self.assertIs(update["ExpressionAttributeValues"][":no"], False)

    def test_retry_does_not_recount_but_finishes_remaining_steps(self):
        self.resource.meta.client.transact_write_items.side_effect = ClientError(
            {
                "Error": {"Code": "TransactionCanceledException"},
                "CancellationReasons": [{"Code": "ConditionalCheckFailed"}, {"Code": "None"}],
            },
            "TransactWriteItems",
        )
        self.assertEqual(self.run_handler(), {"batchItemFailures": []})
        self.assertEqual(self.sqs.send_message.call_count, 2)

    def test_other_transaction_failures_retry(self):
        self.resource.meta.client.transact_write_items.side_effect = ClientError(
            {
                "Error": {"Code": "TransactionCanceledException"},
                "CancellationReasons": [{"Code": "ThrottlingError"}, {"Code": "None"}],
            },
            "TransactWriteItems",
        )
        self.assertEqual(self.run_handler(), {"batchItemFailures": [{"itemIdentifier": "m1"}]})
        self.sqs.send_message.assert_not_called()


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.s3 = mock.Mock()
        patches = [
            mock.patch.object(archive, "_client", return_value=self.s3),
            mock.patch.dict(
                os.environ,
                {"BUCKET": "b", "DATASET_PREFIXES": json.dumps({"history": "history/", "raw": "events/"})},
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def record(message_id, dataset, row):
        return {"messageId": message_id, "body": json.dumps({"dataset": dataset, "row": row})}

    def test_groups_rows_into_one_file_per_dataset(self):
        records = [self.record("1", "history", {"a": 1}), self.record("2", "history", {"a": 2}), self.record("3", "raw", {"b": 1})]
        self.assertEqual(archive.handler({"Records": records}, None), {"batchItemFailures": []})
        calls = {c.kwargs["Key"].split("/")[0]: c.kwargs["Body"] for c in self.s3.put_object.call_args_list}
        lines = gzip.decompress(calls["history"]).decode().splitlines()
        self.assertEqual([json.loads(line)["a"] for line in lines], [1, 2])
        self.assertIn("events", calls)

    def test_bad_messages_and_failed_writes_are_retried(self):
        self.s3.put_object.side_effect = OSError()
        records = [self.record("1", "unknown", {}), self.record("2", "history", {"a": 1})]
        result = archive.handler({"Records": records}, None)
        self.assertEqual({f["itemIdentifier"] for f in result["batchItemFailures"]}, {"1", "2"})


class NextActionTimeTests(unittest.TestCase):
    NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)  # A Wednesday, 5am in California.

    def test_uses_the_best_local_hour(self):
        result = nba.next_action_time({"state": "CA", "email_open_rate": 0.6}, {}, "email", self.NOW)
        self.assertEqual(result, "2026-10-28T16:00:00+00:00")

    def test_waits_for_the_contact_gap(self):
        last = self.NOW.timestamp()
        result = nba.next_action_time({"state": "NY", "email_open_rate": 0.1}, {"last_contact": last}, "email", self.NOW)
        self.assertEqual(result, "2026-11-04T17:00:00+00:00")

    def test_mail_skips_weekends(self):
        saturday = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
        result = nba.next_action_time({"state": "NY"}, {}, "direct_mail", saturday)
        self.assertEqual(result, "2026-11-02T15:00:00+00:00")


class ExportTests(unittest.TestCase):
    def item(self, **attrs):
        base = {"pk": "CUSTOMER#c1", "sk": "PROFILE"}
        base.update({f"t_{k}": v for k, v in TRAITS.items()})
        base.update({f"c_{k}": v for k, v in ALL_CONSENT.items()})
        base.update(attrs)
        return base

    def row(self, **attrs):
        return nba_export.person_row(self.item(**attrs), "now", nba.BUNDLED_MODEL)

    def test_person_row_shows_next_best_action(self):
        row = self.row()
        self.assertEqual(row["status"], "active")
        self.assertIn(row["next_best_action"], ("email", "direct_mail"))
        self.assertTrue(row["next_best_action_time"])
        self.assertEqual(row["address"], "1 Sample St")
        self.assertIsNone(row["no_contact_reason"])
        self.assertIn(row["next_best_asset"], assets.SIGNUP_ASSETS)
        self.assertIsNotNone(row["next_best_propensity"])

    def test_statuses(self):
        self.assertEqual(self.row(c_marketing=False)["status"], "no_marketing_consent")
        blocked = self.row(c_marketing=False)
        self.assertEqual(blocked["next_best_action"], "no_contact")
        self.assertEqual(blocked["no_contact_reason"], "no_marketing_consent")
        self.assertIsNone(blocked["next_best_action_time"])
        self.assertEqual(self.row(s_touches=nba.MAX_TOUCHES)["status"], "journey_complete")
        self.assertEqual(self.row(s_registered=True)["status"], "onboarding_due")
        self.assertEqual(self.row(s_registered=True, s_onboarded=True)["status"], "registered")


class RetrainTests(unittest.TestCase):
    def test_loads_gzipped_records_and_removes_duplicates(self):
        rows = [
            {"message_id": "a", "asset_id": "free_trial", "features": {}, "registered": False},
            {"message_id": "a", "asset_id": "free_trial", "features": {}, "registered": False},
            {"message_id": "b", "asset_id": assets.ONBOARDING, "features": {}, "registered": False},
        ]
        data = gzip.compress(("\n".join(json.dumps(r) for r in rows) + "\n").encode())
        s3 = mock.Mock()
        s3.get_paginator.return_value.paginate.return_value = [{"Contents": [{"Key": "k"}]}]
        s3.get_object.return_value = {"Body": io.BytesIO(data)}
        records = nba_retrain.load_records(s3, "b", "feedback/")
        self.assertEqual([r["message_id"] for r in records], ["a"])


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.table = mock.Mock()
        self.sqs = mock.Mock()
        patches = [
            mock.patch.object(receiver, "_get_table", return_value=self.table),
            mock.patch.object(receiver, "_get_token", return_value="token"),
            mock.patch.object(receiver, "_get_sqs", return_value=self.sqs),
            mock.patch.dict(os.environ, {"FEEDBACK_QUEUE_URL": "q"}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.message = decide(consent={"marketing": True, "email": True})

    def call(self, route="POST /email", body=None, token="token"):
        return receiver.handler(
            {
                "routeKey": route,
                "headers": {"authorization": f"Bearer {token}"},
                "body": json.dumps(self.message if body is None else body),
            },
            None,
        )

    def test_stores_without_sending_and_emits_feedback(self):
        response = self.call()
        self.assertEqual(response["statusCode"], 202)
        self.assertFalse(json.loads(response["body"])["sent"])
        self.table.put_item.assert_called_once()
        feedback = json.loads(self.sqs.send_message.call_args.kwargs["MessageBody"])
        self.assertEqual(set(feedback["outcome"]), {"opened", "clicked", "registered", "unsubscribed"})
        self.assertEqual(feedback["message_id"], self.message["message_id"])

    def test_onboarding_has_no_feedback(self):
        message = decide(state={"registered": True}, consent={"marketing": True, "email": True})
        self.assertEqual(self.call(body=message)["statusCode"], 202)
        self.sqs.send_message.assert_not_called()

    def test_rejects_bad_token(self):
        self.assertEqual(self.call(token="wrong")["statusCode"], 401)
        self.table.put_item.assert_not_called()

    def test_rejects_invalid_requests(self):
        bad = [
            {**self.message, "content_html": "plain text"},
            {**self.message, "recipient": {}},
            {**self.message, "asset_id": "unknown"},
            {k: v for k, v in self.message.items() if k != "message_id"},
        ]
        for body in bad:
            with self.subTest(body=body):
                self.assertEqual(self.call(body=body)["statusCode"], 400)
        self.assertEqual(self.call(route="POST /direct-mail")["statusCode"], 400)
        self.table.put_item.assert_not_called()
        self.sqs.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
