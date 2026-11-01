import io
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.dirname(__file__))

import test_cdp  # noqa: E402,F401  Installs the boto3 stand-in when boto3 is unavailable.

from cdp import webapp  # noqa: E402


def request(path, token="secret", query=None):
    event = {"rawPath": path, "requestContext": {"http": {"method": "GET"}}, "queryStringParameters": query}
    if token is not None:
        event["headers"] = {"authorization": f"Bearer {token}"}
    return event


class WebappTests(unittest.TestCase):
    def setUp(self):
        webapp._cache.clear()
        patches = [
            mock.patch.object(webapp, "_get_token", return_value="secret"),
            mock.patch.object(webapp, "run_query", return_value=[]),
        ]
        self.token, self.query = (patch.start() for patch in patches)
        for patch in patches:
            self.addCleanup(patch.stop)

    def test_page_is_public_with_security_headers_but_has_no_data(self):
        response = webapp.handler(request("/", token=None), None)
        self.assertEqual(response["statusCode"], 200)
        self.assertIn("frame-ancestors 'none'", response["headers"]["Content-Security-Policy"])
        self.query.assert_not_called()

    def test_data_requires_the_password(self):
        for token in (None, "wrong"):
            for path in ("/api/overview", "/api/assets", "/api/people", "/api/monitoring", "/api/people/abc"):
                with self.subTest(path=path, token=token):
                    self.assertEqual(webapp.handler(request(path, token=token), None)["statusCode"], 401)
        self.query.assert_not_called()

    def test_images_are_public_and_fall_back_to_illustrations(self):
        boto = mock.Mock()
        boto.client.return_value.get_object.side_effect = OSError()
        with mock.patch.object(webapp, "boto3", boto), mock.patch.dict(os.environ, {"IMAGE_BUCKET": "b"}):
            response = webapp.handler(request("/images/yoga-03.jpg", token=None), None)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(response["headers"]["Content-Type"], "image/svg+xml")
        self.assertIn("sandbox", response["headers"]["Content-Security-Policy"])

    def test_stored_image_is_returned_as_binary(self):
        boto = mock.Mock()
        boto.client.return_value.get_object.return_value = {"Body": io.BytesIO(b"jpeg")}
        with mock.patch.object(webapp, "boto3", boto), mock.patch.dict(os.environ, {"IMAGE_BUCKET": "b"}):
            response = webapp.handler(request("/images/yoga-03.jpg", token=None), None)
        self.assertEqual(response["headers"]["Content-Type"], "image/jpeg")
        self.assertTrue(response["isBase64Encoded"])

    def test_unknown_or_unsafe_images_are_rejected(self):
        for path in ("/images/nope-01.jpg", "/images/yoga-99.jpg", "/images/../secret.jpg"):
            with self.subTest(path=path):
                self.assertIn(webapp.handler(request(path, token=None), None)["statusCode"], (401, 404))

    def test_only_reads_are_allowed(self):
        event = request("/api/people")
        event["requestContext"]["http"]["method"] = "POST"
        self.assertEqual(webapp.handler(event, None)["statusCode"], 405)

    def test_unsafe_filters_never_reach_the_query(self):
        attacks = [
            ("/api/people", {"status": "active' OR '1'='1"}),
            ("/api/people", {"customer_id": "x' OR '1'='1"}),
            ("/api/people/x'%20OR%201=1", None),
        ]
        for path, query in attacks:
            with self.subTest(path=path, query=query):
                self.assertEqual(webapp.handler(request(path, query=query), None)["statusCode"], 400)
        self.query.assert_not_called()

    def test_filters_are_bound_as_parameters(self):
        webapp.handler(request("/api/people", query={"status": "active", "customer_id": "c1"}), None)
        sql, parameters = self.query.call_args.args
        self.assertNotIn("c1", sql)
        self.assertEqual(parameters, ["'active'", "'c1'"])

    def test_assets_include_previews(self):
        response = webapp.handler(request("/api/assets"), None)
        body = json.loads(response["body"])
        self.assertEqual(len(body["assets"]), 5)
        self.assertIn("<svg", body["assets"][0]["email_html"])
        self.assertIn("<svg", body["assets"][0]["direct_mail_html"])

    def test_errors_do_not_expose_details(self):
        self.query.side_effect = RuntimeError("secret detail")
        response = webapp.handler(request("/api/overview"), None)
        self.assertEqual(response["statusCode"], 500)
        self.assertNotIn("secret detail", response["body"])

    def test_results_are_cached(self):
        webapp.handler(request("/api/overview"), None)
        calls = self.query.call_count
        webapp.handler(request("/api/overview"), None)
        self.assertEqual(self.query.call_count, calls)


class VariationTests(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(webapp, "_get_token", return_value="secret"),
            mock.patch.object(webapp.approvals, "stored_records", return_value={}),
            mock.patch.object(webapp.approvals, "set_status"),
        ]
        self.token, self.records, self.set_status = (patch.start() for patch in patches)
        for patch in patches:
            self.addCleanup(patch.stop)

    def post(self, variation, body, token="secret"):
        event = request(f"/api/variations/{variation}/status", token=token)
        event["requestContext"]["http"]["method"] = "POST"
        event["body"] = json.dumps(body)
        return webapp.handler(event, None)

    def test_all_variations_default_to_auto_approved(self):
        body = json.loads(webapp.handler(request("/api/variations"), None)["body"])
        self.assertEqual(body["total"], 1000)
        self.assertEqual(body["totals"], {"approved": 1000, "pending_approval": 0})
        self.assertEqual(body["variations"][0]["changed_by"], "auto-approval")
        self.assertEqual(len(body["variations"]), webapp.VARIATION_PAGE_SIZE)
        self.assertIn("/images/", body["variations"][0]["html"])

    def test_filters_and_pending_status(self):
        key = "free_trial~email~yoga-03"
        self.records.return_value = {key: {"status": "pending_approval", "changed_by": "console"}}
        query = {"status": "pending_approval", "asset": "free_trial", "channel": "email", "theme": "yoga"}
        body = json.loads(webapp.handler(request("/api/variations", query=query), None)["body"])
        self.assertEqual(body["matching"], 1)
        self.assertEqual(body["totals"]["pending_approval"], 1)
        self.assertEqual(body["variations"][0]["id"], key)

    def test_rejects_bad_filters(self):
        response = webapp.handler(request("/api/variations", query={"status": "x"}), None)
        self.assertEqual(response["statusCode"], 400)

    def test_status_change_needs_password_and_valid_input(self):
        good = "free_trial~email~yoga-03"
        self.assertEqual(self.post(good, {"status": "approved"}, token="wrong")["statusCode"], 401)
        self.assertEqual(self.post("bad", {"status": "approved"})["statusCode"], 400)
        self.assertEqual(self.post(good, {"status": "other"})["statusCode"], 400)
        self.set_status.assert_not_called()
        self.assertEqual(self.post(good, {"status": "pending_approval"})["statusCode"], 200)
        self.set_status.assert_called_once_with(good, "pending_approval")


class ModelPageTests(unittest.TestCase):
    def test_model_card_requires_password(self):
        with mock.patch.object(webapp, "_get_token", return_value="secret"):
            self.assertEqual(webapp.handler(request("/api/model", token=None), None)["statusCode"], 401)
            body = json.loads(webapp.handler(request("/api/model"), None)["body"])
        self.assertGreater(body["test_metrics"]["auc"], 0.6)
        self.assertTrue(body["importance"])
        self.assertEqual(len(body["calibration"]), 10)


class JourneyTests(unittest.TestCase):
    def test_returns_journeys_and_requires_password(self):
        webapp._cache.clear()
        self.addCleanup(webapp._cache.clear)
        rows = [{"journey": "free_trial:email>coach_consult:email", "steps": "2", "reached": "50", "registered": "5"}]
        with mock.patch.object(webapp, "_get_token", return_value="secret"), mock.patch.object(
            webapp, "run_query", return_value=rows
        ) as query:
            self.assertEqual(webapp.handler(request("/api/journeys", token=None), None)["statusCode"], 401)
            query.assert_not_called()
            body = json.loads(webapp.handler(request("/api/journeys"), None)["body"])
        self.assertEqual(body["journeys"], rows)
        self.assertEqual(body["min_people"], webapp.JOURNEY_MIN_PEOPLE)
        self.assertIn("direct_mail", body["channel_cost"])

    def test_query_counts_each_step_and_excludes_onboarding(self):
        self.assertIn("touch <> 'onboarding'", webapp.JOURNEY_SQL)
        self.assertIn("HAVING COUNT(*) >= 30", webapp.JOURNEY_SQL)


class ProfileTests(unittest.TestCase):
    PROFILE = {
        "customer_id": "c1",
        "first_name": "Alex<script>",
        "age": "40",
        "tenure_months": "60",
        "app_sessions_30d": "8",
        "days_since_active": "10",
        "email_open_rate": "0.5",
        "wellness_member": "0",
        "touches": "1",
        "opens": "1",
        "clicks": "0",
        "consent_marketing": "true",
        "consent_email": "true",
        "consent_direct_mail": "false",
    }
    MESSAGE = {
        "message_id": "c1-1",
        "asset_id": "free_trial",
        "channel": "email",
        "image_id": "yoga-03",
        "image_theme": "yoga",
    }

    def setUp(self):
        webapp._cache.clear()
        patches = [
            mock.patch.object(webapp, "_get_token", return_value="secret"),
            mock.patch.object(webapp, "_current_model", return_value=webapp.nba.BUNDLED_MODEL),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def get(self, side_effect):
        with mock.patch.object(webapp, "run_query", side_effect=side_effect) as query:
            response = webapp.handler(request("/api/people/c1"), None)
        return response, json.loads(response["body"]), query

    def test_profile_includes_scores_and_message_content(self):
        response, body, query = self.get([[dict(self.PROFILE)], [dict(self.MESSAGE)]])
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(body["profile"]["customer_id"], "c1")
        self.assertEqual({offer["channel"] for offer in body["offers"]}, {"email"})
        values = [offer["expected_value"] for offer in body["offers"]]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertEqual(len(body["themes"]), len(webapp.content.THEMES))
        message = body["history"][0]["content_html"]
        self.assertIn("/images/yoga-03.jpg", message)
        self.assertNotIn("<script>", message)
        self.assertEqual(query.call_args_list[0].args[1], ["'c1'"])

    def test_no_consent_means_no_offer_scores(self):
        _, body, _ = self.get([[{**self.PROFILE, "consent_marketing": "false"}], []])
        self.assertEqual(body["offers"], [])

    def test_unknown_person(self):
        response, body, _ = self.get([[]])
        self.assertEqual(response["statusCode"], 200)
        self.assertIsNone(body["profile"])


class UsageTests(unittest.TestCase):
    def setUp(self):
        webapp._cache.clear()
        self.addCleanup(webapp._cache.clear)
        patches = [
            mock.patch.object(webapp, "_get_token", return_value="secret"),
            mock.patch.object(webapp, "run_query", return_value=[{"people": "10000", "messages_sent": "5"}]),
            mock.patch.object(webapp, "_storage", return_value=[{"area": "a", "files": 2, "bytes": 10}]),
            mock.patch.object(webapp, "_activity", return_value=[{"name": "x", "count": 3}]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def body(self):
        return json.loads(webapp.handler(request("/api/usage"), None)["body"])

    def test_requires_password(self):
        self.assertEqual(webapp.handler(request("/api/usage", token=None), None)["statusCode"], 401)

    def test_groups_costs_by_service_and_keeps_other_costs(self):
        amounts = {"AWS Lambda": 1.5, "Amazon DynamoDB": 0.5, "Amazon Rekognition": 2.0}
        with mock.patch.object(webapp, "_costs", return_value={"from": "2026-10-01", "by_service": amounts}) as costs:
            body = self.body()
            webapp._cache.pop("usage")
            self.body()
        self.assertEqual(costs.call_count, 1)  # The paid billing request is cached separately.
        self.assertEqual(body["total_cost"], 4.0)
        by_name = {s["name"]: s["cost"] for s in body["services"]}
        self.assertEqual(by_name["Lambda"], 1.5)
        self.assertEqual(by_name["Amazon Rekognition"], 2.0)
        self.assertEqual(body["unavailable"], [])

    def test_blocked_billing_does_not_break_the_page(self):
        with mock.patch.object(webapp, "_costs", side_effect=RuntimeError("denied")):
            response = webapp.handler(request("/api/usage"), None)
        body = json.loads(response["body"])
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(body["unavailable"], ["costs"])
        self.assertEqual(body["records"]["people"], "10000")
        self.assertNotIn("denied", response["body"])


class QueryTests(unittest.TestCase):
    def test_turns_athena_rows_into_records(self):
        athena = mock.Mock()
        athena.start_query_execution.return_value = {"QueryExecutionId": "q"}
        athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
        athena.get_query_results.return_value = {
            "ResultSet": {
                "Rows": [
                    {"Data": [{"VarCharValue": "status"}, {"VarCharValue": "people"}]},
                    {"Data": [{"VarCharValue": "active"}, {}]},
                ]
            }
        }
        boto = mock.Mock()
        boto.client.return_value = athena
        with mock.patch.object(webapp, "boto3", boto), mock.patch.dict(
            os.environ, {"ATHENA_WORKGROUP": "w", "ATHENA_DATABASE": "d"}
        ):
            rows = webapp.run_query("SELECT 1", ["'x'"])
        self.assertEqual(rows, [{"status": "active", "people": None}])
        self.assertEqual(athena.start_query_execution.call_args.kwargs["ExecutionParameters"], ["'x'"])


if __name__ == "__main__":
    unittest.main()
