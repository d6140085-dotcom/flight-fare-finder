"""B6-1: offline test matrix for the multi-platform search links, shared with the TypeScript tests.

Vectors: aws/tests/vectors/search-links-matrix.json (85 cases; every case fixes its own `now`, so
nothing here depends on the wall clock). Covers TPE/KHH/RMQ x all 12 fixed departure months,
year boundaries, legacy rolling rows, fares outside the subscribed month, return-date window
edges, unsupported codes, missing/invalid markers, disabled and unverified providers and the
labelled general-page fallbacks. The TypeScript twin is src/lib/m14/search-links.matrix.test.ts.

Also checks the departure-month defence end to end in the v2 notification service.
Run: python3 -m unittest aws/tests/test_b6_link_matrix.py
"""
import copy
import json
import sys
import unittest
import urllib.parse
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
AWS = HERE.parent
sys.path.insert(0, str(AWS / "lambdas" / "flight-fare-notification-v2"))
from notify2 import config, months, searchlinks as sl, service  # noqa: E402

MATRIX = json.loads((HERE / "vectors" / "search-links-matrix.json").read_text(encoding="utf-8"))
# The shipped config (aws/config/search-platforms.json) is the email-first MVP (B6 MVP safety):
# Skyscanner and Trip.com enabled:false pending authorization, Aviasales the primary link, Google
# the general fallback. The Skyscanner and Trip.com builder code is kept, so these tests still
# exercise it through CFG: the shipped config with both switched back on and Aviasales back in its
# pre-MVP `source` role, as if authorized. Nothing ships with this fixture. SHIPPED is the real
# config; ShippedConfigTests pins it.
SHIPPED = sl.load_config()
AS_IF_AUTHORIZED = {"skyscanner": {"enabled": True}, "tripcom": {"enabled": True}, "aviasales": {"role": "source"}}


def as_if_authorized(cfg):
    c = copy.deepcopy(cfg)
    for p in c["platforms"]:
        p.update(AS_IF_AUTHORIZED.get(p["id"], {}))
    return c


CFG = as_if_authorized(SHIPPED)
CAT = sl.load_catalog()


def instant(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def config_for(case, base=None):
    cfg = copy.deepcopy(CFG if base is None else base)
    for p in cfg["platforms"]:
        if p["id"] in case.get("disable", []):
            p["enabled"] = False
        if p["id"] in case.get("unverify", []):
            p["verified_codes"]["codes"] = []
    return cfg


def run(case, base=None):
    now = instant(case["now"])
    marker = case["marker"] if "marker" in case else MATRIX["default_marker"]
    return sl.build(case["origin"], case["destination"], case["depart_date"], case["return_date"],
                    months.today(now), catalog=CAT, config=config_for(case, base), marker=marker,
                    month=case.get("month"))


def dates_in(platform, url, dep, ret):
    """True when the URL carries both dates in the platform's own format."""
    d, r = dep.replace("-", ""), ret.replace("-", "")
    if platform == "skyscanner":
        return f"/{d[2:]}/{r[2:]}/" in url
    if platform == "tripcom":
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        return q.get("ddate") == dep and q.get("rdate") == ret and q.get("triptype") == "rt"
    if platform == "aviasales":
        return f"{d[6:]}{d[4:6]}" in url and f"{r[6:]}{r[4:6]}1" in url
    return False


class MatrixShapeTests(unittest.TestCase):
    def test_matrix_covers_the_required_dimensions(self):
        cases = MATRIX["cases"]
        self.assertEqual(len({c["id"] for c in cases}), len(cases), "case ids must be unique")
        fixed = {(c["origin"], c["month"]) for c in cases
                 if c["id"].startswith("A-") and c["destination"] == "TYO"}
        self.assertEqual({o for o, _ in fixed}, {"TPE", "KHH", "RMQ"})
        self.assertEqual(len(fixed), 36, "3 origins x 12 fixed months")
        reasons = {r for c in cases for r in c["expect"]["reasons"]}
        for r in ("fare_outside_subscribed_month", "invalid_subscribed_month", "outside_window",
                  "departure_in_the_past", "unsupported_code", "no_valid_aviasales_marker",
                  "aviasales_affiliate_disabled", "missing_or_malformed_date", "return_before_departure"):
            self.assertIn(r, reasons)
        self.assertTrue(any(c.get("subscription", {}).get("departure_month", "x") is None for c in cases),
                        "legacy rolling rows")
        self.assertEqual(MATRIX["default_marker"], "000000")


class MatrixTests(unittest.TestCase):
    def test_effective_month_resolution_matches_the_vectors(self):
        """`month` is what the shared month rules give for the subscription at `now` (fixed month,
        or next month in Asia/Taipei for a legacy rolling row): the same semantics everywhere."""
        for c in MATRIX["cases"]:
            if "subscription" in c:
                with self.subTest(c["id"]):
                    self.assertEqual(months.effective_month(c["subscription"]["departure_month"], instant(c["now"])),
                                     c["month"])

    def test_every_case_with_the_shipped_config(self):
        """The shipped MVP config gives exactly the expected outcome without Skyscanner and Trip.com."""
        for c in MATRIX["cases"]:
            with self.subTest(c["id"]):
                ls = run(c, base=SHIPPED)
                exp = c["expect"]
                self.assertEqual(ls.compliant, exp["compliant"])
                self.assertEqual(ls.itinerary_valid, exp["itinerary_valid"])
                self.assertEqual(list(ls.reasons), exp["reasons"])
                self.assertEqual([[link.platform, link.kind] for link in ls.links],
                                 [pk for pk in exp["links"] if pk[0] not in ("tripcom", "skyscanner")])

    def test_every_case(self):
        for c in MATRIX["cases"]:
            with self.subTest(c["id"]):
                ls = run(c)
                exp = c["expect"]
                self.assertEqual(ls.compliant, exp["compliant"])
                self.assertEqual(ls.itinerary_valid, exp["itinerary_valid"])
                self.assertEqual(list(ls.reasons), exp["reasons"])
                self.assertEqual([[link.platform, link.kind] for link in ls.links], exp["links"])

    def test_itinerary_links_carry_the_fare_dates_and_month(self):
        for c in MATRIX["cases"]:
            ls = run(c)
            for link in ls.links:
                with self.subTest(c["id"], platform=link.platform):
                    if link.kind == sl.ITINERARY:
                        self.assertTrue(dates_in(link.platform, link.url, c["depart_date"], c["return_date"]))
                        if c.get("month") is not None:
                            self.assertEqual(c["depart_date"][:7], c["month"])
                        self.assertIsNone(link.notice)
                    else:
                        self.assertEqual(link.notice, sl.FALLBACK_NOTICE)
                        self.assertIn("一般搜尋頁", link.label)
                        self.assertNotIn("搜尋此行程", link.label)

    def test_safe_urls_and_marker_only_on_aviasales(self):
        for c in MATRIX["cases"]:
            ls = run(c)
            marker = c["marker"] if "marker" in c else MATRIX["default_marker"]
            for link in ls.links:
                with self.subTest(c["id"], platform=link.platform):
                    sl.check_url(link, marker)
                    q = urllib.parse.parse_qs(urllib.parse.urlsplit(link.url).query)
                    if link.platform == "aviasales":
                        self.assertTrue(link.affiliate)
                        self.assertEqual(q.get("marker"), [marker])
                    else:
                        self.assertNotIn("marker", q)
                        self.assertFalse(link.affiliate)

    def test_a_month_mismatch_never_yields_an_itinerary_link(self):
        mismatches = [c for c in MATRIX["cases"] if "fare_outside_subscribed_month" in c["expect"]["reasons"]]
        self.assertGreaterEqual(len(mismatches), 8)
        for c in mismatches:
            with self.subTest(c["id"]):
                ls = run(c)
                self.assertFalse(ls.itinerary_valid)
                self.assertTrue(all(link.kind == sl.FALLBACK for link in ls.links))
                if ls.links:                       # still compliant: labelled affiliate general page
                    self.assertTrue(ls.compliant)

    def test_month_check_is_keyword_only_and_optional(self):
        """Compatibility: existing positional calls are unchanged; no month = no month check."""
        args = ("TPE", "TYO", "2027-01-11", "2027-01-18", datetime(2026, 11, 23).date())
        unchecked = sl.build(*args, catalog=CAT, config=CFG, marker="000000")
        self.assertTrue(unchecked.itinerary_valid)
        checked = sl.build(*args, catalog=CAT, config=CFG, marker="000000", month="2026-12")
        self.assertEqual(checked.reasons, ("fare_outside_subscribed_month",))
        with self.assertRaises(TypeError):
            sl.build(*args, CAT, CFG, "000000", "2026-12")  # noqa: E501 - positional is rejected

    def test_month_problem_compares_year_and_month(self):
        d = datetime(2026, 12, 10).date()
        self.assertIsNone(sl.month_problem(d, "2026-12"))
        self.assertIsNone(sl.month_problem(d, None))
        self.assertIsNone(sl.month_problem(None, "2026-12"))
        self.assertEqual(sl.month_problem(d, "2027-12"), "fare_outside_subscribed_month")
        self.assertEqual(sl.month_problem(d, "2026-11"), "fare_outside_subscribed_month")
        for bad in ("2026-13", "", "2026-1", "x", 202612):
            self.assertEqual(sl.month_problem(d, bad), "invalid_subscribed_month")


class ServiceMonthDefenceTests(unittest.TestCase):
    """The v2 notification passes the message's resolved month to the builder."""
    NOW = datetime(2026, 11, 23, 4, 0, tzinfo=months.TAIPEI)

    def deps(self):
        return SimpleNamespace(settings=config.Settings.from_env({}), links_config=CFG, catalog=CAT,
                               marker="000000", cities={}, airlines={}, log=lambda *a: None)

    def message(self, month, dep="2026-12-11", ret="2026-12-18"):
        m = {"email": "user@example.com", "route": "TPE-TYO", "plan_name": None, "target_price": 9000,
             "as_of": "2026-11-23T04:00:00Z",
             "cheapest": {"price": 8062, "currency": "TWD", "airline": "BR", "depart_date": dep, "return_date": ret}}
        if month is not None:
            m["month"] = month
        return m

    def test_matching_month_keeps_itinerary_links(self):
        ls = service.search_links(self.message("2026-12"), self.NOW, self.deps())
        self.assertTrue(ls.itinerary_valid)
        self.assertEqual(ls.links[0].kind, sl.ITINERARY)

    def test_fare_in_another_month_gets_general_pages_but_stays_compliant(self):
        ls = service.search_links(self.message("2027-01"), self.NOW, self.deps())
        self.assertEqual(ls.reasons, ("fare_outside_subscribed_month",))
        self.assertTrue(all(link.kind == sl.FALLBACK for link in ls.links))
        self.assertTrue(ls.compliant)
        subject, html, text = service.render(self.message("2027-01"), self.NOW, self.deps(), "t")
        self.assertNotIn("搜尋此行程", text)
        self.assertIn("一般搜尋頁", text)

    def test_m1_shape_message_without_month_is_unchanged(self):
        ls = service.search_links(self.message(None), self.NOW, self.deps())
        self.assertTrue(ls.itinerary_valid)
        bad = service.search_links(self.message(None, dep="bad"), self.NOW, self.deps())
        self.assertEqual(bad.reasons, ("missing_or_malformed_date",))


if __name__ == "__main__":
    unittest.main()
