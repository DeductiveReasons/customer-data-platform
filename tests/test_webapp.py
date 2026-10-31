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
