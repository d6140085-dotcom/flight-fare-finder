"""Phase 2B-2: multi-platform flight search links (docs/m1.4-multi-platform-search-links.md §7.1).

Offline: pure builder and renderer tests with mocked fare data, plus a few end-to-end runs of the
v2 notification service on the local DynamoDB of test_m14_notify_dedup (fake Resend, no email).
The marker is an obvious test value ("000000"), never a real Travelpayouts Partner ID.
Run: python3 -m unittest aws/tests/test_m14_search_links.py
"""
import copy
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
AWS = HERE.parent
PKG = AWS / "lambdas" / "flight-fare-notification-v2"
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(HERE))
from notify2 import config, mailer, searchlinks as sl, service  # noqa: E402

TODAY = date(2026, 11, 11)
MARKER = "000000"
EMAIL = "user@example.com"
CFG = sl.load_config()
CAT = sl.load_catalog()
CATALOG = json.loads((AWS / "config" / "airports.json").read_text(encoding="utf-8"))
VECTORS = json.loads((HERE / "vectors" / "search-links.json").read_text(encoding="utf-8"))
CITIES = {"TPE": "台北", "KHH": "高雄", "RMQ": "台中", "TYO": "東京", "OKA": "沖繩"}
CJK = re.compile(r"[一-鿿]")


def build(o="TPE", d="TYO", dep="2026-12-11", ret="2026-12-18", today=TODAY, marker=MARKER, cfg=None):
    return sl.build(o, d, dep, ret, today, catalog=CAT, config=cfg or CFG, marker=marker)


def by(ls):
    return {link.platform: link for link in ls.links}


def fare(route="TPE-TYO", price=8062, target=9000, dep="2026-12-11T08:00:00+08:00", ret="2026-12-18T19:00:00+09:00",
         usd=None, airline="BR"):
    """Mocked notification message (the shape the parser sends)."""
    m = {"email": EMAIL, "route": route, "plan_name": None, "target_price": target,
         "cheapest": {"price": price, "currency": "TWD", "airline": airline, "depart_date": dep, "return_date": ret}}
    if usd:
        m["cheapest_usd"] = {"price": usd}
    return m


def cfg_with(**changes):
    c = copy.deepcopy(CFG)
    for pid, patch in changes.items():
        p = next(x for x in c["platforms"] if x["id"] == pid)
        p.update(patch)
    return c


class GoldenVectorTests(unittest.TestCase):
    """L01: the spec §4.2 URLs, exactly."""

    def test_spec_example_verbatim(self):
        links = by(build())
        self.assertEqual(links["skyscanner"].url, "https://www.skyscanner.com.tw/transport/flights/tpe/tyo/261211/261218/"
                         "?adultsv2=1&cabinclass=economy&market=TW&locale=zh-TW&currency=TWD")
        self.assertEqual(links["tripcom"].url, "https://tw.trip.com/flights/showfarefirst?dcity=tpe&acity=tyo"
                         "&ddate=2026-12-11&rdate=2026-12-18&triptype=rt&class=y&quantity=1&locale=zh-TW&curr=TWD")
        self.assertEqual(links["aviasales"].url, "https://www.aviasales.com/search/TPE1112TYO18121?marker=000000")
        self.assertEqual(links["google"].url, "https://www.google.com/travel/flights?hl=zh-TW")

    def test_shared_vector_file(self):
        self.assertEqual(VECTORS["marker"], MARKER)
        today = date.fromisoformat(VECTORS["today"])
        for case in VECTORS["cases"]:
            ls = build(case["origin"], case["destination"], case["depart_date"], case["return_date"], today=today)
            got = [{"platform": x.platform, "kind": x.kind, "url": x.url, "label": x.label} for x in ls.links]
            self.assertEqual(got, case["links"], case)

    def test_each_origin(self):
        for o in ("TPE", "KHH", "RMQ"):
            links = by(build(o, "OKA"))
            self.assertEqual(links["aviasales"].url, f"https://www.aviasales.com/search/{o}1112OKA18121?marker=000000")
            self.assertEqual(links["tripcom"].kind, "itinerary")
            self.assertEqual(links["skyscanner"].kind, "fallback" if o == "KHH" else "itinerary")   # KHH unverified


class AirportAndCityCodeTests(unittest.TestCase):
    """L02 and the verified-codes rule (§4.3): every catalog destination from every origin."""

    def test_every_destination_and_origin(self):
        origins = [o["code"] for o in CATALOG["origins"]]
        dests = CATALOG["destinations"]
        self.assertEqual(len(dests), 95)
        for o in origins:
            for dest in dests:
                ls = build(o, dest["code"])
                links = by(ls)
                self.assertEqual(list(links), ["skyscanner", "tripcom", "aviasales", "google"])
                for p in ("skyscanner", "tripcom"):
                    vc = next(x for x in CFG["platforms"] if x["id"] == p)["verified_codes"]["codes"]
                    want = "itinerary" if o in vc and dest["code"] in vc else "fallback"
                    self.assertEqual(links[p].kind, want, (o, dest["code"], p))
                    if want == "fallback":
                        self.assertEqual(links[p].url, sl.FALLBACK_URLS[p])
                self.assertEqual(links["aviasales"].kind, "itinerary")
                self.assertIn(f"/search/{o}1112{dest['code']}18121", links["aviasales"].url)
                for link in ls.links:
                    self.assertEqual(urllib.parse.urlsplit(link.url).netloc, sl.HOSTS[link.platform])

    def test_city_codes(self):
        cities = [d["code"] for d in CATALOG["destinations"] if d["type"] == "city"]
        self.assertEqual(len(cities), 26)
        self.assertIn("tyo", by(build("TPE", "TYO"))["skyscanner"].url)            # verified city code
        for code in ("LON", "PAR", "NYC", "SEL", "OSA", "BJS"):                    # unverified city codes
            links = by(build("TPE", code))
            self.assertEqual(links["skyscanner"].kind, "fallback", code)
            self.assertEqual(links["tripcom"].kind, "fallback", code)
            self.assertEqual(links["aviasales"].kind, "itinerary", code)
            self.assertIn(sl.FALLBACK_NOTICE, links["skyscanner"].notice)

    def test_origin_tpe_is_the_city_code_and_tsa_is_not_an_origin(self):
        self.assertIn("dcity=tpe", by(build("TPE", "OKA"))["tripcom"].url)
        self.assertEqual(build("TSA", "OKA").links, ())

    def test_verified_code_override(self):
        c = cfg_with(skyscanner={"verified_codes": {"codes": ["TPE", "OSA"]}, "code_overrides": {"OSA": "osaa"}})
        sl.validate_config(c)
        self.assertIn("/tpe/osaa/", by(build("TPE", "OSA", cfg=c))["skyscanner"].url)
        self.assertIn("/search/TPE1112OSA18121", by(build("TPE", "OSA", cfg=c))["aviasales"].url)   # Aviasales unaffected


class RoundTripTests(unittest.TestCase):
    """L03, and the date window of §4.1."""

    def test_both_dates_in_each_format(self):
        links = by(build(dep="2027-02-03T08:00:00+08:00", ret="2027-02-10T19:00:00+09:00"))
        self.assertIn("/270203/270210/", links["skyscanner"].url)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(links["tripcom"].url).query))
        self.assertEqual((q["ddate"], q["rdate"], q["triptype"]), ("2027-02-03", "2027-02-10", "rt"))
        self.assertIn("TPE0302TYO10021", links["aviasales"].url)

    def test_same_day_return_and_year_boundary(self):
        self.assertIn("/261231/261231/", by(build(dep="2026-12-31", ret="2026-12-31"))["skyscanner"].url)
        self.assertIn("TPE3112TYO04011", by(build(dep="2026-12-31", ret="2027-01-04"))["aviasales"].url)

    def test_window_edges(self):
        self.assertTrue(build(dep="2026-11-11", ret="2027-12-11").itinerary_valid)          # today .. today+13 months
        self.assertEqual(build(dep="2026-11-11", ret="2027-12-12").reasons, ("outside_window",))
        self.assertEqual(sl.window_end(date(2027, 1, 31)), date(2028, 2, 29))              # month-end clamp

    def test_aviasales_never_gets_an_ambiguous_year(self):
        # DDMM has no year: 2027-11-20 would read as 20 Nov 2026 (already past) -> fallback link
        links = by(build(dep="2027-10-01", ret="2027-11-20"))
        self.assertEqual(links["aviasales"].kind, "fallback")
        self.assertEqual(links["aviasales"].url, "https://www.aviasales.com/?marker=000000")
        self.assertEqual(links["skyscanner"].kind, "itinerary")
        self.assertEqual(by(build(dep="2027-10-01", ret="2027-11-10"))["aviasales"].kind, "itinerary")


class LocalizationTests(unittest.TestCase):
    """L04: zh-TW and TWD where supported; every label is Traditional Chinese."""

    def test_locale_and_currency(self):
        links = by(build())
        q = lambda p: dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(links[p].url).query))
        self.assertEqual({k: q("skyscanner")[k] for k in ("locale", "currency", "market")},
                         {"locale": "zh-TW", "currency": "TWD", "market": "TW"})
        self.assertEqual((q("tripcom")["locale"], q("tripcom")["curr"]), ("zh-TW", "TWD"))
        self.assertEqual(q("google"), {"hl": "zh-TW"})
        self.assertEqual(q("aviasales"), {"marker": MARKER})                # no undocumented locale/currency

    def test_labels(self):
        labels = {x.platform: x.label for x in build().links}
        self.assertEqual(labels, {"skyscanner": "在 Skyscanner 搜尋此行程", "tripcom": "在 Trip.com 搜尋此行程",
                                  "aviasales": "在 Aviasales 搜尋此行程（票價來源・聯盟連結・英文介面）",
                                  "google": "前往 Google 航班 一般搜尋頁（未帶入行程，請自行輸入並確認航線與日期）"})
        for ls in (build(), build(d="LON"), build(dep=None)):
            for link in ls.links:
                self.assertTrue(CJK.search(link.label), link.label)
                for simplified in ("搜寻", "来源", "链接", "联盟", "需手动", "航线"):
                    self.assertNotIn(simplified, link.label)


class InvalidInputTests(unittest.TestCase):
    """L05 (dates) and L06 (codes)."""

    def test_bad_dates_give_fallbacks_never_one_way(self):
        cases = {(None, "2026-12-18"): "missing_or_malformed_date", ("2026-12-11", None): "missing_or_malformed_date",
                 ("2026-12-11", ""): "missing_or_malformed_date", ("2026-13-01", "2026-12-18"): "missing_or_malformed_date",
                 ("2026-02-30", "2026-12-18"): "missing_or_malformed_date", ("20261211", "20261218"): "missing_or_malformed_date",
                 ("2026-12-11", "2026-12-18junk"): "missing_or_malformed_date", (20261211, 20261218): "missing_or_malformed_date",
                 ("2026-11-10", "2026-12-18"): "departure_in_the_past",
                 ("2026-12-11", "2027-12-12"): "outside_window", ("2026-12-18", "2026-12-11"): "return_before_departure"}
        for (dep, ret), reason in cases.items():
            ls = build(dep=dep, ret=ret)
            self.assertEqual(ls.reasons, (reason,), (dep, ret))
            self.assertFalse(ls.itinerary_valid)
            for link in ls.links:
                self.assertEqual(link.kind, "fallback", (dep, ret, link.platform))
                self.assertTrue(link.notice)
                self.assertNotIn("search/", link.url)
                self.assertNotIn("ddate", link.url)
                self.assertNotIn("transport/flights", link.url)

    def test_unsupported_codes_give_no_links_and_no_exception(self):
        for o, d in (("ZZZ", "TYO"), ("TPE", "ZZZ"), ("tpe", "TYO"), ("TPE", "tyo"), ("TSA", "TYO"), ("", "TYO"),
                     ("TPE", ""), ("TPEX", "TYO"), ("TPE", "TYOO"), ('"><script>', "TYO"), ("TPE", "T<O"),
                     (None, "TYO"), ("TPE", None), (123, "TYO"), ("TPE", "TYO\n"), ("TPE", "ＴＹＯ")):
            ls = build(o, d)
            self.assertEqual((ls.links, ls.reasons), ((), ("unsupported_code",)), (o, d))


class FallbackTests(unittest.TestCase):
    """L07 and §6."""

    def test_disabled_platform_is_omitted_others_unaffected(self):
        for pid in ("skyscanner", "tripcom", "google"):
            c = cfg_with(**{pid: {"enabled": False}})
            sl.validate_config(c)
            links = by(build(cfg=c))
            self.assertNotIn(pid, links)
            self.assertEqual({p: links[p].url for p in links}, {p: x.url for p, x in by(build()).items() if p != pid})

    def test_unverified_platform_falls_back(self):
        c = cfg_with(tripcom={"verified_codes": {"codes": []}})          # simulated broken URL format
        links = by(build(cfg=c))
        self.assertEqual((links["tripcom"].kind, links["tripcom"].url), ("fallback", "https://tw.trip.com/flights/"))
        self.assertEqual(links["tripcom"].label, "前往 Trip.com 一般搜尋頁（未帶入行程，請自行輸入並確認航線與日期）")
        self.assertEqual(links["skyscanner"].kind, "itinerary")

    def test_order_comes_from_config(self):
        c = cfg_with(skyscanner={"order": 2}, tripcom={"order": 1})
        self.assertEqual([x.platform for x in build(cfg=c).links], ["tripcom", "skyscanner", "aviasales", "google"])

    def test_general_search_pages_are_labelled_as_such(self):
        """Every fallback says it is a general search page, that nothing was carried over and that
        the user must enter the route and dates; it never claims the itinerary or a price."""
        for ls in (build(dep=None), build(d="LON"), build(dep="2027-10-01", ret="2027-11-20"), build()):
            for link in ls.links:
                if link.kind == "fallback":
                    self.assertIn("一般搜尋頁", link.label)
                    self.assertIn("未帶入行程", link.label)
                    self.assertIn("請自行輸入並確認航線與日期", link.label)
                    self.assertNotIn("搜尋此行程", link.label)
                    self.assertTrue(link.label.startswith("前往 "))
                    self.assertEqual(link.notice, sl.FALLBACK_NOTICE)
                    self.assertEqual(urllib.parse.urlsplit(link.url).path in ("/", "/flights/", "/travel/flights"), True)
                else:
                    self.assertIn("搜尋此行程", link.label)
                for word in ("同價", "相同價格", "保證", "此價格", "NT$"):
                    self.assertNotIn(word, link.label)
        av = by(build(dep=None))["aviasales"]
        self.assertEqual(av.label, "前往 Aviasales 一般搜尋頁（票價來源・聯盟連結・英文介面；未帶入行程，請自行輸入並確認航線與日期）")
        self.assertEqual(av.url, "https://www.aviasales.com/?marker=000000")

    def test_missing_or_invalid_marker_gives_no_links_at_all(self):
        """Fail closed: never a plain Aviasales link, never third-party links without the affiliate link."""
        for marker in (None, "", "abc", "12", "1234567.sub", "1234567&x=1", " 123456", "123456\n", "1" * 13,
                       123456, "１２３４５６", "eyJhbGciOi", "user@example.com"):
            for dep, ret in (("2026-12-11", "2026-12-18"), (None, None)):
                ls = build(marker=marker, dep=dep, ret=ret)
                self.assertEqual(ls.links, (), marker)
                self.assertFalse(ls.compliant)
                self.assertIsNone(ls.aviasales)
                self.assertTrue(ls.third_party_suppressed)
                self.assertIn("no_valid_aviasales_marker", ls.reasons)
        self.assertTrue(build().compliant)

    def test_no_third_party_links_without_the_affiliate_link(self):
        """Even if config validation were bypassed, a LinkSet with other platforms always has the
        labelled Aviasales affiliate link, or is empty."""
        tampered = cfg_with(aviasales={"affiliate": {"enabled": False}})          # invalid, not validated here
        ls = build(cfg=tampered)
        self.assertEqual((ls.links, ls.compliant), ((), False))
        self.assertIn("aviasales_affiliate_disabled", ls.reasons)
        ls = build(cfg=cfg_with(aviasales={"enabled": False}))
        self.assertEqual((ls.links, ls.compliant), ((), False))
        for o in ("TPE", "KHH", "RMQ"):
            for dest in CATALOG["destinations"]:
                for marker in (MARKER, None):
                    ls = build(o, dest["code"], marker=marker)
                    if ls.links:
                        self.assertTrue(ls.compliant)
                        self.assertEqual(sum(1 for x in ls.links if x.platform == "aviasales" and x.affiliate), 1)


class PrivacyAndAffiliateTests(unittest.TestCase):
    """L08, L09, L10."""

    def all_link_sets(self):
        for o in ("TPE", "KHH", "RMQ"):
            for d in ("TYO", "OKA", "LON", "NYC", "BKK"):
                for dep, ret in (("2026-12-11", "2026-12-18"), (None, None), ("2027-10-01", "2027-11-20")):
                    for marker in (MARKER, None):
                        ls = build(o, d, dep, ret, marker=marker)
                        self.assertEqual(bool(ls.links), marker == MARKER)
                        yield ls

    def test_urls_carry_nothing_personal_or_secret(self):
        digest = hashlib.sha256(EMAIL.encode()).hexdigest()
        for ls in self.all_link_sets():
            for link in ls.links:
                u = urllib.parse.urlsplit(link.url)
                self.assertEqual(u.scheme, "https")
                self.assertEqual(u.netloc, sl.HOSTS[link.platform])
                for bad in ("@", "%40", EMAIL, digest[:8], "eyJ", "token", "secret", "user", "email", "sub=",
                            "tp_token", "jwt", "password", "key="):
                    self.assertNotIn(bad, link.url.lower() if bad.islower() else link.url)
                keys = [k for k, _ in urllib.parse.parse_qsl(u.query, keep_blank_values=True)]
                self.assertTrue(set(keys) <= sl.ALLOWED_QUERY[link.platform], link.url)
                self.assertFalse(any(k.lower().startswith("utm_") for k in keys))

    def test_marker_only_on_aviasales_exactly_once(self):
        for ls in self.all_link_sets():
            for link in ls.links:
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(link.url).query)
                if link.platform == "aviasales" and link.affiliate:
                    self.assertEqual(q.get("marker"), [MARKER])
                else:
                    self.assertNotIn("marker", q)
                    self.assertFalse({k.lower() for k in q} & sl.AFFILIATE_KEYS)

    def test_check_url_rejects_tampering(self):
        good = by(build())
        for platform, url in (("skyscanner", good["skyscanner"].url + "&marker=000000"),
                              ("tripcom", good["tripcom"].url + "&Allianceid=1"),
                              ("tripcom", good["tripcom"].url.replace("https://", "http://")),
                              ("google", "https://www.google.com.evil.example/travel/flights?hl=zh-TW"),
                              ("google", "https://www.google.com/travel/flights?hl=zh-TW&email=user%40example.com"),
                              ("aviasales", "https://www.aviasales.com/search/X?marker=000000&marker=1"),
                              ("aviasales", "https://user@www.aviasales.com/"),
                              ("skyscanner", "javascript:alert(1)")):
            with self.assertRaises(ValueError, msg=url):
                sl.check_url(sl.Link(platform, "x", url, "itinerary", "", platform == "aviasales"), MARKER)

    def test_html_is_escaped_and_links_are_safe(self):
        nasty = {"TPE": '<script>alert("x")</script>', "TYO": "東京&<b>"}
        subject, page, text = mailer.render_reference(fare(), nasty, {"BR": "<i>Eva</i>"}, build())
        self.assertNotIn("<script>", page)
        self.assertNotIn("<i>", page)
        self.assertIn("&lt;script&gt;", page)
        hrefs = re.findall(r'href="([^"]+)"', page)
        self.assertEqual(len(hrefs), 4)
        for h in hrefs:
            self.assertTrue(h.startswith("https://"))
        self.assertEqual(page.count('rel="noopener noreferrer"'), 4)
        self.assertIn("&amp;cabinclass=economy", page)                       # & escaped inside href


class RenderTests(unittest.TestCase):
    """L11, L13 and the §2.1 layout, with mocked fare data."""

    def render(self, m=None, links="default"):
        m = m or fare()
        if links == "default":
            o, d = m["route"].split("-")
            links = build(o, d, m["cheapest"]["depart_date"], m["cheapest"]["return_date"])
        return mailer.render_reference(m, CITIES, {"BR": "長榮航空"}, links)

    def test_reference_wording(self):
        subject, page, text = self.render(fare(usd=260))
        self.assertEqual(subject, "✈️ 台北 → 東京 參考票價 約 NT$8,062（已低於你的目標價）")
        lines = text.split("\n")
        self.assertEqual(lines[:4], ["參考票價（來源：Aviasales）：約 NT$8,062（約 US$260）", "你的目標價：NT$9,000",
                                     "去程 2026-12-11  回程 2026-12-18（來回）", "航空公司：長榮航空（BR）"])
        self.assertEqual(self.render(fare(price=9000))[0], "✈️ 台北 → 東京 參考票價 約 NT$9,000（已達你的目標價）")

    def test_links_in_order_and_in_both_parts(self):
        subject, page, text = self.render()
        for link in build().links:
            self.assertIn(f"{link.label}：{link.url}", text)
            self.assertIn(link.label, page)
        positions = [page.index(x) for x in ("Skyscanner 搜尋", "Trip.com 搜尋", "Aviasales 搜尋", "Google 航班")]
        self.assertEqual(positions, sorted(positions))
        self.assertIn(mailer.BUTTON["primary"], page.split("Skyscanner 搜尋")[0])
        self.assertIn(mailer.BUTTON["secondary"], page.split("Trip.com 搜尋")[0])

    def test_disclaimer_and_disclosure(self):
        for m in (fare(), fare(route="KHH-LON"), fare(dep=None, ret=None)):
            subject, page, text = self.render(m)
            for part in (page, text):
                self.assertIn(mailer.DISCLAIMER, part)
                self.assertIn(mailer.AFFILIATE_DISCLOSURE, part)
                self.assertIn("來源：Aviasales", part)
                self.assertIn("約 NT$", part)
                self.assertNotIn("立即訂購", part)
                self.assertNotIn("保證", part)

    def test_render_refuses_without_the_affiliate_link(self):
        bad_link_sets = [build(marker=None), build(marker="abc"), None, sl.LinkSet(),
                         sl.LinkSet((sl.Link("aviasales", "x", "https://www.aviasales.com/search/TPE1112TYO18121",
                                             "itinerary", "source", False),)),                    # plain link
                         sl.LinkSet(tuple(x for x in build().links if x.platform != "aviasales"))]  # third-party only
        for links in bad_link_sets:
            with self.assertRaises(mailer.AffiliateLinkUnavailable) as cm:
                self.render(links=links)
            self.assertNotIn(MARKER, cm.exception.reason)
        with self.assertRaises(mailer.AffiliateLinkUnavailable) as cm:
            self.render(links=build(marker=None))
        self.assertEqual(cm.exception.reason, "no_valid_aviasales_marker")

    def test_no_other_platform_named_next_to_the_price(self):
        subject, page, text = self.render()
        price_line = text.split("\n")[0]
        for name in ("Skyscanner", "Trip.com", "Google"):
            self.assertNotIn(name, price_line)
            self.assertNotIn(name, subject)

    def test_missing_dates_are_shown_as_missing(self):
        subject, page, text = self.render(fare(dep=None, ret=None))
        self.assertIn("去程 未提供  回程 未提供（來回）", text)
        self.assertIn(mailer.FALLBACK_LINE, text)
        self.assertIn(mailer.FALLBACK_LINE, page)
        self.assertEqual(mailer.FALLBACK_NOTICE, sl.FALLBACK_NOTICE)
        self.assertIn("一般搜尋頁", mailer.FALLBACK_LINE)
        self.assertIn("價格可能與 Aviasales 參考票價不同", mailer.FALLBACK_LINE)
        self.assertIn(mailer.FALLBACK_LINE, self.render()[2])               # Google is always a general page
        only_itinerary = sl.LinkSet(tuple(x for x in build().links if x.kind == "itinerary"))
        self.assertNotIn(mailer.FALLBACK_LINE, self.render(links=only_itinerary)[2])


class ConfigValidationTests(unittest.TestCase):
    """L12: an unsafe search-platforms.json fails validation (and the package build)."""

    def bad(self, c):
        with self.assertRaises(sl.ConfigError):
            sl.validate_config(c)

    def test_shipped_config_is_valid_and_marker_free(self):
        sl.validate_config(CFG)
        raw = (AWS / "config" / "search-platforms.json").read_text(encoding="utf-8")
        self.assertNotRegex(raw, r"\d{5,}")
        self.assertNotIn("marker=", raw)
        self.assertEqual([p["id"] for p in CFG["platforms"] if (p.get("affiliate") or {}).get("enabled")], ["aviasales"])

    def test_rejections(self):
        self.bad(cfg_with(tripcom={"order": 1}))                                         # duplicate order
        c = copy.deepcopy(CFG)
        c["platforms"].append({"id": "kayak", "label": "Kayak", "order": 9, "enabled": True,
                               "verified_codes": {"codes": []}})
        self.bad(c)                                                                      # unknown platform/host
        self.bad(cfg_with(tripcom={"url": "https://tw.trip.com/?Allianceid=1"}))          # templates aren't configurable
        self.bad(cfg_with(skyscanner={"params": {"utm_source": "x"}}))                    # nor are parameters
        self.bad(cfg_with(skyscanner={"affiliate": {"enabled": True}}))                   # no issued ID
        self.bad(cfg_with(aviasales={"affiliate": {"enabled": False}}))                   # condition 3
        self.bad(cfg_with(aviasales={"enabled": False}))                                  # condition 3
        self.bad(cfg_with(aviasales={"affiliate": {"enabled": True, "param": "ref", "env": "TP_MARKER", "required": True}}))
        self.bad(cfg_with(aviasales={"affiliate": {"enabled": True, "param": "marker", "env": "TP_MARKER",
                                                   "required": True, "value": "1234567"}}))    # marker committed
        self.bad(cfg_with(aviasales={"affiliate": {"enabled": True, "param": "marker", "env": "TP_MARKER",
                                                   "required": True, "value": 1234567}}))
        self.bad(cfg_with(skyscanner={"verified_codes": {"codes": ["tpe"]}}))
        self.bad(cfg_with(skyscanner={"verified_codes": {"codes": "catalog"}}))
        self.bad(cfg_with(skyscanner={"code_overrides": {"OSA": "osa/../x"}}))
        self.bad(cfg_with(skyscanner={"enabled": "yes"}))
        self.bad({"platforms": []})

    def test_aviasales_affiliate_link_is_always_required(self):
        only_av = dict(skyscanner={"enabled": False}, tripcom={"enabled": False}, google={"enabled": False})
        sl.validate_config(cfg_with(**only_av))
        self.bad(cfg_with(**only_av, aviasales={"affiliate": {"enabled": False}}))
        self.bad(cfg_with(**only_av, aviasales={"affiliate": {"enabled": True, "param": "marker", "env": "TP_MARKER",
                                                               "required": False}}))


class ServiceIntegrationTests(unittest.TestCase):
    """notification-v2 rendering layer (service.render) with mocked fare data."""

    def deps(self, **kw):
        base = dict(settings=config.Settings(), cities=CITIES, airlines={}, links_config=CFG, catalog=CAT,
                    marker=MARKER, log=self.logs.append)
        base.update(kw)
        return SimpleNamespace(**base)

    def setUp(self):
        self.logs = []

    NOW = datetime(2026, 11, 11, 2, 0, tzinfo=timezone.utc)               # 10:00 Taipei

    def test_reference_email_with_links(self):
        subject, page, text = service.render(fare(), self.NOW, self.deps(), "t")
        self.assertIn("skyscanner.com.tw/transport/flights/tpe/tyo/261211/261218/", text)
        self.assertIn("marker=000000", text)
        self.assertEqual(self.logs, [])

    def test_today_is_taipei_time(self):
        late = datetime(2026, 12, 10, 16, 30, tzinfo=timezone.utc)        # 2026-12-11 00:30 Taipei
        subject, page, text = service.render(fare(), late, self.deps(), "t")
        self.assertIn("transport/flights/tpe/tyo", text)                   # depart today: still valid
        later = datetime(2026, 12, 11, 16, 30, tzinfo=timezone.utc)       # 2026-12-12 Taipei: departed
        subject, page, text = service.render(fare(), later, self.deps(), "t")
        self.assertNotIn("transport/flights/tpe", text)
        self.assertEqual(self.logs, ["t: links: departure_in_the_past"])

    def blocked(self, d, m=None):
        with self.assertRaises(mailer.AffiliateLinkUnavailable) as cm:
            service.render(m or fare(), self.NOW, d, "t")
        return cm.exception.reason

    def test_builder_error_blocks_the_email(self):
        broken = copy.deepcopy(CFG)
        del broken["platforms"][0]["verified_codes"]
        self.assertEqual(self.blocked(self.deps(links_config=broken)), "link_builder_error:KeyError")

    def test_missing_config_or_marker_blocks_and_m1_rollback_is_unchanged(self):
        self.assertEqual(self.blocked(self.deps(links_config=None)), "links_config_unavailable")
        self.assertEqual(self.blocked(self.deps(catalog=None)), "links_config_unavailable")
        self.assertEqual(self.blocked(self.deps(marker=None)), "no_valid_aviasales_marker")
        self.assertEqual(self.blocked(self.deps(marker="12345x")), "no_valid_aviasales_marker")
        self.assertEqual(self.blocked(self.deps(), fare(route="TPE-ZZZ")), "unsupported_code")
        d = self.deps(settings=config.Settings(email_template="m1"), marker=None, links_config=None)
        subject, page, text = service.render(fare(), self.NOW, d, "t")              # M1: no marker needed
        self.assertEqual((subject, page, text), mailer.render(fare(), CITIES, {}))
        self.assertIn("立即訂購", text)
        with self.assertRaises(config.ConfigError):
            config.Settings.from_env({"EMAIL_TEMPLATE": "fancy"})
        self.assertEqual(config.Settings.from_env({}).email_template, "reference")

    def test_logs_carry_no_personal_data(self):
        self.blocked(self.deps(marker="99999x"), fare(route="KHH-LON", dep="bad", ret=None))
        service.render(fare(dep="bad"), self.NOW, self.deps(), "t")
        joined = " ".join(self.logs)
        for bad in ("@", MARKER, "99999x", EMAIL):
            self.assertNotIn(bad, joined)

    def test_handler_link_deps(self):
        import handler as h
        logs = []
        d = h.link_deps({"TP_MARKER": "000000"}, log=logs.append)
        self.assertEqual((d["marker"], logs), (MARKER, []))
        self.assertIsNotNone(d["links_config"])
        self.assertIsNotNone(d["catalog"])
        logs.clear()
        d = h.link_deps({"TP_MARKER": "abc.def", "TP_TOKEN": "tok-secret-value"}, log=logs.append)
        self.assertIsNone(d["marker"])
        self.assertEqual(logs, ["links: ERROR TP_MARKER has an unexpected format; reference-fare emails will be "
                                "blocked (EMAIL_TEMPLATE=reference)"])
        logs.clear()
        self.assertIsNone(h.link_deps({"TP_TOKEN": "tok-secret-value"}, log=logs.append)["marker"])
        self.assertEqual(logs, ["links: ERROR TP_MARKER is not set; reference-fare emails will be blocked "
                                "(EMAIL_TEMPLATE=reference)"])
        self.assertNotIn("tok-secret-value", repr(d))


class EndToEndTests(unittest.TestCase):
    """service.process() on local DynamoDB with a fake Resend: the email that would be sent."""

    @classmethod
    def setUpClass(cls):
        import test_m14_notify_dedup as dedup
        if not dedup.AVAILABLE:
            raise unittest.SkipTest("boto3/moto not available")
        cls.dedup = dedup

    def run_case(self, marker, links_config=CFG, **fare_kw):
        dedup = self.dedup

        class Case(dedup.Base):
            def runTest(self):
                pass
        case = Case()
        Case.setUpClass()
        case.setUp()
        try:
            d = case.deps()
            d.links_config, d.catalog, d.marker, d.cities = links_config, CAT, marker, CITIES
            m = fare(**fare_kw)
            now = datetime(2026, 11, 11, 2, 0, tzinfo=timezone.utc)
            result = service.process(m, "delivery-1", now, d)
            return result, case.resend.requests, case.logs, case.items()
        finally:
            case.tearDown()

    def test_sent_email_contains_the_links(self):
        result, requests, logs, items = self.run_case(MARKER)
        self.assertEqual(result, "sent")
        self.assertTrue(any(i["sent_at"] == "STATE" and i.get("last_event_id") for i in items))
        body = requests[0]["body"]
        self.assertEqual(body["to"], [EMAIL])
        self.assertIn("https://www.aviasales.com/search/TPE1112TYO18121?marker=000000", body["text"])
        self.assertIn("在 Skyscanner 搜尋此行程", body["html"])
        self.assertIn(mailer.AFFILIATE_DISCLOSURE, body["text"])
        for url in re.findall(r"https://\S+", body["text"]):
            self.assertNotIn(EMAIL, url)
            self.assertNotIn("@", url)

    def test_blocked_notifications_send_nothing_and_record_nothing(self):
        broken = copy.deepcopy(CFG)
        del broken["platforms"][0]["verified_codes"]
        for marker, cfg, reason in ((None, CFG, "no_valid_aviasales_marker"),
                                    ("not-a-marker", CFG, "no_valid_aviasales_marker"),
                                    (MARKER, None, "links_config_unavailable"),
                                    (MARKER, broken, "link_builder_error:KeyError")):
            result, requests, logs, items = self.run_case(marker, links_config=cfg)
            self.assertEqual(result, f"blocked:{reason}")
            self.assertEqual(requests, [])                         # Resend never called
            self.assertEqual(items, [])                            # no claim, no STATE, no log, no M1 row
            self.assertTrue(any("BLOCKED" in x for x in logs))
            for x in logs:
                self.assertNotIn("@", x)
                self.assertNotIn("not-a-marker", x)


class PackagingTests(unittest.TestCase):
    def test_zip_bundles_the_config_and_loads_it(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("pkg_n2b", AWS / "scripts" / "package_notification_v2.py")
        pkg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pkg)
        with tempfile.TemporaryDirectory() as out, tempfile.TemporaryDirectory() as run:
            r = pkg.build(Path(out))
            self.assertIn("notify2/data/search-platforms.json", r["files"])
            self.assertIn("notify2/data/airports.json", r["files"])
            zipfile.ZipFile(r["zip"]).extractall(run)
            code = (f"import sys; sys.path.insert(0, {run!r}); from notify2 import searchlinks as s; from datetime import date; "
                    "print(s.build('TPE','TYO','2026-12-11','2026-12-18',date(2026,11,11),"
                    "catalog=s.load_catalog(),config=s.load_config(),marker='000000').links[0].url)")
            out_text = subprocess.run([sys.executable, "-I", "-c", code], cwd=run, capture_output=True, text=True,
                                      env={"PYTHONDONTWRITEBYTECODE": "1"}, check=True).stdout
            self.assertTrue(out_text.startswith("https://www.skyscanner.com.tw/transport/flights/tpe/tyo/"))

    def test_unsafe_config_fails_the_build(self):
        import importlib.util
        from unittest import mock
        spec = importlib.util.spec_from_file_location("pkg_n2c", AWS / "scripts" / "package_notification_v2.py")
        pkg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pkg)
        bad = cfg_with(tripcom={"affiliate": {"enabled": True}})
        with mock.patch.object(sl, "validate_config", side_effect=sl.ConfigError("x")), \
                tempfile.TemporaryDirectory() as out:
            with self.assertRaises(sl.ConfigError):
                pkg.build(Path(out))
        with self.assertRaises(sl.ConfigError):
            sl.validate_config(bad)


if __name__ == "__main__":
    unittest.main()
