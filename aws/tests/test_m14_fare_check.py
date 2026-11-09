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


def settings(dry_run=False, **kw):
    return config.Settings(mode="dry_run" if dry_run else "live", **kw)


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
            self.assertEqual(runner.parse_event(item.payload(NOW), CATALOG, NOW), (item, NOW))
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
                         {"route", "origin", "destination", "departure_month", "alerts_paused", "target_price"})
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
        item, as_of = runner.parse_event({"origin": "TPE", "destination": "SEL", "route": "TPE-SEL", "plan": "seoul"},
                                         CATALOG, NOW)
        self.assertEqual((item, as_of), (workitems.WorkItem("TPE", "SEL", "2026-12"), NOW))


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
    ITEM = workitems.WorkItem("TPE", "TYO", "2026-12")

    def offer(self, price=8062):
        return tp.validate_offer(offer(price), origin="TPE", destination="TYO", month="2026-12", currency="twd",
                                 catalog=CATALOG, now=NOW, source="v3")[0]

    def test_fixture_rows(self):
        matched, skipped = matching.match(fixture("subscriptions.json"), self.ITEM, self.offer(), NOW, CATALOG)
        self.assertEqual(sorted(r["email"] for r in matched), ["fixed-dec@example.com", "m1-tokyo@example.com"])
        self.assertEqual(skipped, {"other_route": 3, "other_month": 1, "paused": 1, "expired": 1, "invalid": 0,
                                   "no_target": 0, "above_target": 1})

    def test_per_user_matching_after_one_shared_lookup(self):
        """One lookup for TPE-TYO#2026-12; each user is then judged on their own target and month."""
        rows = [row("a@example.com", "TPE-TYO", target_price=Decimal(9000)),
                row("b@example.com", "TPE-TYO", target_price=Decimal(8000), departure_month="2026-12"),
                row("c@example.com", "TPE-TYO", target_price=Decimal(8062), departure_month="2026-12"),
                row("d@example.com", "TPE-TYO", target_price=Decimal(20000), departure_month="2027-01")]
        matched, _ = matching.match(rows, self.ITEM, self.offer(), NOW, CATALOG)
        self.assertEqual([r["email"] for r in matched], ["a@example.com", "c@example.com"])

    def test_message_shape_is_m1(self):
        r = row("a@example.com", "TPE-TYO", plan_name="tokyo")
        b = matching.message_body(r, self.ITEM, self.offer(), self.offer(262))
        self.assertEqual(list(b), ["email", "route", "plan_name", "target_price", "cheapest", "cheapest_usd"])
        self.assertEqual(list(b["cheapest"]), ["price", "currency", "airline", "depart_date", "return_date"])
        self.assertIsInstance(b["target_price"], int)

    def test_bad_targets_never_match(self):
        for tpv in (None, True, "abc", Decimal("NaN"), Decimal("-1"), 0):
            matched, skipped = matching.match([row("a@example.com", "TPE-TYO", target_price=tpv)],
                                              self.ITEM, self.offer(), NOW, CATALOG)
            self.assertEqual((matched, skipped["no_target"]), ([], 1), tpv)

    def test_target_equal_to_fare_matches_and_rows_without_email_work(self):
        r = {"route": "TPE-TYO", "target_price": Decimal(8062)}         # dry-run rows carry no email
        matched, _ = matching.match([r], self.ITEM, self.offer(), NOW, CATALOG)
        self.assertEqual(matched, [r])


# ---- DRY_RUN safety (fail-closed) ---------------------------------------------------------

def refuse(name):
    raise AssertionError(f"AWS client {name!r} created in DRY_RUN")


class FakeTable:
    """DynamoDB Table stand-in: honours ProjectionExpression and the route filter, records calls."""
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def scan(self, **kw):
        self.calls.append(("scan", kw))
        items = self.rows
        if "FilterExpression" in kw:
            items = [r for r in items if r.get("route") == kw["ExpressionAttributeValues"][":r"]]
        if "ProjectionExpression" in kw:
            fields = set(kw["ExpressionAttributeNames"].values())
            items = [{k: v for k, v in r.items() if k in fields} for r in items]
        return {"Items": items}

    def __getattr__(self, name):          # any write method is recorded, then the test fails on it
        def write(*a, **kw):
            self.calls.append((name, kw))
        return write


class Aws:
    """boto3 stand-in. `lambda` invoke runs the OLD M1 parser (simulated production) with a
    recording SQS client, so any fan-out would visibly produce real notifications."""
    def __init__(self, rows):
        self.clients, self.resources, self.sqs_sent, self.m1_runs = [], [], [], []
        self.table = FakeTable(rows)
        self.module = types.ModuleType("boto3")
        self.module.client = self.client
        self.module.resource = self.resource

    def client(self, name, *a, **kw):
        self.clients.append(name)
        m = mock.MagicMock(name=name)
        if name == "ssm":
            m.get_parameter.return_value = {"Parameter": {"Value": TOKEN}}
        if name == "sqs":
            m.send_message.side_effect = lambda **k: self.sqs_sent.append(k)
        if name == "lambda":
            m.invoke.side_effect = self.run_old_m1_parser
        return m

    def resource(self, name, *a, **kw):
        self.resources.append(name)
        r = mock.MagicMock()
        r.Table.return_value = self.table
        return r

    def run_old_m1_parser(self, FunctionName, Payload, **kw):
        m1 = load_m1_parser()
        m1._token, m1._table = TOKEN, mock.MagicMock()
        m1._table.scan.return_value = {"Items": self.table.rows}
        m1._sqs = mock.MagicMock()
        m1._sqs.send_message.side_effect = lambda **k: self.sqs_sent.append(k)
        self.m1_runs.append(json.loads(Payload))
        body = fixture("v1_tpe_tyo_2026-12_twd.json")
        with mock.patch.object(m1.urllib.request, "urlopen", lambda req, timeout: io.BytesIO(json.dumps(body).encode())), \
             mock.patch.dict("os.environ", {"FARE_QUEUE_URL": "https://sqs.example/q"}):
            m1.handler(json.loads(Payload), None)


class FixedNow(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def load_handlers(aws):
    mods = {}
    with mock.patch.dict(sys.modules, {"boto3": aws.module}):
        for name in ("wrapper", "parser"):
            spec = importlib.util.spec_from_file_location(f"fc_{name}", PKG / f"{name}.py")
            mods[name] = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mods[name])
            mods[name].datetime = FixedNow
    return mods


def fixture_urlopen(req, timeout):
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query))
    api = "v3" if "v3" in req.full_url else "v1"
    month = q.get("depart_date") or q.get("departure_at")
    path = FIX / f"{api}_{q['origin']}_{q['destination']}_{month}_{q['currency']}.json".lower()
    if not path.exists():
        raise OSError("no fixture")
    return io.BytesIO(path.read_bytes())


class DryRunSafetyTests(unittest.TestCase):
    ROWS = fixture("subscriptions.json")

    def run_handler(self, name, env, event=None):
        aws = Aws(self.ROWS)
        mods = load_handlers(aws)
        with mock.patch.dict("os.environ", env, clear=True), mock.patch("urllib.request.urlopen", fixture_urlopen):
            try:
                result, error = mods[name].handler(event or {}, None), None
            except Exception as e:  # noqa: BLE001 - the test inspects it
                result, error = None, e
        return aws, result, error

    def assert_no_production_effects(self, aws):
        self.assertNotIn("lambda", aws.clients, "a Lambda client was created")
        self.assertNotIn("sqs", aws.clients, "an SQS client was created")
        self.assertNotIn("ses", aws.clients)
        self.assertEqual(aws.m1_runs, [], "the production parser was invoked")
        self.assertEqual(aws.sqs_sent, [], "an SQS message was sent")
        self.assertTrue(all(c[0] == "scan" for c in aws.table.calls), aws.table.calls)

    # 1. new scheduler + old production parser
    def test_new_scheduler_in_dry_run_never_reaches_the_old_m1_parser(self):
        aws, out, err = self.run_handler("wrapper", {"FARECHECK_MODE": "dry_run", "FARE_API": "v3"})
        self.assertIsNone(err)
        self.assert_no_production_effects(aws)
        self.assertEqual(aws.clients, ["ssm"])                 # token only
        report = {r["key"]: r for r in out["dry_run_report"]}
        self.assertEqual(report["TPE-TYO#2026-12"]["would_alert"], 2)   # matches exist, nothing is sent
        self.assertEqual(report["TPE-TYO#2026-12"]["would_alert_targets"], [8100, 9000])
        blob = json.dumps(out)
        self.assertNotIn("@", blob)
        self.assertNotIn(TOKEN, blob)

    def test_harness_is_live_the_same_old_parser_does_send_in_live_mode(self):
        """Control: in live mode the stand-in old parser really produces SQS messages, so the dry-run
        test above would have caught a fan-out."""
        aws, out, err = self.run_handler("wrapper", {"FARECHECK_MODE": "live"})
        self.assertIsNone(err)
        self.assertEqual(len(aws.m1_runs), 5)
        self.assertGreater(len(aws.sqs_sent), 0)

    def test_dry_run_scan_never_returns_email(self):
        aws, out, err = self.run_handler("wrapper", {"FARECHECK_MODE": "dry_run"})
        self.assertIsNone(err)
        kinds = [c[1] for c in aws.table.calls]
        self.assertEqual(len(kinds), 1)
        self.assertNotIn("email", kinds[0]["ExpressionAttributeNames"].values())

    # 2. missing configuration, 3. conflicting configuration
    def test_missing_or_conflicting_mode_refuses_before_any_client(self):
        bad_envs = [{}, {"DRY_RUN": "1"}, {"DRY_RUN": "0"}, {"FARECHECK_MODE": ""}, {"FARECHECK_MODE": "maybe"},
                    {"FARECHECK_MODE": "live", "DRY_RUN": "1"}, {"FARECHECK_MODE": "live", "DRY_RUN": "true"},
                    {"FARECHECK_MODE": "dry_run", "DRY_RUN": "0"}, {"FARECHECK_MODE": "dry_run", "DRY_RUN": "false"}]
        for env in bad_envs:
            for name in ("wrapper", "parser"):
                aws, out, err = self.run_handler(name, env, {"origin": "TPE", "destination": "TYO", "month": "2026-12"})
                self.assertIsInstance(err, config.ConfigError, (name, env))
                self.assertEqual((aws.clients, aws.resources, aws.table.calls), ([], [], []), (name, env))

    def test_consistent_configurations_are_accepted(self):
        for env, mode in (({"FARECHECK_MODE": "dry_run"}, "dry_run"), ({"FARECHECK_MODE": "DRY_RUN", "DRY_RUN": "1"}, "dry_run"),
                          ({"FARECHECK_MODE": "live"}, "live"), ({"FARECHECK_MODE": "live", "DRY_RUN": "0"}, "live")):
            self.assertEqual(config.Settings.from_env(env).mode, mode)
        self.assertTrue(config.Settings().dry_run)              # code default is dry, never live

    # 4. downstream Lambda
    def test_dry_run_cannot_build_any_downstream_invoker(self):
        with self.assertRaises(effects.DryRunViolation):
            effects.LambdaInvoker("flight-parser", refuse, dry_run=True)
        with self.assertRaises(effects.DryRunViolation):
            effects.guarded_factory(refuse, {"ssm"})("lambda")
        with self.assertRaises(effects.DryRunViolation):
            runner.run_wrapper(settings=settings(dry_run=True), catalog=CATALOG, reader=effects.ListReader(self.ROWS),
                               now=NOW, invoker=effects.RecordingInvoker(), dry_evaluator=lambda i, r: {})
        live = effects.LambdaInvoker("flight-parser", refuse, dry_run=False)
        with self.assertRaises(effects.DryRunViolation):
            live.invoke({"origin": "TPE", "dry_run": True})

    def test_dry_run_fails_closed_when_its_components_are_missing(self):
        with self.assertRaises(effects.DryRunViolation):          # no evaluator: refuse, never fall back to live
            runner.run_wrapper(settings=settings(dry_run=True), catalog=CATALOG,
                               reader=effects.ListReader(self.ROWS), now=NOW)
        with self.assertRaises(effects.DryRunViolation):
            runner.DryRunEvaluator(settings=settings(), catalog=CATALOG, client=None, as_of=NOW)
        aws = Aws(self.ROWS)                                         # token unavailable: error, nothing else
        aws.module.client = lambda name, *a, **k: (aws.clients.append(name), (_ for _ in ()).throw(RuntimeError("ssm down")))[1]
        mods = load_handlers(aws)
        with mock.patch.dict("os.environ", {"FARECHECK_MODE": "dry_run"}, clear=True):
            with self.assertRaises(RuntimeError):
                mods["wrapper"].handler({}, None)
        self.assert_no_production_effects(aws)

    # 5. SQS
    def test_dry_run_cannot_connect_to_sqs(self):
        with self.assertRaises(effects.DryRunViolation):
            effects.SqsPublisher("https://sqs.example/q", refuse, dry_run=True)
        with self.assertRaises(effects.DryRunViolation):
            effects.guarded_factory(refuse, {"ssm"})("sqs")
        for env, event in (({"FARECHECK_MODE": "dry_run", "FARE_API": "v3"}, {}),
                           ({"FARECHECK_MODE": "live", "FARE_API": "v3", "FARE_QUEUE_URL": "https://sqs.example/q"},
                            {"dry_run": True})):
            ev = dict({"origin": "TPE", "destination": "TYO", "month": "2026-12"}, **event)
            aws, out, err = self.run_handler("parser", env, ev)
            self.assertIsNone(err)
            self.assertTrue(out["dry_run"])
            self.assertEqual(out["matched"], 2)
            self.assert_no_production_effects(aws)

    def test_parser_client_guard_alone_blocks_sqs_in_dry_run(self):
        """Second layer: even if the publisher logic were wrong, the parser's client factory refuses SQS."""
        seen = {}

        def probe(event, *, client_factory, **kw):
            for name in ("sqs", "lambda", "ses"):
                try:
                    client_factory(name)
                    seen[name] = "created"
                except effects.DryRunViolation:
                    seen[name] = "refused"
            return {"ok": True}

        aws = Aws(self.ROWS)
        mods = load_handlers(aws)
        for env, event in (({"FARECHECK_MODE": "dry_run"}, {}), ({"FARECHECK_MODE": "live"}, {"dry_run": True})):
            seen.clear()
            with mock.patch.dict("os.environ", env, clear=True), mock.patch.object(mods["parser"].runner, "run_parser", probe):
                mods["parser"]._token = None
                mods["parser"].handler(dict({"origin": "TPE", "destination": "TYO"}, **event), None)
            self.assertEqual(seen, {"sqs": "refused", "lambda": "refused", "ses": "refused"}, env)
        self.assert_no_production_effects(aws)

    def test_payload_can_turn_dry_run_on_but_never_off(self):
        aws, out, err = self.run_handler("parser", {"FARECHECK_MODE": "dry_run", "FARE_API": "v3"},
                                         {"origin": "TPE", "destination": "TYO", "month": "2026-12", "dry_run": False})
        self.assertTrue(out["dry_run"])
        self.assert_no_production_effects(aws)

    # 6. DynamoDB writes
    def test_subscriptions_table_is_read_only_in_every_mode(self):
        t = effects.ReadOnlyTable(FakeTable([]))
        for method in ("put_item", "update_item", "delete_item", "batch_writer", "transact_write_items", "delete"):
            with self.assertRaises(effects.ReadOnlyViolation, msg=method):
                getattr(t, method)
        self.assertEqual(t.scan(), {"Items": []})
        for env in ({"FARECHECK_MODE": "dry_run"}, {"FARECHECK_MODE": "live"}):
            aws, _, err = self.run_handler("wrapper", env)
            self.assertIsNone(err)
            self.assertTrue(all(c[0] == "scan" for c in aws.table.calls))

    def test_live_parser_is_the_only_path_that_sends(self):
        """Live parser (control): SQS is reachable only here, and only with an explicit live mode."""
        aws, out, err = self.run_handler("parser", {"FARECHECK_MODE": "live", "FARE_API": "v3",
                                                    "FARE_QUEUE_URL": "https://sqs.example/q"},
                                         {"origin": "TPE", "destination": "TYO", "month": "2026-12",
                                          "as_of": "2026-11-06T13:00:00Z"})
        self.assertIsNone(err)
        self.assertEqual(len(aws.sqs_sent), 2)
        self.assertEqual(sorted(aws.clients), ["sqs", "ssm"])


# ---- month boundary (Asia/Taipei) ----------------------------------------------------------

BEFORE = datetime(2026, 11, 30, 15, 59, 59, tzinfo=timezone.utc)   # 2026-11-30 23:59:59 Taipei
AFTER = datetime(2026, 11, 30, 16, 0, 1, tzinfo=timezone.utc)      # 2026-12-01 00:00:01 Taipei
BOUNDARY_ROWS = [
    {"email": "rolling@example.com", "route": "TPE-TYO", "target_price": Decimal(20000)},
    {"email": "nov@example.com", "route": "TPE-TYO", "departure_month": "2026-11", "target_price": Decimal(20000)},
    {"email": "dec@example.com", "route": "TPE-TYO", "departure_month": "2026-12", "target_price": Decimal(20000)},
    {"email": "jan@example.com", "route": "TPE-TYO", "departure_month": "2027-01", "target_price": Decimal(20000)},
]


def dec_offer_body(u, q):
    m = q.get("departure_at") or q.get("depart_date")
    d = {"2026-12": ("2026-12-11T07:30:00+08:00", "2026-12-18T19:00:00+09:00"),
         "2027-01": ("2027-01-11T07:30:00+08:00", "2027-01-18T19:00:00+09:00"),
         "2026-11": ("2026-11-12T07:30:00+08:00", "2026-11-18T19:00:00+09:00")}[m]
    return v3_body(offer(8000, dep=d[0], ret=d[1]), currency=q["currency"])


class MonthBoundaryTests(unittest.TestCase):
    def plan(self, at):
        return [i.key for i in workitems.build(BOUNDARY_ROWS, at, CATALOG).items]

    def test_2026_11_30_235959_taipei(self):
        self.assertEqual(self.plan(BEFORE), ["TPE-TYO#2026-11", "TPE-TYO#2026-12", "TPE-TYO#2027-01"])

    def test_2026_12_01_000001_taipei(self):
        # rolling moves to 2027-01 (merged with the fixed January row); November has expired
        self.assertEqual(self.plan(AFTER), ["TPE-TYO#2026-12", "TPE-TYO#2027-01"])

    def run_parser_at(self, payload, now):
        return runner.run_parser(payload, settings=settings(fare_api="v3", dry_run=True, fetch_usd=False), catalog=CATALOG,
                                 token=TOKEN, reader=effects.ListReader(BOUNDARY_ROWS), now=now,
                                 transport=Transport(dec_offer_body), log=lambda m: None)

    def test_scheduler_run_crossing_midnight_keeps_the_wrapper_month(self):
        """Wrapper at 23:59:59, parser at 00:00:01: the rolling row still matches December."""
        inv = effects.RecordingInvoker()
        runner.run_wrapper(settings=settings(), catalog=CATALOG, reader=effects.ListReader(BOUNDARY_ROWS), now=BEFORE,
                           invoker=inv, log=lambda m: None)
        dec = next(p for p in inv.payloads if p["month"] == "2026-12")
        self.assertEqual(dec["as_of"], "2026-11-30T15:59:59Z")
        out = self.run_parser_at(dec, AFTER)
        self.assertEqual(out["matched"], 2)          # rolling + fixed December, resolved at as_of
        self.assertEqual(out["as_of"], "2026-11-30T15:59:59Z")

    def test_without_as_of_the_race_would_skip_the_rolling_row(self):
        """Documents the bug the as_of fixes: the parser's own clock says the rolling row is January."""
        out = self.run_parser_at({"origin": "TPE", "destination": "TYO", "month": "2026-12"}, AFTER)
        self.assertEqual(out["matched"], 1)                 # only the fixed December row
        self.assertEqual(out["skipped"]["other_month"], 2)  # the rolling row (now January) and January

    def test_november_item_after_midnight_alerts_nobody(self):
        inv = effects.RecordingInvoker()
        runner.run_wrapper(settings=settings(), catalog=CATALOG, reader=effects.ListReader(BOUNDARY_ROWS), now=BEFORE,
                           invoker=inv, log=lambda m: None)
        nov = next(p for p in inv.payloads if p["month"] == "2026-11")
        out = self.run_parser_at(nov, AFTER)          # every November departure has now passed
        self.assertEqual((out["fare_check"]["kind"], out["matched"]), ("no_data", 0))

    def test_in_process_dry_run_uses_one_as_of(self):
        t = Transport(dec_offer_body)
        late_client = tp.Client(TOKEN, CATALOG, AFTER, api="v3", transport=t, log=lambda m: None)
        ev = runner.DryRunEvaluator(settings=settings(dry_run=True, fetch_usd=False), catalog=CATALOG,
                                    client=late_client, as_of=BEFORE, log=lambda m: None)
        out = runner.run_wrapper(settings=settings(dry_run=True), catalog=CATALOG,
                                 reader=effects.ListReader(BOUNDARY_ROWS), now=BEFORE, dry_evaluator=ev, log=lambda m: None)
        dec = next(r for r in out["dry_run_report"] if r["key"] == "TPE-TYO#2026-12")
        self.assertEqual(dec["would_alert"], 2)

    def test_consecutive_runs_check_each_month_once_without_gaps(self):
        months_checked = []
        for at in (BEFORE - timedelta(minutes=30), BEFORE, AFTER, AFTER + timedelta(minutes=30)):
            inv = effects.RecordingInvoker()
            runner.run_wrapper(settings=settings(), catalog=CATALOG, reader=effects.ListReader(BOUNDARY_ROWS[:1]),
                               now=at, invoker=inv, log=lambda m: None)
            self.assertEqual(len(inv.payloads), 1)    # never two months for one rolling row in one run
            months_checked.append(inv.payloads[0]["month"])
        self.assertEqual(months_checked, ["2026-12", "2026-12", "2027-01", "2027-01"])

    def test_as_of_is_validated(self):
        base = {"origin": "TPE", "destination": "TYO", "month": "2026-12"}
        for bad in ("2026-11-06 13:00:00", "yesterday", 12345, "2026-11-04T12:00:00Z", "2026-11-06T13:30:00Z"):
            with self.assertRaises(ValueError, msg=bad):
                runner.parse_event(dict(base, as_of=bad), CATALOG, NOW)
        with self.assertRaises(ValueError):          # month already over at as_of
            runner.parse_event(dict(base, month="2026-10", as_of="2026-11-06T12:00:00Z"), CATALOG, NOW)
        item, as_of = runner.parse_event(dict(base, as_of="2026-11-06T12:00:00Z"), CATALOG, NOW)
        self.assertEqual(as_of, NOW - timedelta(hours=1))


# ---- bounded rotation (no starvation) -----------------------------------------------------

def many_rows(n_routes_months):
    months_ = ["2027-01", "2027-02", "2027-03", "2027-04", "2027-05"]
    combos = [(o, d, m) for m in months_ for o in ORIGINS for d in DESTS][:n_routes_months]
    return [{"route": f"{o}-{d}", "departure_month": m, "target_price": 9000} for o, d, m in combos]


class RotationTests(unittest.TestCase):
    def run_at(self, rows, at, **kw):
        inv = effects.RecordingInvoker()
        out = runner.run_wrapper(settings=settings(**kw), catalog=CATALOG, reader=effects.ListReader(rows), now=at,
                                 invoker=inv, log=lambda m: None)
        return [p["route"] + "#" + p["month"] for p in inv.payloads], out

    def test_1200_items_cap_500_every_item_within_three_scheduled_runs(self):
        rows = many_rows(1200)
        all_keys = {i.key for i in workitems.build(rows, NOW, CATALOG).items}
        self.assertEqual(len(all_keys), 1200)
        seen, runs = [], []
        for k in range(3):
            keys, out = self.run_at(rows, NOW + timedelta(minutes=30 * k))
            self.assertLessEqual(len(keys), 500)
            self.assertEqual(out["window"]["windows"], 3)
            runs.append(set(keys))
            seen.extend(keys)
        self.assertEqual(set(seen), all_keys)
        self.assertEqual(len(seen), len(set(seen)))   # no item twice within the cycle
        self.assertNotEqual(runs[0], runs[1])

    def test_window_sizes_and_cycle_repeat(self):
        rows = many_rows(1425)                         # 3 origins x 95 destinations x 5 months
        sizes = [len(self.run_at(rows, NOW + timedelta(minutes=30 * k))[0]) for k in range(6)]
        self.assertEqual(sorted(sizes[:3]), [425, 500, 500])
        self.assertEqual(self.run_at(rows, NOW)[0], self.run_at(rows, NOW + timedelta(minutes=90))[0])

    def test_any_start_time_covers_everything_in_consecutive_runs(self):
        rows = many_rows(1001)
        all_keys = {i.key for i in workitems.build(rows, NOW, CATALOG).items}
        for start in range(0, 24 * 60, 37):
            t0 = NOW + timedelta(minutes=start)
            seen = set()
            for k in range(3):
                seen |= set(self.run_at(rows, t0 + timedelta(minutes=30 * k))[0])
            self.assertEqual(seen, all_keys, start)

    def test_dry_run_cap_rotates_too(self):
        rows = many_rows(120)
        ev = lambda item, r: {"key": item.key}
        seen = []
        for k in range(3):
            out = runner.run_wrapper(settings=settings(dry_run=True), catalog=CATALOG, reader=effects.ListReader(rows),
                                     now=NOW + timedelta(minutes=30 * k), dry_evaluator=ev, log=lambda m: None)
            self.assertLessEqual(len(out["dry_run_report"]), 50)
            seen += [r["key"] for r in out["dry_run_report"]]
        self.assertEqual(len(set(seen)), 120)

    def test_under_cap_everything_every_run(self):
        keys, out = self.run_at(many_rows(40), NOW)
        self.assertEqual((len(keys), out["window"]["windows"]), (40, 1))


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
        self.assertIn(len(inv.payloads), (4, 2))       # windows of 4, 4, 2
        self.assertIn("WARNING", logs[0])
        self.assertIn("checked once per 3 scheduled runs", logs[0])


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
