"""Offline tests for the M1.2 Lambdas. boto3 and the network are stubbed; no AWS calls.

Run: python3 -m unittest discover -s aws/tests -v
"""
import datetime
import importlib.util
import io
import json
import os
import sys
import types
import unittest
import urllib.error
from decimal import Decimal
from pathlib import Path
from unittest import mock

LAMBDAS = Path(__file__).resolve().parents[1] / "lambdas"


def _stub_boto3():
    boto3 = types.ModuleType("boto3")
    boto3.client = mock.MagicMock(name="client")
    boto3.resource = mock.MagicMock(name="resource")
    dynamodb = types.ModuleType("boto3.dynamodb")
    conditions = types.ModuleType("boto3.dynamodb.conditions")

    class Attr:
        def __init__(self, name):
            self.name = name

        def eq(self, value):
            return ("eq", self.name, value)

    conditions.Attr = Attr
    sys.modules.update({"boto3": boto3, "boto3.dynamodb": dynamodb, "boto3.dynamodb.conditions": conditions})


def _load(name):
    _stub_boto3()
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), LAMBDAS / name / "index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tp_response(dest, offers):
    return io.BytesIO(json.dumps({"success": True, "data": {dest: offers}}).encode())


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.p = _load("flight-parser")
        self.p._token = None
        self.p._ssm = mock.MagicMock()
        self.p._sqs = mock.MagicMock()
        self.p._table = mock.MagicMock()
        os.environ["FARE_QUEUE_URL"] = "https://sqs.example/queue"

    def test_next_month_rolls_over_december(self):
        self.assertEqual(self.p.next_month(datetime.date(2026, 12, 15)), "2027-01")
        self.assertEqual(self.p.next_month(datetime.date(2026, 10, 31)), "2026-11")

    def test_token_accepts_bare_string_or_json(self):
        self.p._ssm.get_parameter.return_value = {"Parameter": {"Value": "abc123"}}
        self.assertEqual(self.p.get_token(), "abc123")
        self.p._token = None
        self.p._ssm.get_parameter.return_value = {"Parameter": {"Value": '{"token":"xyz","marker":"1"}'}}
        self.assertEqual(self.p.get_token(), "xyz")

    def test_token_sent_in_header_not_url(self):
        captured = {}

        def fake_urlopen(req, timeout):
            captured["url"], captured["headers"] = req.full_url, dict(req.header_items())
            return _tp_response("TYO", {"0": {"price": 9325, "airline": "LJ", "departure_at": "d", "return_at": "r"}})

        with mock.patch.object(self.p.urllib.request, "urlopen", fake_urlopen):
            fare = self.p.fetch_cheapest("TPE", "TYO", "2026-11", "SECRET", "twd")
        self.assertNotIn("SECRET", captured["url"])
        self.assertEqual(captured["headers"].get("X-access-token"), "SECRET")
        self.assertEqual(fare["price"], 9325)
        self.assertEqual(fare["depart_date"], "d")  # parsed from departure_at

    def test_picks_cheapest_offer(self):
        offers = {"0": {"price": 9900, "airline": "BR"}, "1": {"price": 9325, "airline": "LJ"}}
        with mock.patch.object(self.p.urllib.request, "urlopen", lambda req, timeout: _tp_response("TYO", offers)):
            self.assertEqual(self.p.fetch_cheapest("TPE", "TYO", "2026-11", "t", "twd")["airline"], "LJ")

    def test_http_error_returns_none(self):
        err = urllib.error.HTTPError("https://x", 429, "Too Many Requests", {}, None)
        with mock.patch.object(self.p.urllib.request, "urlopen", side_effect=err):
            self.assertIsNone(self.p.fetch_cheapest("TPE", "TYO", "2026-11", "t", "usd"))

    def _run(self, twd, usd, items):
        self.p._ssm.get_parameter.return_value = {"Parameter": {"Value": "tok"}}
        self.p._table.scan.return_value = {"Items": items}
        with mock.patch.object(self.p, "fetch_cheapest", side_effect=[twd, usd]):
            return self.p.handler({"origin": "TPE", "destination": "TYO", "route": "TPE-TYO"}, None)

    def test_enqueues_only_met_targets_with_dual_currency(self):
        twd = {"price": 9325, "currency": "TWD", "airline": "LJ", "depart_date": "d", "return_date": "r"}
        usd = {"price": 298, "currency": "USD", "airline": "LJ", "depart_date": "d", "return_date": "r"}
        items = [
            {"email": "a@example.com", "route": "TPE-TYO", "plan_name": "tokyo", "target_price": Decimal("10000")},
            {"email": "b@example.com", "route": "TPE-TYO", "plan_name": "tokyo", "target_price": Decimal("8800")},
            {"email": "c@example.com", "route": "TPE-TYO", "plan_name": "tokyo", "target_price": Decimal("9325")},
        ]
        out = self._run(twd, usd, items)
        self.assertEqual(out["matched"], 2)
        bodies = [json.loads(c.kwargs["MessageBody"]) for c in self.p._sqs.send_message.call_args_list]
        self.assertEqual({b["email"] for b in bodies}, {"a@example.com", "c@example.com"})
        self.assertEqual(bodies[0]["cheapest"]["currency"], "TWD")
        self.assertEqual(bodies[0]["cheapest_usd"]["price"], 298)
        self.assertIsInstance(bodies[0]["target_price"], int)
        self.assertNotIn("subscription_status", bodies[0])

    def test_usd_missing_still_enqueues_twd_only(self):
        twd = {"price": 9325, "currency": "TWD", "airline": "LJ", "depart_date": "d", "return_date": "r"}
        items = [{"email": "a@example.com", "route": "TPE-TYO", "target_price": Decimal("10000")}]
        self.assertEqual(self._run(twd, None, items)["matched"], 1)
        self.assertNotIn("cheapest_usd", json.loads(self.p._sqs.send_message.call_args.kwargs["MessageBody"]))

    def test_no_twd_fare_skips_without_scanning(self):
        out = self._run(None, None, [])
        self.assertEqual(out["matched"], 0)
        self.p._table.scan.assert_not_called()
        self.p._sqs.send_message.assert_not_called()

    def test_scan_paginates(self):
        self.p._table.scan.side_effect = [
            {"Items": [{"email": "a@example.com"}], "LastEvaluatedKey": {"k": 1}},
            {"Items": [{"email": "b@example.com"}]},
        ]
        self.assertEqual(len(list(self.p.subscribers_for("TPE-TYO"))), 2)
        self.assertEqual(self.p._table.scan.call_args_list[1].kwargs["ExclusiveStartKey"], {"k": 1})


class WrapperTests(unittest.TestCase):
    def test_fans_out_one_async_invoke_per_route(self):
        w = _load("flight-parser-wrapper")
        w._s3 = mock.MagicMock()
        w._lambda = mock.MagicMock()
        routes = (LAMBDAS.parent / "config" / "flight-routes.json").read_bytes()
        w._s3.get_object.return_value = {"Body": io.BytesIO(routes)}
        os.environ["CONFIG_BUCKET"] = "bucket"
        out = w.handler({}, None)
        self.assertEqual(out["routes"], ["TPE-TYO", "TPE-SEL"])
        calls = w._lambda.invoke.call_args_list
        self.assertTrue(all(c.kwargs["InvocationType"] == "Event" for c in calls))
        self.assertEqual(json.loads(calls[1].kwargs["Payload"])["destination"], "SEL")


if __name__ == "__main__":
    unittest.main()
