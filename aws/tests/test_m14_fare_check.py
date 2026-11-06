"""Offline tests for the M1.4 Phase 2A fare check (aws/lambdas/flight-fare-check).

No AWS and no network: Travelpayouts answers come from fixtures or inline dicts; boto3 is
stubbed. Run: python3 -m unittest discover -s aws/tests -p 'test_*.py'
"""
import base64
import importlib.util
import io
import json
import sys
import types
import unittest
import urllib.parse
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

AWS = Path(__file__).resolve().parents[1]
PKG = AWS / "lambdas" / "flight-fare-check"
FIX = Path(__file__).resolve().parent / "fixtures" / "travelpayouts"
sys.path.insert(0, str(PKG))

from farecheck import compat, config, effects, matching, months, runner, status, workitems  # noqa: E402
from farecheck import travelpayouts as tp  # noqa: E402

NOW = datetime(2026, 11, 6, 13, 0, tzinfo=timezone.utc)      # 21:00 Taipei; next month 2026-12
CATALOG = config.load_catalog()
ORIGINS = sorted(CATALOG.origins)
DESTS = sorted(CATALOG.destinations)
TOKEN = "SECRET-TOKEN-123"


def fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def settings(**kw):
    return config.Settings(**kw)


def offer(price=8000, dep="2026-12-11T07:30:00+08:00", ret="2026-12-18T19:00:00+09:00", **extra):
    o = {"price": price, "airline": "BR", "departure_at": dep, "return_at": ret}
    o.update(extra)
    return o


def v3_body(*offers, currency="twd"):
    return {"success": True, "currency": currency, "data": list(offers)}


def v1_body(dest, *offers, currency="twd"):
    return {"success": True, "currency": currency, "data": {dest: {str(i): o for i, o in enumerate(offers)}}}


class Transport:
    """Records every request; answers from a function of (api, query)."""
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def __call__(self, url, headers, timeout):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.calls.append({"url": url, "query": q, "headers": headers})
        r = self.answer(url, q)
        if isinstance(r, Exception):
            raise r
        return r


def client(answer, api="v3", now=NOW, logs=None):
    t = Transport(answer)
    return tp.Client(TOKEN, CATALOG, now, api=api, transport=t, log=(logs.append if logs is not None else lambda m: None)), t


def row(email, route, **kw):
    o, d = route.split("-")
    r = {"email": email, "route": route, "origin": o, "destination": d, "target_price": Decimal("9000")}
    r.update(kw)
    return r


# ---- catalog and codes ---------------------------------------------------------------------

class CatalogCodeTests(unittest.TestCase):
    def test_catalog_has_three_origins_and_95_destinations(self):
        self.assertEqual(ORIGINS, ["KHH", "RMQ", "TPE"])
        self.assertEqual(len(DESTS), 95)
        self.assertEqual(CATALOG.airports("TPE"), {"TPE", "TSA"})
        self.assertEqual(CATALOG.airports("KHH"), {"KHH"})

    def test_every_origin_destination_pair_becomes_a_work_item_and_request(self):
        rows = [row(f"{o}{d}@example.com", f"{o}-{d}", departure_month="2027-03") for o in ORIGINS for d in DESTS]
        plan = workitems.build(rows, NOW, CATALOG)
        self.assertEqual(len(plan.items), 3 * 95)
        self.assertEqual(plan.counts["active"], 285)
        c, t = client(lambda u, q: v3_body())
        for item in plan.items:
            self.assertEqual(runner.parse_event(item.payload(), CATALOG, NOW), item)
            c.fetch(item.origin, item.destination, item.month)
        self.assertEqual(len(t.calls), 285)
        self.assertTrue(all(call["query"]["one_way"] == "false" and call["query"]["currency"] == "twd"
                            and call["query"]["departure_at"] == "2027-03" for call in t.calls))

    def test_every_destination_accepts_its_own_airports_and_rejects_foreign_ones(self):
        for d in DESTS:
            for ap in CATALOG.airports(d):
                o, why = tp.validate_offer(offer(destination=d, destination_airport=ap, origin="TPE", origin_airport="TPE"),
                                           origin="TPE", destination=d, month="2026-12", currency="twd",
                                           catalog=CATALOG, now=NOW, source="v3")
                self.assertIsNotNone(o, (d, ap, why))
                self.assertEqual(o.destination_airport, ap)
            foreign = "ZZZ"
            _, why = tp.validate_offer(offer(destination_airport=foreign), origin="TPE", destination=d, month="2026-12",
                                       currency="twd", catalog=CATALOG, now=NOW, source="v3")
            self.assertEqual(why, "destination_airport_unexpected")

    def test_origin_airports_tpe_city_vs_single_airports(self):
        ok = lambda o, ap: tp.validate_offer(offer(origin_airport=ap), origin=o, destination="OKA", month="2026-12",
                                             currency="twd", catalog=CATALOG, now=NOW, source="v3")
        self.assertEqual(ok("TPE", "TSA")[0].origin_airport, "TSA")   # Songshan under city TPE (T2)
        self.assertEqual(ok("TPE", "tpe")[0].origin_airport, "TPE")   # case-insensitive
        self.assertEqual(ok("KHH", "TSA")[1], "origin_airport_unexpected")
        self.assertEqual(ok("RMQ", "RMQ")[0].origin_airport, "RMQ")
        self.assertEqual(ok("KHH", "TPE")[1], "origin_airport_unexpected")

    def test_unknown_or_inconsistent_codes_are_rejected(self):
        for ev in ({"origin": "TSA", "destination": "TYO"}, {"origin": "TPE", "destination": "ZZZ"},
                   {"origin": "TPE", "destination": "TYO", "route": "TPE-SEL"}, {"origin": None, "destination": "TYO"},
                   {"origin": "TPE", "destination": "TYO", "month": "2026-13"}):
            with self.assertRaises(ValueError):
                runner.parse_event(ev, CATALOG, NOW)
        for r in (row("a@example.com", "TPE-ZZZ"), {"route": "TPE"}, {"route": None},
                  dict(row("b@example.com", "TPE-TYO"), origin="KHH"),
                  row("c@example.com", "TPE-TYO", departure_month="2026/12")):
            self.assertEqual(workitems.classify(r, NOW, CATALOG)[0], workitems.INVALID)


# ---- months -------------------------------------------------------------------------------

class MonthTests(unittest.TestCase):
    def test_parity_with_subscriptions_api_months(self):
        spec = importlib.util.spec_from_file_location(
            "subsapi_months", AWS / "lambdas" / "flight-subscriptions-api" / "subsapi" / "months.py")
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        while t < datetime(2028, 1, 1, tzinfo=timezone.utc):
            for dep in (None, "2026-03", "2027-01", "2027-12"):
                self.assertEqual(months.effective_month(dep, t), api.effective_month(dep, t))
                self.assertEqual(months.is_expired(dep, t), api.is_expired(dep, t))
            t += timedelta(hours=7, minutes=13)

    def test_rolling_rows_use_next_month_in_taipei(self):
        self.assertEqual(months.effective_month(None, NOW), "2026-12")
        dec31_taipei = datetime(2026, 12, 31, 16, 30, tzinfo=timezone.utc)   # 2027-01-01 00:30 Taipei
        self.assertEqual(months.effective_month(None, dec31_taipei), "2027-02")
        self.assertEqual(months.effective_month(None, datetime(2026, 12, 15, tzinfo=timezone.utc)), "2027-01")

    def test_different_departure_months_are_separate_lookups(self):
        rows = [row(f"u{m}@example.com", "TPE-TYO", departure_month=m) for m in ("2026-12", "2027-01", "2027-06", "2027-11")]
        plan = workitems.build(rows, NOW, CATALOG)
        self.assertEqual([i.key for i in plan.items],
                         ["TPE-TYO#2026-12", "TPE-TYO#2027-01", "TPE-TYO#2027-06", "TPE-TYO#2027-11"])

    def test_current_month_is_active_previous_month_expired(self):
        self.assertEqual(workitems.classify(row("a@example.com", "TPE-TYO", departure_month="2026-11"), NOW, CATALOG)[0],
                         workitems.ACTIVE)
        self.assertEqual(workitems.classify(row("a@example.com", "TPE-TYO", departure_month="2026-10"), NOW, CATALOG)[0],
                         workitems.EXPIRED)


# ---- work items: legacy, paused, expired, dedup --------------------------------------------

class WorkItemTests(unittest.TestCase):
    def test_fixture_plan(self):
        plan = workitems.build(fixture("subscriptions.json"), NOW, CATALOG)
        self.assertEqual([i.key for i in plan.items], ["KHH-OKA#2027-01", "RMQ-OKA#2027-01", "TPE-SEL#2026-12",
                                                       "TPE-TYO#2026-12", "TPE-TYO#2027-02"])
        self.assertEqual(plan.counts, {"active": 7, "paused": 1, "expired": 1, "work_items": 5})

    def test_dedup_same_route_month_across_users_and_rolling_vs_fixed(self):
        rows = [row("a@example.com", "TPE-TYO"), row("b@example.com", "TPE-TYO", plan_name="tokyo"),
                row("c@example.com", "TPE-TYO", departure_month="2026-12"),
                row("d@example.com", "TPE-TYO", departure_month="2026-12")]
        self.assertEqual([i.key for i in workitems.build(rows, NOW, CATALOG).items], ["TPE-TYO#2026-12"])

    def test_legacy_m1_rows_without_origin_attributes(self):
        r = {"email": "m1@example.com", "route": "TPE-SEL", "plan_name": "seoul", "target_price": Decimal(7000)}
        self.assertEqual(workitems.classify(r, NOW, CATALOG), (workitems.ACTIVE, workitems.WorkItem("TPE", "SEL", "2026-12")))

    def test_paused_and_expired_rows_produce_no_lookup(self):
        rows = [row("p@example.com", "TPE-OKA", departure_month="2027-01", alerts_paused=True),
                row("e@example.com", "TPE-BKK", departure_month="2026-09"),
                row("pe@example.com", "TPE-SEL", departure_month="2026-09", alerts_paused=True),
                row("rp@example.com", "TPE-HKG", alerts_paused=True)]
        plan = workitems.build(rows, NOW, CATALOG)
        self.assertEqual(plan.items, ())
        self.assertEqual(plan.counts, {"paused": 2, "expired": 2, "work_items": 0})

    def test_alerts_paused_false_or_missing_is_active(self):
        for v in (False, None):
            r = row("a@example.com", "TPE-OKA", alerts_paused=v)
            self.assertEqual(workitems.classify(r, NOW, CATALOG)[0], workitems.ACTIVE)

    def test_wrapper_scan_never_requests_email(self):
        table = mock.MagicMock()
        table.scan.side_effect = [{"Items": [{"route": "TPE-TYO"}], "LastEvaluatedKey": {"k": 1}}, {"Items": []}]
        rows = list(effects.TableReader(table).rows_without_email())
        self.assertEqual(rows, [{"route": "TPE-TYO"}])
        kw = table.scan.call_args_list[0].kwargs
        self.assertNotIn("email", json.dumps(kw))
        self.assertEqual(set(kw["ExpressionAttributeNames"].values()),
                         {"route", "origin", "destination", "departure_month", "alerts_paused"})
        self.assertEqual(table.scan.call_args_list[1].kwargs["ExclusiveStartKey"], {"k": 1})


# ---- v3 adapter ----------------------------------------------------------------------------

class V3AdapterTests(unittest.TestCase):
    def test_request_is_round_trip_twd_month_and_token_only_in_header(self):
        c, t = client(lambda u, q: v3_body())
        c.fetch("TPE", "TYO", "2026-12")
        call = t.calls[0]
        self.assertTrue(call["url"].startswith(tp.V3_URL + "?"))
        self.assertEqual(call["query"], {"origin": "TPE", "destination": "TYO", "departure_at": "2026-12",
                                         "one_way": "false", "currency": "twd", "sorting": "price", "limit": "30"})
        self.assertNotIn("market", call["query"])
        self.assertNotIn(TOKEN, call["url"])
        self.assertEqual(call["headers"]["X-Access-Token"], TOKEN)

    def test_fixture_cheapest_valid_offer_and_rejections(self):
        c, _ = client(lambda u, q: fixture("v3_tpe_tyo_2026-12_twd.json"))
        r = c.fetch("TPE", "TYO", "2026-12")
        self.assertEqual(r.kind, tp.OK)
        self.assertEqual((r.offer.price, r.offer.origin_airport, r.offer.destination_airport), (8062, "TSA", "HND"))
        self.assertEqual((r.offer.depart_date, r.offer.return_date), ("2026-12-11", "2026-12-18"))
        self.assertEqual(r.offer.link, "/search/TSA1112HND18121?t=example")
        self.assertEqual(r.offers_seen, 9)
        self.assertEqual(r.rejected, Counter({"one_way": 1, "wrong_month": 1, "origin_mismatch": 1, "currency_mismatch": 1,
                                              "return_before_departure": 1, "destination_airport_unexpected": 1,
                                              "bad_price": 1}))

    def test_false_low_prices_never_win(self):
        """Each cheaper offer here is invalid; the alert price must be the valid 9000."""
        cheaper_but_wrong = [
            offer(1000, ret=None), offer(1001, ret=""),                          # one-way
            offer(1002, currency="usd"), offer(1003, currency="RUB"),            # currency
            offer(1004, dep="2027-01-03T08:00:00+08:00", ret="2027-01-09T08:00:00+08:00"),   # wrong month
            offer(1005, origin_airport="KHH"), offer(1006, destination="OSA"),   # wrong codes
        ]
        c, _ = client(lambda u, q: v3_body(*cheaper_but_wrong, offer(9000)))
        r = c.fetch("TPE", "TYO", "2026-12")
        self.assertEqual(r.offer.price, 9000)
        self.assertEqual(sum(r.rejected.values()), 7)

    def test_empty_unsuccessful_errors_and_malformed(self):
        cases = {
            "empty": (lambda u, q: v3_body(), tp.NO_DATA, "empty"),
            "null data": (lambda u, q: {"success": True, "data": None}, tp.NO_DATA, "empty"),
            "unsuccessful": (lambda u, q: {"success": False, "error": "x", "data": []}, tp.ERROR, "api_unsuccessful"),
            "429": (lambda u, q: tp.HttpError(429), tp.ERROR, "http_429"),
            "500": (lambda u, q: tp.HttpError(500), tp.ERROR, "http_500"),
            "timeout": (lambda u, q: TimeoutError(), tp.ERROR, "unavailable"),
            "network": (lambda u, q: OSError("reset"), tp.ERROR, "unavailable"),
            "bad json": (lambda u, q: ValueError("bad"), tp.ERROR, "malformed"),
            "not object": (lambda u, q: ["x"], tp.ERROR, "malformed"),
            "data object": (lambda u, q: {"success": True, "data": {"TYO": {}}}, tp.ERROR, "malformed"),
            "all rejected": (lambda u, q: v3_body(offer(ret=None)), tp.NO_DATA, "all_rejected"),
        }
        for name, (answer, kind, reason) in cases.items():
            c, _ = client(answer)
            r = c.fetch("TPE", "TYO", "2026-12")
            self.assertEqual((r.kind, r.reason), (kind, reason), name)
            self.assertIsNone(r.offer, name)

    def test_response_currency_mismatch_is_an_error_not_a_fare(self):
        c, _ = client(lambda u, q: v3_body(offer(250), currency="usd"))
        r = c.fetch("TPE", "TYO", "2026-12", "twd")
        self.assertEqual((r.kind, r.reason), (tp.ERROR, "currency_mismatch"))

    def test_missing_currency_fields_accepted_as_requested(self):
        c, _ = client(lambda u, q: {"success": True, "data": [offer(8000)]})
        self.assertEqual(c.fetch("TPE", "TYO", "2026-12").offer.currency, "TWD")

    def test_round_trip_date_validation(self):
        v = lambda **kw: tp.validate_offer(offer(**kw), origin="TPE", destination="TYO", month="2026-12",
                                           currency="twd", catalog=CATALOG, now=NOW, source="v3")[1]
        self.assertIsNone(v())
        self.assertIsNone(v(dep="2026-12-11", ret="2026-12-11"))                            # date-only, same day
        self.assertIsNone(v(dep="2026-12-31T23:30:00+08:00", ret="2027-01-04T10:00:00+09:00"))
        self.assertEqual(v(dep="2026-12-11T10:00:00+08:00", ret="2026-12-11T09:00:00+08:00"), "return_before_departure")
        self.assertEqual(v(dep="2026-12-11", ret="2026-12-10"), "return_before_departure")
        self.assertEqual(v(ret="2028-01-01T10:00:00+09:00"), "return_too_far")
        self.assertEqual(v(dep="2026-11-30T23:30:00+08:00"), "wrong_month")
        self.assertEqual(v(dep="2027-12-01T08:00:00+08:00", ret="2027-12-05T08:00:00+08:00"), "wrong_month")
        for bad in ("2026-13-01", "2026-02-30T10:00:00", "20261211", "", None, 20261211):
            self.assertEqual(v(dep=bad), "bad_departure", bad)
            self.assertEqual(v(ret=bad if bad not in ("", None) else "x"), "bad_return", bad)

    def test_stale_offers_departed_or_expired(self):
        late = datetime(2026, 12, 11, 0, 0, tzinfo=timezone.utc)   # 08:00 Taipei on the travel day
        v = lambda now, **kw: tp.validate_offer(offer(**kw), origin="TPE", destination="TYO", month="2026-12",
                                                currency="twd", catalog=CATALOG, now=now, source="v1")[1]
        self.assertEqual(v(late, dep="2026-12-11T07:30:00+08:00"), "departed")
        self.assertIsNone(v(late, dep="2026-12-11T09:30:00+08:00"))
        self.assertEqual(v(late + timedelta(days=1), dep="2026-12-11", ret="2026-12-15"), "departed")
        self.assertEqual(v(NOW, expires_at="2026-11-05T00:00:00Z"), "expired")
        self.assertEqual(v(NOW, expires_at="garbage"), "expired")
        self.assertIsNone(v(NOW, expires_at="2026-11-07T00:00:00Z"))

    def test_price_parsing(self):
        for raw, expect in ((8062, 8062), (8062.0, 8062), ("8062", 8062), (8062.5, 8063)):
            self.assertEqual(tp.to_price(raw), expect, raw)
        for bad in (0, -5, True, None, "abc", float("nan"), float("inf"), 2_000_001, [], {}):
            self.assertIsNone(tp.to_price(bad), bad)

    def test_ties_are_stable(self):
        a = offer(8000, dep="2026-12-12T08:00:00+08:00", ret="2026-12-15T08:00:00+08:00", airline="JX")
        b = offer(8000, dep="2026-12-10T08:00:00+08:00", ret="2026-12-15T08:00:00+08:00", airline="CI")
        for order in ((a, b), (b, a)):
            c, _ = client(lambda u, q, order=order: v3_body(*order))
            self.assertEqual(c.fetch("TPE", "TYO", "2026-12").offer.airline, "CI")

    def test_token_never_logged(self):
        logs = []
        for ans in (tp.HttpError(403), OSError("x"), ValueError("x")):
            c, _ = client(lambda u, q, ans=ans: ans, logs=logs)
            c.fetch("TPE", "TYO", "2026-12")
        self.assertTrue(logs)
        self.assertFalse(any(TOKEN in line for line in logs))


# ---- v1 adapter and M1 compatibility ------------------------------------------------------

def load_m1_parser():
    boto3 = types.ModuleType("boto3")
    boto3.client = mock.MagicMock()
    boto3.resource = mock.MagicMock()
    cond = types.ModuleType("boto3.dynamodb.conditions")
    cond.Attr = lambda name: mock.MagicMock()
    with mock.patch.dict(sys.modules, {"boto3": boto3, "boto3.dynamodb": types.ModuleType("boto3.dynamodb"),
                                       "boto3.dynamodb.conditions": cond}):
        spec = importlib.util.spec_from_file_location("m1_parser", AWS / "lambdas" / "flight-parser" / "index.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    return m


class V1AdapterTests(unittest.TestCase):
    def test_request_url_is_exactly_m1s(self):
        m1 = load_m1_parser()
        seen = {}

        def fake_urlopen(req, timeout):
            seen["url"], seen["headers"] = req.full_url, dict(req.header_items())
            return io.BytesIO(json.dumps({"success": True, "data": {}}).encode())

        with mock.patch.object(m1.urllib.request, "urlopen", fake_urlopen):
            m1.fetch_cheapest("TPE", "TYO", "2026-12", TOKEN, "twd")
        c, t = client(lambda u, q: {"success": True, "data": {}}, api="v1")
        c.fetch("TPE", "TYO", "2026-12", "twd")
        self.assertEqual(t.calls[0]["url"], seen["url"])
        self.assertEqual(t.calls[0]["headers"]["X-Access-Token"], seen["headers"]["X-access-token"])
        self.assertEqual(t.calls[0]["headers"]["User-Agent"], seen["headers"]["User-agent"])

    def test_fixture_excludes_expired_offer(self):
        c, _ = client(lambda u, q: fixture("v1_tpe_tyo_2026-12_twd.json"), api="v1")
        r = c.fetch("TPE", "TYO", "2026-12")
        self.assertEqual((r.kind, r.offer.price, r.rejected), (tp.OK, 8300, Counter({"expired": 1})))
        self.assertIsNone(r.offer.origin_airport)   # v1 has no airport fields

    def test_empty_or_other_destination_is_no_data(self):
        for body in ({"success": True, "data": {}}, {"success": True, "data": []},
                     {"success": True, "data": {"NRT": {"0": offer()}}}):
            c, _ = client(lambda u, q, body=body: body, api="v1")
            self.assertEqual(c.fetch("TPE", "TYO", "2026-12").kind, tp.NO_DATA, body)

    def test_m1_message_bodies_are_identical_for_m1_rows(self):
        """Same Travelpayouts answer + same M1 rows -> byte-identical SQS bodies to the M1 parser."""
        resp = v1_body("TYO", offer(9325, airline="LJ"), offer(9900, airline="BR"))
        usd = v1_body("TYO", offer(298, airline="LJ"), currency="usd")
        rows = [{"email": "a@example.com", "route": "TPE-TYO", "plan_name": "tokyo", "origin": "TPE",
                 "destination": "TYO", "target_price": Decimal("10000"), "currency": "TWD"},
                {"email": "b@example.com", "route": "TPE-TYO", "plan_name": "tokyo", "origin": "TPE",
                 "destination": "TYO", "target_price": Decimal("8800"), "currency": "TWD"},
                {"email": "c@example.com", "route": "TPE-TYO", "plan_name": "tokyo", "target_price": Decimal("9325")}]
        m1 = load_m1_parser()
        m1._token, m1._sqs, m1._table = TOKEN, mock.MagicMock(), mock.MagicMock()
        m1._table.scan.return_value = {"Items": rows}
        answers = iter([resp, usd])
        with mock.patch.object(m1.urllib.request, "urlopen",
                               lambda req, timeout: io.BytesIO(json.dumps(next(answers)).encode())), \
             mock.patch.object(m1, "next_month", lambda: "2026-12"), \
             mock.patch.dict("os.environ", {"FARE_QUEUE_URL": "https://sqs.example/q"}):
            m1.handler({"origin": "TPE", "destination": "TYO", "route": "TPE-TYO", "plan": "tokyo"}, None)
        m1_bodies = [c.kwargs["MessageBody"] for c in m1._sqs.send_message.call_args_list]

        sqs = mock.MagicMock()
        out = runner.run_parser({"origin": "TPE", "destination": "TYO", "route": "TPE-TYO", "plan": "tokyo"},
                                settings=settings(fare_api="v1", queue_url="https://sqs.example/q"), catalog=CATALOG,
                                token=TOKEN, reader=effects.ListReader(rows), now=NOW,
                                transport=Transport(lambda u, q: usd if q["currency"] == "usd" else resp),
                                client_factory=lambda name: sqs, log=lambda m: None)
        new_bodies = [c.kwargs["MessageBody"] for c in sqs.send_message.call_args_list]
        self.assertEqual(len(m1_bodies), 2)
        self.assertEqual(new_bodies, m1_bodies)
        self.assertEqual(out["month"], "2026-12")

    def test_m1_wrapper_payload_still_accepted(self):
        """The deployed M1 wrapper sends {origin, destination, route, plan} with no month."""
        item = runner.parse_event({"origin": "TPE", "destination": "SEL", "route": "TPE-SEL", "plan": "seoul"}, CATALOG, NOW)
        self.assertEqual(item, workitems.WorkItem("TPE", "SEL", "2026-12"))


class CompatTests(unittest.TestCase):
    """B5 side-by-side scoring (live comparison needs separate approval)."""

    def pair(self, v1_answer, v3_answer):
        c1, _ = client(v1_answer, api="v1")
        c3, _ = client(v3_answer, api="v3")
        return c1.fetch("TPE", "TYO", "2026-12"), c3.fetch("TPE", "TYO", "2026-12")

    def test_equivalent_answers_agree(self):
        same = offer(8062, origin="TPE", destination="TYO", origin_airport="TSA", destination_airport="HND")
        v1, v3 = self.pair(lambda u, q: v1_body("TYO", offer(8062)), lambda u, q: v3_body(same))
        c = compat.compare(v1, v3)
        self.assertTrue(c["both_ok"] and c["within_tolerance"] and c["same_currency"] and c["both_round_trip"])
        self.assertEqual(c["v3_origin_airport"], "TSA")
        self.assertEqual(v1.offer.message_dict(), v3.offer.message_dict())

    def test_price_disagreement_and_availability(self):
        v1, v3 = self.pair(lambda u, q: v1_body("TYO", offer(8000)), lambda u, q: v3_body(offer(9500)))
        self.assertFalse(compat.compare(v1, v3)["within_tolerance"])
        v1, v3 = self.pair(lambda u, q: v1_body("TYO", offer(8000)), lambda u, q: v3_body())
        self.assertFalse(compat.compare(v1, v3)["v3_at_least_as_available"])
        s = compat.summarize([compat.compare(*self.pair(lambda u, q: v1_body("TYO", offer(8000)),
                                                        lambda u, q: v3_body(offer(8200, origin_airport="TPE"))))] * 3)
        self.assertTrue(s["switch_ok"])
        self.assertEqual(s["v3_origin_airports"], {"TPE": 3})

    def test_fixture_pair(self):
        v1, v3 = self.pair(lambda u, q: fixture("v1_tpe_tyo_2026-12_twd.json"),
                           lambda u, q: fixture("v3_tpe_tyo_2026-12_twd.json"))
        c = compat.compare(v1, v3)
        self.assertEqual((c["v1_price"], c["v3_price"]), (8300, 8062))
        self.assertTrue(c["within_tolerance"])


# ---- matching -----------------------------------------------------------------------------

class MatchingTests(unittest.TestCase):
    def offer(self, price=8062):
        return tp.validate_offer(offer(price), origin="TPE", destination="TYO", month="2026-12", currency="twd",
                                 catalog=CATALOG, now=NOW, source="v3")[0]

    def test_fixture_rows(self):
        rows = fixture("subscriptions.json")
        bodies, skipped = matching.match(rows, workitems.WorkItem("TPE", "TYO", "2026-12"), self.offer(), None, NOW, CATALOG)
        self.assertEqual(sorted(b["email"] for b in bodies), ["fixed-dec@example.com", "m1-tokyo@example.com"])
        self.assertEqual(skipped, {"other_route": 3, "other_month": 1, "paused": 1, "expired": 1, "invalid": 0,
                                   "no_target": 0, "above_target": 1})

    def test_message_shape_is_m1(self):
        bodies, _ = matching.match([row("a@example.com", "TPE-TYO", plan_name="tokyo")],
                                   workitems.WorkItem("TPE", "TYO", "2026-12"), self.offer(), self.offer(262), NOW, CATALOG)
        b = bodies[0]
        self.assertEqual(list(b), ["email", "route", "plan_name", "target_price", "cheapest", "cheapest_usd"])
        self.assertEqual(list(b["cheapest"]), ["price", "currency", "airline", "depart_date", "return_date"])
        self.assertIsInstance(b["target_price"], int)

    def test_bad_targets_never_match(self):
        for tpv in (None, True, "abc", Decimal("NaN"), Decimal("-1"), 0):
            bodies, skipped = matching.match([row("a@example.com", "TPE-TYO", target_price=tpv)],
                                             workitems.WorkItem("TPE", "TYO", "2026-12"), self.offer(), None, NOW, CATALOG)
            self.assertEqual((bodies, skipped["no_target"]), ([], 1), tpv)

    def test_target_equal_to_fare_matches(self):
        bodies, _ = matching.match([row("a@example.com", "TPE-TYO", target_price=Decimal(8062))],
                                   workitems.WorkItem("TPE", "TYO", "2026-12"), self.offer(), None, NOW, CATALOG)
        self.assertEqual(len(bodies), 1)


# ---- DRY_RUN safety -----------------------------------------------------------------------

def refuse(name):
    raise AssertionError(f"AWS client {name!r} created in DRY_RUN")


class DryRunTests(unittest.TestCase):
    def run_parser(self, event, st, logs):
        rows = fixture("subscriptions.json")
        return runner.run_parser(event, settings=st, catalog=CATALOG, token=TOKEN, reader=effects.ListReader(rows),
                                 now=NOW, transport=Transport(lambda u, q: fixture("v3_tpe_tyo_2026-12_twd.json")
                                                              if q["currency"] == "twd" else fixture("v3_tpe_tyo_2026-12_usd.json")),
                                 client_factory=refuse, log=logs.append)

    def test_env_dry_run_creates_no_clients_and_logs_no_addresses_or_token(self):
        logs = []
        out = self.run_parser({"origin": "TPE", "destination": "TYO", "month": "2026-12"},
                              settings(fare_api="v3", dry_run=True), logs)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["matched"], 2)
        blob = json.dumps(out) + "\n".join(logs)
        self.assertNotIn("@", blob)
        self.assertNotIn(TOKEN, blob)

    def test_payload_can_turn_dry_run_on_but_never_off(self):
        logs = []
        out = self.run_parser({"origin": "TPE", "destination": "TYO", "month": "2026-12", "dry_run": True},
                              settings(fare_api="v3", dry_run=False), logs)
        self.assertTrue(out["dry_run"])
        out = self.run_parser({"origin": "TPE", "destination": "TYO", "month": "2026-12", "dry_run": False},
                              settings(fare_api="v3", dry_run=True), logs)
        self.assertTrue(out["dry_run"])

    def test_live_publisher_refused_in_dry_run(self):
        with self.assertRaises(effects.DryRunViolation):
            effects.SqsPublisher("https://sqs.example/q", refuse, dry_run=True)
        inv = effects.LambdaInvoker("flight-parser", refuse, dry_run_payloads=True)
        with self.assertRaises(effects.DryRunViolation):
            inv.invoke({"origin": "TPE", "destination": "TYO"})

    def test_wrapper_dry_run_marks_every_payload(self):
        inv = effects.RecordingInvoker()
        out = runner.run_wrapper(settings=settings(dry_run=True), catalog=CATALOG,
                                 reader=effects.ListReader(fixture("subscriptions.json")), invoker=inv, now=NOW,
                                 log=lambda m: None)
        self.assertEqual(len(inv.payloads), 5)
        self.assertTrue(all(p["dry_run"] is True for p in inv.payloads))
        self.assertTrue(out["dry_run"])
        self.assertNotIn("@", json.dumps(inv.payloads))

    def test_dry_run_flag_parsing_fails_safe(self):
        for raw, expect in (("1", True), ("true", True), ("yes", True), ("maybe", True), ("0", False),
                            ("false", False), ("No", False), ("", False), (None, False)):
            env = {} if raw is None else {"DRY_RUN": raw}
            self.assertEqual(config.Settings.from_env(env).dry_run, expect, raw)
        with self.assertRaises(ValueError):
            config.Settings.from_env({"FARE_API": "v2"})

    def _handler_modules(self, env):
        boto3 = types.ModuleType("boto3")
        created = []

        def client(name, *a, **kw):
            created.append(name)
            m = mock.MagicMock(name=name)
            if name == "ssm":
                m.get_parameter.return_value = {"Parameter": {"Value": TOKEN}}
            return m

        table = mock.MagicMock()
        table.scan.return_value = {"Items": fixture("subscriptions.json")}
        resource = mock.MagicMock()
        resource.return_value.Table.return_value = table
        boto3.client, boto3.resource = client, resource
        mods = {}
        with mock.patch.dict(sys.modules, {"boto3": boto3}), mock.patch.dict("os.environ", env, clear=False):
            for name in ("wrapper", "parser"):
                spec = importlib.util.spec_from_file_location(f"fc_{name}", PKG / f"{name}.py")
                mods[name] = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mods[name])
        return mods, created, table

    def test_handlers_in_dry_run_never_touch_sqs_notification_or_writes(self):
        env = {"DRY_RUN": "1", "FARE_API": "v3", "FARE_QUEUE_URL": "https://sqs.example/q"}
        mods, created, table = self._handler_modules(env)
        answers = lambda req, timeout: io.BytesIO(json.dumps(
            fixture("v3_tpe_tyo_2026-12_twd.json")).encode())
        class FixedNow(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW

        with mock.patch.dict("os.environ", env), \
             mock.patch("urllib.request.urlopen", answers), \
             mock.patch.object(mods["parser"], "datetime", FixedNow), \
             mock.patch.object(mods["wrapper"], "datetime", FixedNow):
            w = mods["wrapper"].handler({}, None)
            p = mods["parser"].handler({"origin": "TPE", "destination": "TYO", "month": "2026-12", "dry_run": True}, None)
        self.assertTrue(w["dry_run"] and p["dry_run"])
        self.assertNotIn("sqs", created)
        self.assertEqual(set(created), {"lambda", "ssm"})   # parser fan-out (dry-run payloads) + token
        for call in table.method_calls:
            self.assertEqual(call[0], "scan")              # reads only: no put/update/delete/batch writes

    def test_wrapper_handler_invokes_only_the_parser(self):
        env = {"DRY_RUN": "1"}
        mods, created, _ = self._handler_modules(env)
        sent = []
        with mock.patch.dict("os.environ", env):
            inv_client = mock.MagicMock()
            inv_client.invoke.side_effect = lambda **kw: sent.append(kw)
            with mock.patch.object(mods["wrapper"].boto3, "client", lambda name: inv_client):
                mods["wrapper"].handler({}, None)
        self.assertTrue(sent)
        self.assertEqual({s["FunctionName"] for s in sent}, {"flight-parser"})
        self.assertTrue(all(json.loads(s["Payload"])["dry_run"] is True for s in sent))


# ---- duplicate requests and status -------------------------------------------------------

class RequestCountTests(unittest.TestCase):
    def test_one_lookup_per_work_item_and_bounded_requests(self):
        rows = [row(f"u{i}@example.com", "TPE-TYO", departure_month="2026-12") for i in range(50)]
        inv = effects.RecordingInvoker()
        runner.run_wrapper(settings=settings(), catalog=CATALOG, reader=effects.ListReader(rows), invoker=inv,
                           now=NOW, log=lambda m: None)
        self.assertEqual(len(inv.payloads), 1)
        for fetch_usd, expected in ((True, 2), (False, 1)):
            t = Transport(lambda u, q: v3_body(offer(8000), currency=q["currency"]))
            out = runner.run_parser(inv.payloads[0], settings=settings(fare_api="v3", dry_run=True, fetch_usd=fetch_usd),
                                    catalog=CATALOG, token=TOKEN, reader=effects.ListReader(rows), now=NOW,
                                    transport=t, client_factory=refuse, log=lambda m: None)
            self.assertEqual((len(t.calls), out["matched"]), (expected, 50))

    def test_no_fare_means_one_request_and_no_scan(self):
        reader = mock.MagicMock()
        t = Transport(lambda u, q: v3_body())
        out = runner.run_parser({"origin": "TPE", "destination": "TYO", "month": "2026-12"},
                                settings=settings(fare_api="v3"), catalog=CATALOG, token=TOKEN, reader=reader, now=NOW,
                                transport=t, client_factory=refuse, log=lambda m: None)
        self.assertEqual((len(t.calls), out["fare_check"]["kind"]), (1, "no_data"))
        reader.rows_for_route.assert_not_called()

    def test_bad_event_makes_no_request(self):
        t = Transport(lambda u, q: v3_body())
        out = runner.run_parser({"origin": "TPE", "destination": "ZZZ"}, settings=settings(), catalog=CATALOG,
                                token=TOKEN, reader=mock.MagicMock(), now=NOW, transport=t, log=lambda m: None)
        self.assertFalse(out["ok"])
        self.assertEqual(t.calls, [])

    def test_max_work_items_cap(self):
        rows = [row(f"u{d}@example.com", f"TPE-{d}", departure_month="2027-01") for d in DESTS[:10]]
        inv, logs = effects.RecordingInvoker(), []
        runner.run_wrapper(settings=settings(max_work_items=4), catalog=CATALOG, reader=effects.ListReader(rows),
                           invoker=inv, now=NOW, log=logs.append)
        self.assertEqual(len(inv.payloads), 4)
        self.assertIn("WARNING", logs[0])


class StatusTests(unittest.TestCase):
    def test_record_and_api_view_for_each_kind(self):
        item = workitems.WorkItem("TPE", "TYO", "2026-12")
        c, _ = client(lambda u, q: fixture("v3_tpe_tyo_2026-12_twd.json"))
        rec = status.record(item, c.fetch("TPE", "TYO", "2026-12"), NOW)
        self.assertEqual(rec["key"], "TPE-TYO#2026-12")
        self.assertEqual((rec["kind"], rec["price"], rec["currency"], rec["depart_date"], rec["return_date"],
                          rec["origin_airport"], rec["destination_airport"], rec["checked_at"]),
                         ("ok", 8062, "TWD", "2026-12-11", "2026-12-18", "TSA", "HND", "2026-11-06T13:00:00Z"))
        self.assertEqual(rec["expires_at"], int((NOW + timedelta(days=3)).timestamp()))
        view = status.api_view(rec)
        expected = json.loads((FIX.parent / "fare_check_api_view.json").read_text(encoding="utf-8"))
        self.assertEqual(view, expected["ok"])
        for kind, answer in (("no_data", lambda u, q: v3_body()), ("error", lambda u, q: tp.HttpError(429))):
            c, _ = client(answer)
            v = status.api_view(status.record(item, c.fetch("TPE", "TYO", "2026-12"), NOW))
            self.assertEqual(v, expected[kind])
        self.assertIsNone(status.api_view(None))
        self.assertNotIn("@", json.dumps(rec))


# ---- packaging and M1 sources untouched ----------------------------------------------------

class PackagingTests(unittest.TestCase):
    def test_zip_is_deterministic_and_complete(self):
        spec = importlib.util.spec_from_file_location("pkg_fc", AWS / "scripts" / "package_fare_check.py")
        pkg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pkg)
        import tempfile
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            r1, r2 = pkg.build(Path(a)), pkg.build(Path(b))
            self.assertEqual(r1["sha256"], r2["sha256"])
            names = zipfile.ZipFile(r1["zip"]).namelist()
        self.assertIn("parser.py", names)
        self.assertIn("wrapper.py", names)
        self.assertIn("farecheck/config/airports.json", names)
        self.assertFalse(any("test" in n or "fixture" in n for n in names))
        self.assertEqual(base64.b64decode(r1["code_sha256"]).hex(), r1["sha256"])

    def test_m1_lambda_sources_unchanged(self):
        """Phase 2A adds a new package; the deployed M1 sources stay byte-identical."""
        import hashlib
        pinned = {"flight-parser": "b4335b9b7ea2ab44dd82301980d9e0f82d17e699090ed6113f21a8c6cd7039b5",
                  "flight-parser-wrapper": "93a9c3aa7742fddcffba1e34781c08859150e86b40761154a9e4a9b72d1cd326",
                  "flight-fare-notification": "f1ccdf0292c03c6a595381ab027e0a893a2d5a5e9487b991496de311c80c969f"}
        for name, digest in pinned.items():
            self.assertEqual(hashlib.sha256((AWS / "lambdas" / name / "index.py").read_bytes()).hexdigest(), digest, name)


if __name__ == "__main__":
    unittest.main()
