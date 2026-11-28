"""B6-3B: Partner Links API producer in the fare check (offline).

The parser creates ONE partner link per fare offer after matching and before anything is
enqueued, and attaches it to every recipient's message as `aviasales_partner_url`. Any failure
enqueues nothing. The API is never reached: every test injects a fake transport, and the network
(sockets, urlopen) is blocked for the whole module. All values are synthetic: marker and project
id 000000, hosts on .example.test, addresses @example.com.

Producer/consumer parity: farecheck/searchlinks.py and farecheck/partnerlinks.py are mirrored,
byte-identical copies of the notify2 modules (as subkeys.py is mirrored today); tests check the
copies, the shared 85-case link matrix, the expected destination on both sides, and a full
producer -> notification round trip on DynamoDB Local (moto).

Run: python3 -m unittest aws/tests/test_m14_partner_producer.py
The file name sorts after test_m14_notify_dedup.py for the boto3-stub reason given in
test_m14_notify_month_guard.py.
"""
import copy
import dataclasses
import importlib
import json
import socket
import sys
import unittest
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
AWS = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(AWS / "lambdas" / "flight-fare-check"))
from farecheck import config, effects, partnerapi, partnerlinks as fpl, runner, searchlinks as fsl  # noqa: E402

dedup = importlib.import_module(f"{__package__}.test_m14_notify_dedup" if __package__ else "test_m14_notify_dedup")
if dedup.AVAILABLE:
    from notify2 import keys, partnerlinks as npl, searchlinks as nsl, service  # noqa: E402

NOW = datetime(2026, 11, 6, 13, 0, tzinfo=timezone.utc)      # 21:00 Taipei
AS_OF = "2026-11-06T13:00:00Z"
MARKER, TRS = "100001", "200002"                              # synthetic; numbers without a leading zero
TOKEN = "SYNTHETIC-TOKEN-xyz"
HEADER = "X-Synthetic-Auth"                                   # a test-only header name, not a guess
HOST = "links.example.test"
CATALOG = config.load_catalog()
EVENT = {"origin": "TPE", "destination": "TYO", "route": "TPE-TYO", "month": "2026-12", "as_of": AS_OF}


def link_format(module):
    return module.LinkFormat(name="synthetic-test", host=HOST, path="/r",
                             params=(("marker", "marker"), ("trs", "trs"), ("sub_id", ("fixed", "fff-alert-v1")),
                                     ("u", "destination")),
                             evidence="synthetic offline test format; NOT an observed Travelpayouts format")


def partner_url(dest, **override):
    v = {"marker": MARKER, "trs": TRS, "sub_id": "fff-alert-v1", "u": dest}
    v.update(override)
    return f"https://{HOST}/r?" + urllib.parse.urlencode(list(v.items()))


def api_ok(req, url=None, **result):
    links = [{"url": x["url"], "code": "success", "partner_url": url or partner_url(x["url"])} for x in req["links"]]
    r = {"trs": req["trs"], "marker": req["marker"], "shorten": False, "links": links}
    r.update(result)
    return {"code": "success", "status": 200, "result": r}


class FakeApi:
    """Records each request; `answers` is a list of callables/exceptions, one per call."""
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": copy.deepcopy(body), "timeout": timeout})
        a = self.answers.pop(0) if self.answers else api_ok
        if isinstance(a, BaseException):
            raise a
        return a(body) if callable(a) else a


def fare(price=8000, dep="2026-12-11T07:30:00+08:00", ret="2026-12-18T19:00:00+09:00"):
    return {"success": True, "currency": "twd",
            "data": [{"price": price, "airline": "BR", "departure_at": dep, "return_at": ret}]}


def row(email, target=9000, **kw):
    r = {"email": email, "route": "TPE-TYO", "origin": "TPE", "destination": "TYO",
         "target_price": Decimal(str(target)), "departure_month": "2026-12"}
    r.update(kw)
    return r


def settings(mode="partner_api", **kw):
    base = dict(mode="live", fare_api="v3", fetch_usd=False, message_version=2, queue_url="https://sqs.example.test/q",
                affiliate_link_mode=mode, tp_marker=MARKER, tp_trs=TRS)
    base.update(kw)
    return config.Settings(**base)


def no_network(*a, **k):
    raise AssertionError("network access is blocked in these tests")


class Offline(unittest.TestCase):
    def setUp(self):
        for target, attr in ((socket, "socket"), (socket, "create_connection"), (urllib.request, "urlopen")):
            p = mock.patch.object(target, attr, no_network)
            p.start()
            self.addCleanup(p.stop)
        self.logs, self.sqs = [], mock.MagicMock()

    def run_parser(self, rows, api=None, s=None, formats="approved", fare_body=None, remaining_ms=None, sleep=None):
        self.api = api if api is not None else FakeApi()
        self.sleeps = []
        partner = {"transport": self.api, "auth_header": HEADER, "sleep": sleep or self.sleeps.append,
                   "formats": (link_format(fpl),) if formats == "approved" else formats}
        return runner.run_parser(dict(EVENT), settings=s or settings(), catalog=CATALOG, token=TOKEN,
                                 reader=effects.ListReader(rows), now=NOW,
                                 transport=lambda url, headers, timeout: fare_body or fare(),
                                 client_factory=lambda name: self.sqs, log=self.logs.append,
                                 partner=partner, remaining_ms=remaining_ms)

    def enqueued(self):
        return [json.loads(c.kwargs["MessageBody"]) for c in self.sqs.send_message.call_args_list]

    def assert_nothing_enqueued(self, out, reason):
        self.assertEqual(self.enqueued(), [])
        self.assertEqual(out["matched"], 0)
        self.assertEqual(out["partner_link"], reason)
        self.assertGreater(out["not_enqueued"], 0)

    def assert_no_secrets_logged(self):
        text = "\n".join(self.logs)
        for secret in (MARKER, TOKEN, HOST, "aviasales.com", "@example.com"):
            self.assertNotIn(secret, text)


class FlowTests(Offline):
    def test_01_no_matched_subscriber_no_api_call(self):
        out = self.run_parser([row("a@example.com", target=5000)])            # fare above every target
        self.assertEqual((out["matched"], self.api.calls, self.enqueued()), (0, [], []))
        self.run_parser([])
        self.assertEqual(self.api.calls, [])

    def test_02_03_04_one_call_per_offer_and_a_shared_link(self):
        out = self.run_parser([row("a@example.com"), row("b@example.com", target=8500), row("c@example.com", target=5000)])
        self.assertEqual(len(self.api.calls), 1)
        call = self.api.calls[0]
        self.assertEqual(call["url"], "https://api.travelpayouts.com/links/v1/create")
        self.assertEqual(call["headers"][HEADER], TOKEN)
        dest = "https://www.aviasales.com/search/TPE1112TYO18121"
        self.assertEqual(call["body"], {"trs": 200002, "marker": 100001, "shorten": False,
                                        "links": [{"url": dest, "sub_id": "fff-alert-v1"}]})
        self.assertLessEqual(call["timeout"], 5)
        bodies = self.enqueued()
        self.assertEqual(out["matched"], 2)
        self.assertEqual(sorted(b["email"] for b in bodies), ["a@example.com", "b@example.com"])
        self.assertEqual({b["aviasales_partner_url"] for b in bodies}, {partner_url(dest)})
        self.assert_no_secrets_logged()

    def test_08_dest_has_no_marker_and_is_exactly_what_the_notification_rebuilds(self):
        self.run_parser([row("a@example.com")])
        dest = self.api.calls[0]["body"]["links"][0]["url"]
        self.assertNotIn("marker", dest)
        self.assertEqual(urllib.parse.urlsplit(dest).query, "")

    def test_18_disabled_and_direct_modes_are_unchanged(self):
        for mode in ("disabled", "direct"):
            with self.subTest(mode):
                self.sqs = mock.MagicMock()
                out = self.run_parser([row("a@example.com")], s=settings(mode=mode))
                self.assertEqual(self.api.calls, [])
                self.assertEqual(out["matched"], 1)
                self.assertNotIn("aviasales_partner_url", self.enqueued()[0])
                self.assertNotIn("partner_link", out)

    def test_dry_run_never_calls_the_api(self):
        out = runner.run_parser(dict(EVENT, dry_run=True), settings=settings(), catalog=CATALOG, token=TOKEN,
                                reader=effects.ListReader([row("a@example.com")]), now=NOW,
                                transport=lambda u, h, t: fare(), client_factory=lambda n: self.sqs,
                                log=self.logs.append, partner={"transport": FakeApi(), "auth_header": HEADER})
        self.assertTrue(out["dry_run"])
        self.sqs.send_message.assert_not_called()
        self.assertTrue(any("never called in dry run" in x for x in self.logs))


class ResponseTests(Offline):
    ROWS = [row("a@example.com"), row("b@example.com")]

    def check_fails(self, answers, reason, calls=1, **kw):
        out = self.run_parser(self.ROWS, api=FakeApi(*answers), **kw)
        self.assert_nothing_enqueued(out, reason)
        self.assertEqual(len(self.api.calls), calls)
        self.assert_no_secrets_logged()

    def mutate(self, fn):
        def answer(req):
            r = api_ok(req)
            fn(r, req)
            return r
        return answer

    def test_05_invalid_json(self):
        self.check_fails([ValueError("bad json")], "malformed_json")

    def test_06_missing_or_malformed_fields(self):
        cases = {
            "api_not_success": lambda r, q: r.update(code="error"),
            "api_no_result": lambda r, q: r.pop("result"),
            "api_link_count": lambda r, q: r["result"].update(links=[]),
            "api_link_shape": lambda r, q: r["result"].update(links=["x"]),
            "api_no_partner_url": lambda r, q: r["result"]["links"][0].pop("partner_url"),
            "api_short_links": lambda r, q: r["result"].update(shorten=True),
        }
        for reason, fn in cases.items():
            with self.subTest(reason):
                self.sqs = mock.MagicMock()
                self.check_fails([self.mutate(fn)], reason)
        self.sqs = mock.MagicMock()
        self.check_fails([self.mutate(lambda r, q: r["result"]["links"][0].update(partner_url=123))],
                         "api_no_partner_url")
        self.sqs = mock.MagicMock()
        self.check_fails(["not an object"], "api_not_success")

    def test_07_mismatched_marker_or_trs(self):
        self.check_fails([self.mutate(lambda r, q: r["result"].update(marker=111111))], "api_marker_mismatch")
        self.sqs = mock.MagicMock()
        self.check_fails([self.mutate(lambda r, q: r["result"].update(trs=222222))], "api_trs_mismatch")

    def test_08_mismatched_destination(self):
        self.check_fails([self.mutate(lambda r, q: r["result"]["links"][0].update(url="https://www.aviasales.com/"))],
                         "api_unexpected_url")
        self.sqs = mock.MagicMock()
        other = partner_url("https://www.aviasales.com/search/TPE1112OKA18121")
        self.check_fails([lambda req: api_ok(req, url=other)], "partner_link_invalid:destination_mismatch")
        self.sqs = mock.MagicMock()
        self.check_fails([lambda req: api_ok(req, url=partner_url(req["links"][0]["url"], marker="111111"))],
                         "partner_link_invalid:marker_mismatch")

    def test_09_failed_individual_link(self):
        self.check_fails([self.mutate(lambda r, q: r["result"]["links"][0].update(code="failed",
                                                                                     message="can't create partner link"))],
                         "api_link_failed")

    def test_10_http_400_and_401_are_not_retried(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status):
                self.sqs = mock.MagicMock()
                self.check_fails([partnerapi.HttpError(status)], f"http_{status}")
                self.assertEqual(self.sleeps, [])

    def test_11_429_and_5xx_retry_once(self):
        for status in (429, 500, 502, 503):
            with self.subTest(status):
                self.sqs = mock.MagicMock()
                out = self.run_parser(self.ROWS, api=FakeApi(partnerapi.HttpError(status), api_ok))
                self.assertEqual((len(self.api.calls), self.sleeps, out["matched"]), (2, [1], 2))
                self.assertEqual(len({b["aviasales_partner_url"] for b in self.enqueued()}), 1)
                self.sqs = mock.MagicMock()
                self.check_fails([partnerapi.HttpError(status), partnerapi.HttpError(status), api_ok],
                                 f"http_{status}", calls=2)

    def test_12_timeout_and_bounded_retry(self):
        out = self.run_parser(self.ROWS, api=FakeApi(TimeoutError(), api_ok))
        self.assertEqual((len(self.api.calls), out["matched"]), (2, 2))
        self.sqs = mock.MagicMock()
        self.check_fails([TimeoutError(), OSError("reset"), api_ok], "unavailable", calls=2)
        self.sqs = mock.MagicMock()
        self.check_fails([TimeoutError(), api_ok], "unavailable_no_time_to_retry", calls=1, remaining_ms=lambda: 14_999)
        self.assertEqual(self.sleeps, [])

    def test_13_no_partial_enqueue(self):
        """Link creation precedes every send: a failure leaves the queue untouched for all
        recipients of the offer, and no message without the link is ever sent."""
        for answers in ([partnerapi.HttpError(500), partnerapi.HttpError(500)], [ValueError()],
                        [lambda req: api_ok(req, url="https://evil.example.test/x")]):
            self.sqs = mock.MagicMock()
            out = self.run_parser([row(f"u{i}@example.com") for i in range(5)], api=FakeApi(*answers))
            self.sqs.send_message.assert_not_called()
            self.assertEqual(out["not_enqueued"], 5)

    def test_16_unapproved_format_fails_closed(self):
        """A response that passes the documented contract is still not queued while no format is
        approved (the shipped list is empty until B6-3C)."""
        self.assertEqual(fpl.APPROVED_FORMATS, ())
        self.check_fails([api_ok], "partner_link_invalid:no_approved_format", formats=fpl.APPROVED_FORMATS)

    def test_16b_unconfirmed_auth_header_never_sends(self):
        self.assertIsNone(partnerapi.CONFIRMED_AUTH_HEADER)
        api = FakeApi()
        out = runner.run_parser(dict(EVENT), settings=settings(), catalog=CATALOG, token=TOKEN,
                                reader=effects.ListReader(self.ROWS), now=NOW, transport=lambda u, h, t: fare(),
                                client_factory=lambda n: self.sqs, log=self.logs.append,
                                partner={"transport": api, "formats": (link_format(fpl),)})
        self.assert_nothing_enqueued(out, "auth_method_unconfirmed")
        self.assertEqual(api.calls, [])

    def test_17_direct_marker_fallback_prohibited(self):
        """A failure never enqueues a message without the partner link (which the notification
        would block anyway), and a direct-marker URL returned by the API is rejected."""
        direct = "https://www.aviasales.com/search/TPE1112TYO18121?marker=000000"
        self.check_fails([lambda req: api_ok(req, url=direct)], "partner_link_invalid:unapproved_host_or_path")
        self.sqs = mock.MagicMock()
        self.run_parser(self.ROWS)
        self.assertTrue(all("aviasales_partner_url" in b for b in self.enqueued()))


class UnitTests(Offline):
    def body(self, **kw):
        b = {"email": "a@example.com", "route": "TPE-TYO", "target_price": 9000, "month": "2026-12", "as_of": AS_OF,
             "cheapest": {"price": 8000, "currency": "TWD", "airline": "BR",
                          "depart_date": "2026-12-11T07:30:00+08:00", "return_date": "2026-12-18T19:00:00+09:00"}}
        b.update(kw)
        return b

    def test_15_month_mismatch_gets_no_link_and_no_api_call(self):
        api = FakeApi()
        for month in ("2027-01", "2026-13"):
            with self.subTest(month), self.assertRaises(partnerapi.PartnerLinkUnavailable) as e:
                partnerapi.approved_partner_link(self.body(month=month), token=TOKEN, marker=MARKER, trs=TRS,
                                                 sub_id="fff-alert-v1", transport=api, auth_header=HEADER,
                                                 formats=(link_format(fpl),))
            self.assertIn(e.exception.reason, ("fare_outside_subscribed_month", "invalid_subscribed_month"))
        with self.assertRaises(partnerapi.PartnerLinkUnavailable) as e:            # v1 shape: no as_of/month
            partnerapi.expected_destination(self.body(month=None, as_of=None), marker=MARKER)
        self.assertEqual(e.exception.reason, "needs_v2_message")
        self.assertEqual(api.calls, [])

    def test_general_fallback_destination(self):
        """A fare the builder can't link as an itinerary (code not verified for Aviasales here,
        or a return too far ahead) gets the Aviasales general page as its destination."""
        far = self.body(cheapest=dict(self.body()["cheapest"], depart_date="2027-11-20", return_date="2027-11-27"),
                        month="2027-11")
        self.assertEqual(partnerapi.expected_destination(far, marker=MARKER), "https://www.aviasales.com/")

    def test_config(self):
        self.assertEqual(config.Settings().affiliate_link_mode, "disabled")
        env = {"FARECHECK_MODE": "live", "NOTIFY_MESSAGE_VERSION": "2"}
        self.assertEqual(config.Settings.from_env(env).affiliate_link_mode, "disabled")
        s = config.Settings.from_env(dict(env, AFFILIATE_LINK_MODE="partner_api", TP_MARKER=MARKER, TP_TRS=TRS))
        self.assertEqual((s.affiliate_link_mode, s.partner_sub_id), ("partner_api", "fff-alert-v1"))
        self.assertNotIn(MARKER, repr(s))
        self.assertNotIn(TRS, repr(s))
        bad = [dict(AFFILIATE_LINK_MODE="partner_api", TP_TRS=TRS),
               dict(AFFILIATE_LINK_MODE="partner_api", TP_MARKER=MARKER),
               dict(AFFILIATE_LINK_MODE="partner_api", TP_MARKER="abc", TP_TRS=TRS),
               dict(AFFILIATE_LINK_MODE="partner_api", TP_MARKER="000000", TP_TRS=TRS),     # leading zero
               dict(AFFILIATE_LINK_MODE="partner_api", TP_MARKER=MARKER, TP_TRS="0200"),
               dict(AFFILIATE_LINK_MODE="partner_api", TP_MARKER=MARKER, TP_TRS=TRS, NOTIFY_MESSAGE_VERSION="1"),
               dict(AFFILIATE_LINK_MODE="on"),
               dict(PARTNER_SUB_ID="user@example.com"), dict(PARTNER_SUB_ID="a b"), dict(PARTNER_SUB_ID="x" * 33)]
        for extra in bad:
            with self.subTest(extra), self.assertRaises(config.ConfigError) as e:
                config.Settings.from_env(dict(env, **extra))
            for v in (MARKER, TRS, "000000"):
                self.assertNotIn(v, str(e.exception))

    def test_packaging_includes_the_mirrored_builder_and_its_data(self):
        spec = importlib.util.spec_from_file_location("pkg_fc_b63b", AWS / "scripts" / "package_fare_check.py")
        pkg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pkg)
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            names = zipfile.ZipFile(pkg.build(Path(d))["zip"]).namelist()
        for n in ("farecheck/searchlinks.py", "farecheck/partnerlinks.py", "farecheck/partnerapi.py",
                  "farecheck/data/search-platforms.json", "farecheck/data/airports.json", "farecheck/config/airports.json"):
            self.assertIn(n, names)


@unittest.skipUnless(dedup.AVAILABLE, "boto3/moto not available")
class ParityTests(unittest.TestCase):
    def test_19a_mirrored_modules_are_identical(self):
        for name in ("searchlinks.py", "partnerlinks.py"):
            a = (AWS / "lambdas" / "flight-fare-check" / "farecheck" / name).read_bytes()
            b = (AWS / "lambdas" / "flight-fare-notification-v2" / "notify2" / name).read_bytes()
            self.assertEqual(a, b, f"{name}: the fare-check copy differs from notify2's")

    def test_19b_link_matrix_identical_on_both_sides(self):
        """All 85 shared vectors: codes, both dates, passenger count, month check, encoding,
        fallbacks; with the shipped config and with Skyscanner/Trip.com switched on."""
        matrix = json.loads((HERE / "vectors" / "search-links-matrix.json").read_text(encoding="utf-8"))
        shipped = nsl.load_config()
        authorized = copy.deepcopy(shipped)
        for p in authorized["platforms"]:
            p.update({"skyscanner": {"enabled": True}, "tripcom": {"enabled": True},
                      "aviasales": {"role": "source"}}.get(p["id"], {}))
        for base in (shipped, authorized):
            for c in matrix["cases"]:
                cfg = copy.deepcopy(base)
                for p in cfg["platforms"]:
                    if p["id"] in c.get("disable", []):
                        p["enabled"] = False
                    if p["id"] in c.get("unverify", []):
                        p["verified_codes"]["codes"] = []
                now = datetime.fromisoformat(c["now"].replace("Z", "+00:00"))
                args = (c["origin"], c["destination"], c["depart_date"], c["return_date"])
                kw = dict(config=cfg, marker=c.get("marker", matrix["default_marker"]), month=c.get("month"))
                a = fsl.build(*args, fsl.months.today(now), catalog=fsl.load_catalog(), **kw)
                b = nsl.build(*args, nsl.months.today(now), catalog=nsl.load_catalog(), **kw)
                with self.subTest(c["id"]):                   # field by field (two distinct classes)
                    self.assertEqual(dataclasses.asdict(a), dataclasses.asdict(b))
                    self.assertEqual(a.compliant, b.compliant)

    def test_19c_expected_destination_parity(self):
        dests = set()
        for origin in ("TPE", "KHH", "RMQ"):
            for month, dep, ret in (("2026-12", "2026-12-11T07:30:00+08:00", "2026-12-18"),
                                    ("2027-06", "2027-06-01", "2027-06-30"), ("2027-11", "2027-11-20", "2027-11-27")):
                m = {"email": "a@example.com", "route": f"{origin}-TYO", "month": month, "as_of": AS_OF, "target_price": 9000,
                     "cheapest": {"price": 8000, "currency": "TWD", "airline": "BR", "depart_date": dep, "return_date": ret}}
                produced = partnerapi.expected_destination(m, marker=MARKER)
                deps = dataclasses.replace(service.Deps(settings=dedup.config.Settings(), history=None, pepper="p" * 40,
                                                         resend_cfg=None), links_config=nsl.load_config(),
                                           catalog=nsl.load_catalog(), marker=MARKER)
                consumed = npl.expected_destination(service.search_links(m, NOW, deps).aviasales.url, MARKER)
                with self.subTest(origin=origin, month=month):
                    self.assertEqual(produced, consumed)
                    self.assertNotIn("marker", produced)
                dests.add(produced)
        self.assertIn("https://www.aviasales.com/", dests)                     # general fallback covered
        self.assertTrue(any("/search/" in d for d in dests))                   # itinerary covered


@unittest.skipUnless(dedup.AVAILABLE, "boto3/moto not available")
class RoundTripTests(dedup.Base):
    """19d/14/18: a producer message goes through the B6-3A notification unchanged in semantics."""
    def produce(self, rows):
        sqs, api = mock.MagicMock(), FakeApi()
        runner.run_parser(dict(EVENT), settings=settings(), catalog=CATALOG, token=TOKEN, reader=effects.ListReader(rows),
                          now=NOW, transport=lambda u, h, t: fare(), client_factory=lambda n: sqs, log=lambda m: None,
                          partner={"transport": api, "auth_header": HEADER, "sleep": lambda s: None,
                                   "formats": (link_format(fpl),)})
        return [json.loads(c.kwargs["MessageBody"]) for c in sqs.send_message.call_args_list]

    def consumer(self, formats):
        d = self.deps(affiliate_link_mode="partner_api")
        return dataclasses.replace(d, partner_formats=formats, partner_trs=TRS, marker=MARKER)

    def test_round_trip_sends_the_producer_link(self):
        (m,) = self.produce([row("user@example.com")])
        at = NOW + timedelta(minutes=1)
        self.assertEqual(self.run_msg(m, at, d=self.consumer((link_format(npl),))), "sent")
        self.assertIn(m["aviasales_partner_url"], self.resend.requests[0]["body"]["text"])
        self.assertEqual(self.run_msg(m, at + timedelta(hours=1), d=self.consumer((link_format(npl),))),
                         "skipped:duplicate_event")
        self.assertEqual(len(self.resend.deliveries), 1)

    def test_round_trip_with_no_approved_format_is_blocked_without_history(self):
        (m,) = self.produce([row("user@example.com")])
        out = self.run_msg(m, NOW + timedelta(minutes=1), d=self.consumer(npl.APPROVED_FORMATS))
        self.assertEqual(out, "blocked:partner_link_invalid:no_approved_format")
        self.assertEqual((self.resend.requests, list(self.items())), ([], []))

    def test_14_event_id_unchanged(self):
        (m,) = self.produce([row("user@example.com")])
        pk = keys.stream_pk(keys.subscriber_id("user@example.com", dedup.PEPPER), "TPE-TYO", "2026-12")
        without = {k: v for k, v in m.items() if k != "aviasales_partner_url"}
        self.assertEqual(keys.event_id(pk, m, "d1"), keys.event_id(pk, without, "d2"))

    def test_18_dedup_and_history_unchanged(self):
        (m,) = self.produce([row("user@example.com")])
        d = self.consumer((link_format(npl),))
        self.assertEqual(self.run_msg(m, NOW + timedelta(minutes=1), d=d), "sent")
        later = dict(m, as_of="2026-11-06T13:30:00Z")                   # next schedule, same fare
        self.assertEqual(self.run_msg(later, NOW + timedelta(minutes=31), d=d), "skipped:deduped")
        rows = [i for i in self.items() if i["sent_at"] != keys.STATE_SK]
        self.assertEqual(len(rows), 2)                                    # v2 log row + M1 dual-write row


if __name__ == "__main__":
    unittest.main()
