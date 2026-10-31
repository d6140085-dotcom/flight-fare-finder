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
    conditions.Key = Attr
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


MSG = {
    "email": "user@example.com", "route": "TPE-TYO", "plan_name": "tokyo", "target_price": 10000,
    "cheapest": {"price": 7634, "currency": "TWD", "airline": "TR",
                 "depart_date": "2026-11-02T06:50:00+08:00", "return_date": "2026-11-07T22:05:00+09:00"},
    "cheapest_usd": {"price": 234, "currency": "USD", "airline": "TR"},
}
NOW = datetime.datetime(2026, 10, 31, 8, 0, tzinfo=datetime.timezone.utc)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.n = _load("flight-fare-notification")
        self.n._cfg = {"key": "re_test", "from": "onboarding@resend.dev"}
        self.n._hist = mock.MagicMock()
        self.n._hist.query.return_value = {"Items": []}
        for k in ("NOTIFY_FLOOR_HOURS", "REALERT_PCT", "REALERT_ABS_TWD"):
            os.environ.pop(k, None)

    def last(self, hours_ago, price):
        return {"sent_at": (NOW - datetime.timedelta(hours=hours_ago)).isoformat(), "price": Decimal(price)}

    def test_dedup_rules(self):
        s = self.n.should_send
        self.assertTrue(s(None, Decimal(7634), NOW))                      # never alerted
        self.assertFalse(s(self.last(1, 7634), Decimal(7634), NOW))      # same price inside 24h
        self.assertTrue(s(self.last(25, 7634), Decimal(7634), NOW))      # floor elapsed
        self.assertTrue(s(self.last(1, 10000), Decimal(8000), NOW))      # >= 20% cheaper
        self.assertTrue(s(self.last(1, 20000), Decimal(17999), NOW))     # >= NT$2,000 cheaper
        self.assertFalse(s(self.last(1, 9000), Decimal(8000), NOW))      # 11% / NT$1,000: not enough

    def test_resend_param_shapes(self):
        self.n._cfg = None
        self.n._ssm = mock.MagicMock()
        os.environ["RESEND_PARAM"] = "/flight/resend"
        self.n._ssm.get_parameter.return_value = {"Parameter": {"Value": "re_bare"}}
        self.assertEqual(self.n.resend_cfg(), {"key": "re_bare", "from": "onboarding@resend.dev"})
        self.n._cfg = None
        self.n._ssm.get_parameter.return_value = {"Parameter": {"Value": '{"api_key":"re_x","from":"A <a@example.com>"}'}}
        self.assertEqual(self.n.resend_cfg()["from"], "A <a@example.com>")

    def test_render(self):
        subject, page, text = self.n.render(MSG)
        self.assertEqual(subject, "✈️ 台北 → 東京 降價通知！NT$7,634 已達標")
        self.assertIn("約 US$234", page)
        self.assertIn("立即訂購", page)
        self.assertIn("https://www.aviasales.com/search/TPE0211TYO0711", text)
        no_usd = {k: v for k, v in MSG.items() if k != "cheapest_usd"}
        self.assertNotIn("US$", self.n.render(no_usd)[1])

    def _handle(self, status):
        with mock.patch.object(self.n, "send", return_value=(status, "err")) as send:
            out = self.n.handler({"Records": [{"messageId": "m1", "body": json.dumps(MSG)}]}, None)
        return out, send

    def test_2xx_sends_and_records(self):
        out, send = self._handle(200)
        self.assertEqual(out["batchItemFailures"], [])
        item = self.n._hist.put_item.call_args.kwargs["Item"]
        self.assertEqual(item["pk"], "user@example.com#TPE-TYO")
        self.assertEqual(item["price"], Decimal(7634))
        self.assertEqual(send.call_args.args[1], "user@example.com")

    def test_permanent_error_dropped_without_history(self):
        for status in (403, 422):
            self.n._hist.put_item.reset_mock()
            out, _ = self._handle(status)
            self.assertEqual(out["batchItemFailures"], [])
            self.n._hist.put_item.assert_not_called()

    def test_transient_error_retried_without_history(self):
        for status in (429, 500):
            out, _ = self._handle(status)
            self.assertEqual(out["batchItemFailures"], [{"itemIdentifier": "m1"}])
        self.n._hist.put_item.assert_not_called()

    def test_resend_error_text_masks_addresses(self):
        body = b'{"message":"You can only send testing emails to your own email address (owner@example.com)."}'
        err = urllib.error.HTTPError("https://api.resend.com/emails", 403, "Forbidden", {}, io.BytesIO(body))
        with mock.patch.object(self.n.urllib.request, "urlopen", side_effect=err):
            status, text = self.n.send(self.n._cfg, "user@example.com", "s", "<p>h</p>", "t")
        self.assertEqual(status, 403)
        self.assertNotIn("@", text)
        self.assertIn("<email>", text)

    def test_deduped_message_not_sent(self):
        self.n._hist.query.return_value = {"Items": [{"sent_at": "2099-01-01T00:00:00+00:00", "price": Decimal(7634)}]}
        out, send = self._handle(200)
        send.assert_not_called()
        self.assertEqual(out["batchItemFailures"], [])


if __name__ == "__main__":
    unittest.main()
