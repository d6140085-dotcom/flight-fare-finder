"""B6-3A: notification-side foundation for official Travelpayouts partner links (offline).

Covers AFFILIATE_LINK_MODE (disabled | direct | partner_api), the optional SQS field
`aviasales_partner_url`, the layered validator (notify2.partnerlinks) and the event-to-link pin
in notification_history. No API client exists and nothing here calls Travelpayouts or Resend:
DynamoDB is moto's local server via test_m14_notify_dedup.Base, Resend is its FakeResend (which
replays an Idempotency-Key and rejects a different payload under the same key, as Resend does).

Every marker, project id, host and link is synthetic: the marker and project id are the fake
value 000000 and the approved format below is a TEST format on an .example.test host, not an
observed Travelpayouts format. The shipped APPROVED_FORMATS list is empty.

Run: python3 -m unittest aws/tests/test_m14_notify_partner_links.py

The file name sorts after test_m14_notify_dedup.py for the reason given in
test_m14_notify_month_guard.py (boto3 stubs in test_lambdas.py / test_m14_phase0.py).
"""
import dataclasses
import importlib
import io
import json
import sys
import threading
import unittest
import urllib.parse
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
dedup = importlib.import_module(f"{__package__}.test_m14_notify_dedup" if __package__ else "test_m14_notify_dedup")

if dedup.AVAILABLE:
    from notify2 import config, keys, mailer, partnerlinks as pl, service  # noqa: E402

NOW, A, PEPPER = dedup.NOW, dedup.A, dedup.PEPPER
AS_OF = "2026-11-20T03:30:00Z"           # v2 messages: one schedule run -> one event id
MARKER = TRS = "000000"                  # synthetic test values (never real identifiers)
HOST = "links.example.test"


def load_handler():
    """handler.py creates boto3 clients at import: give it a fake region (no AWS is called)."""
    with mock.patch.dict("os.environ", dedup.ddb_local.FAKE_ENV):
        return importlib.import_module("handler")


def test_format():
    return pl.LinkFormat(name="synthetic-test", host=HOST, path="/r",
                         params=(("marker", "marker"), ("trs", "trs"), ("sub_id", ("fixed", "b6test")),
                                 ("u", "destination")),
                         evidence="synthetic test format for offline tests; NOT an observed Travelpayouts format")


def partner(dest, order=("marker", "trs", "sub_id", "u"), **override):
    values = {"marker": MARKER, "trs": TRS, "sub_id": "b6test", "u": dest}
    values.update(override)
    return f"https://{HOST}/r?" + urllib.parse.urlencode([(k, values[k]) for k in order])


def v2(month="2027-01", link=None, as_of=AS_OF, **kw):
    m = dedup.month_msg(month, as_of=as_of, **kw)
    if link is not None:
        m["aviasales_partner_url"] = link
    return m


@unittest.skipUnless(dedup.AVAILABLE, "boto3/moto not available")
class PartnerBase(dedup.Base):
    def pdeps(self, formats=None, trs=TRS, **settings):
        settings.setdefault("affiliate_link_mode", "partner_api")
        d = self.deps(**settings)
        return dataclasses.replace(d, partner_formats=(test_format(),) if formats is None else formats,
                                   partner_trs=trs)

    def dest(self, m):
        """The expected destination: the direct Aviasales link without its marker."""
        direct = service.search_links(m, NOW, self.deps())
        return pl.expected_destination(direct.aviasales.url, MARKER)

    def sent_links(self):
        return [r["body"]["text"].split(f"https://{HOST}/r?")[1].split()[0] for r in self.resend.requests]

    def state(self):
        items = [i for i in self.items() if i["sent_at"] == keys.STATE_SK]
        return items[0] if items else None

    def history_rows(self):
        return list(self.items())


class ValidLinkTests(PartnerBase):
    def test_01_first_valid_link_accepted(self):
        m = v2()
        link = partner(self.dest(m))
        m["aviasales_partner_url"] = link
        self.assertEqual(self.run_msg(m, d=self.pdeps()), "sent")
        body = self.resend.requests[0]["body"]
        self.assertIn(link, body["text"])
        self.assertIn(link.replace("&", "&amp;"), body["html"])
        for part in (body["text"], body["html"]):                         # no direct-marker URL anywhere
            self.assertNotIn("https://www.aviasales.com/search/", part)   # (only encoded, inside the partner link)
            self.assertNotIn("https://www.aviasales.com/?marker", part)
        st = self.state()
        self.assertEqual(st["link_url"], link)
        self.assertEqual(st["link_event"], st["last_event_id"])

    def test_google_stays_the_only_general_fallback_and_disabled_providers_stay_off(self):
        m = v2()
        m["aviasales_partner_url"] = partner(self.dest(m))
        ls = service.search_links(m, NOW, self.pdeps())
        self.assertEqual([link.platform for link in ls.links], ["aviasales", "google"])
        self.assertEqual(ls.aviasales.via, "partner_api")
        self.assertTrue(ls.compliant)
        self.assertFalse(ls.links[1].affiliate)

    def test_02_same_sqs_message_retried(self):
        m = v2()
        m["aviasales_partner_url"] = partner(self.dest(m))
        d = self.pdeps()
        self.resend.statuses = [429]                                      # definite failure -> release
        with self.assertRaises(service.Retry):
            self.run_msg(m, delivery="msg-1", d=d)
        self.assertEqual(self.run_msg(m, NOW + timedelta(seconds=180), delivery="msg-1", d=d), "sent")
        self.assertEqual(self.run_msg(m, NOW + timedelta(hours=1), delivery="msg-1", d=d), "skipped:duplicate_event")
        self.assertEqual(len(self.resend.deliveries), 1)
        self.assertEqual(len(set(self.sent_links())), 1)


class PinTests(PartnerBase):
    def test_03_two_valid_links_for_one_event_id(self):
        """A parser re-run (same as_of, same fare) enqueues a second message whose link differs."""
        m1, m2 = v2(), v2()
        dest = self.dest(m1)
        m1["aviasales_partner_url"] = partner(dest)
        m2["aviasales_partner_url"] = partner(dest, order=("u", "sub_id", "trs", "marker"))
        self.assertNotEqual(m1["aviasales_partner_url"], m2["aviasales_partner_url"])
        pk = keys.stream_pk(keys.subscriber_id(A, PEPPER), "KHH-TYO", "2027-01")
        self.assertEqual(keys.event_id(pk, m1, "a"), keys.event_id(pk, m2, "b"))      # one fare event
        d = self.pdeps()
        self.resend.statuses = ["lost"]                                  # sent, response lost
        with self.assertRaises(service.Retry):
            self.run_msg(m1, delivery="a", d=d)
        self.assertEqual(self.run_msg(m2, NOW + timedelta(seconds=30), delivery="b", d=d), "sent")
        self.assertEqual(len(self.resend.deliveries), 1)
        self.assertEqual(self.resend.requests[0]["body"], self.resend.requests[1]["body"])   # same payload
        self.assertEqual(self.resend.requests[0]["key"], self.resend.requests[1]["key"])
        self.assertIn(m1["aviasales_partner_url"], self.resend.requests[1]["body"]["text"])
        self.assertNotIn(m2["aviasales_partner_url"], self.resend.requests[1]["body"]["text"])
        # after the commit, either message is a duplicate of the sent event
        self.assertEqual(self.run_msg(m1, NOW + timedelta(hours=2), delivery="a", d=d), "skipped:duplicate_event")

    def test_04_concurrent_claims_for_one_event_id(self):
        dest = self.dest(v2())
        orders = [("marker", "trs", "sub_id", "u"), ("u", "sub_id", "trs", "marker"), ("trs", "marker", "u", "sub_id"),
                  ("sub_id", "u", "marker", "trs")]
        d = self.pdeps()
        outcomes, lock = [], threading.Lock()

        def worker(i):
            m = v2(link=partner(dest, order=orders[i % 4]))
            try:
                r = self.run_msg(m, delivery=f"d{i}", d=d)
            except service.Retry:
                r = "retry"
            with lock:
                outcomes.append(r)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.resend.deliveries), 1, outcomes)
        self.assertEqual(len(set(self.sent_links())), 1, "every attempt of one event sends the same link")
        self.assertTrue(all(o in ("sent", "sent:duplicate_key", "retry", "skipped:duplicate_event", "skipped:deduped")
                            for o in outcomes), outcomes)

    def test_04b_pin_is_a_conditional_first_writer_wins(self):
        d = self.pdeps()
        pk = "v2#pin-test#KHH-TYO#2027-01"
        d.history.claim(pk, None, "e1", NOW, 120)
        self.assertEqual(d.history.pin_link(pk, "e1", "https://x.example.test/1"), "https://x.example.test/1")
        # a second attempt of the same event (re-entered claim) gets the first link
        d.history.claim(pk, d.history.state(pk), "e1", NOW, 120)
        self.assertEqual(d.history.pin_link(pk, "e1", "https://x.example.test/2"), "https://x.example.test/1")
        with self.assertRaises(dedup.history.ClaimConflict):          # not the claimant: never pins
            d.history.pin_link(pk, "e2", "https://x.example.test/3")
        with self.assertRaises(ValueError):
            d.history.pin_link(pk, "e1", None)

    def test_05_resend_result_unknown(self):
        m = v2()
        m["aviasales_partner_url"] = partner(self.dest(m))
        d = self.pdeps()
        self.resend.statuses = ["lost"]
        with self.assertRaises(service.Retry):
            self.run_msg(m, delivery="m", d=d)
        self.assertEqual(self.state()["claim_event"], self.state()["link_event"])    # claim and pin kept
        self.assertEqual(self.run_msg(m, NOW + timedelta(seconds=180), delivery="m", d=d), "sent")
        self.assertEqual(len(self.resend.deliveries), 1)
        self.assertEqual(self.resend.requests[0]["body"], self.resend.requests[1]["body"])

    def test_06_claim_released_then_retried_keeps_the_link_and_new_events_dont_inherit(self):
        m1 = v2()
        dest = self.dest(m1)
        link1, link2 = partner(dest), partner(dest, order=("u", "sub_id", "trs", "marker"))
        m1["aviasales_partner_url"] = link1
        d = self.pdeps()
        self.resend.statuses = [429]                                      # release
        with self.assertRaises(service.Retry):
            self.run_msg(m1, delivery="a", d=d)
        st = self.state()
        self.assertNotIn("claim_event", st)                               # released
        self.assertEqual(st["link_url"], link1)                           # the pin survived
        m2 = v2(link=link2)                                               # same event, other link
        self.assertEqual(self.run_msg(m2, NOW + timedelta(seconds=60), delivery="b", d=d), "sent")
        self.assertIn(link1, self.resend.requests[-1]["body"]["text"])
        self.assertNotIn(link2, self.resend.requests[-1]["body"]["text"])
        # a NEW event (next schedule, lower price) never inherits the old event's link
        m3 = v2(link=link2, as_of="2026-11-21T03:30:00Z", price=5000)
        self.assertEqual(self.run_msg(m3, NOW + timedelta(days=1), delivery="c", d=d), "sent")
        self.assertIn(link2, self.resend.requests[-1]["body"]["text"])
        self.assertEqual(len(self.resend.deliveries), 2)

    def test_pinned_link_is_validated_again_before_use(self):
        m = v2()
        m["aviasales_partner_url"] = partner(self.dest(m))
        d = self.pdeps()
        self.resend.statuses = [429]
        with self.assertRaises(service.Retry):
            self.run_msg(m, delivery="a", d=d)                            # pin, then release
        pk = keys.stream_pk(keys.subscriber_id(A, PEPPER), "KHH-TYO", "2027-01")
        self.hist.update_item(Key={"pk": pk, "sent_at": keys.STATE_SK}, UpdateExpression="SET link_url = :u",
                              ExpressionAttributeValues={":u": "https://evil.example.test/r?u=x"})
        self.assertEqual(self.run_msg(m, NOW + timedelta(seconds=60), delivery="a", d=d),
                         "blocked:partner_link_invalid:unapproved_host_or_path")
        self.assertEqual(len(self.resend.requests), 1)                    # only the earlier 429 attempt
        self.assertNotIn("claim_event", self.state())                     # released, nothing recorded
        self.assertNotIn("last_event_id", self.state())


class BlockedTests(PartnerBase):
    def assert_nothing(self):
        self.assertEqual(self.resend.requests, [])
        self.assertEqual(self.history_rows(), [])                         # no claim, no history

    def test_07_missing_partner_url(self):
        self.assertEqual(self.run_msg(v2(), d=self.pdeps()), "blocked:partner_link_missing")
        self.assert_nothing()

    def test_08_malformed_partner_urls(self):
        m = v2()
        dest = self.dest(m)
        good = partner(dest)
        cases = {
            "http://" + good[len("https://"):]: "not_https",
            good.replace(f"https://{HOST}", f"https://user:pw@{HOST}"): "personal_data_or_token",
            good.replace(HOST, HOST + ":8443"): "port",
            good + "#frag": "fragment",
            good.replace(HOST, HOST.upper()): "host",
            good + "&marker=" + MARKER: "duplicate_query_key",
            good + "&extra=1": "unexpected_query_keys",
            partner(dest, marker="111111"): "marker_mismatch",
            partner(dest, trs="222222"): "trs_mismatch",
            partner(dest, sub_id="other"): "fixed_value_mismatch",
            partner(dest + "x"): "destination_mismatch",
            partner(urllib.parse.quote(dest, safe="")): "destination_mismatch",          # double-encoded
            partner("https://www.aviasales.com/"): "destination_mismatch",               # other destination
            partner("https://attacker.example.test/"): "destination_mismatch",
            partner(dest, sub_id="a@example.com"): "personal_data_or_token",
            good + " ": "non_printable",
            good + "&x=" + "a" * 3000: "too_long",
            "": "missing",
            "not a url": "non_printable",
            f"https:///r?u={dest}": "host",
        }
        for url, reason in cases.items():
            with self.subTest(url=url[:80]):
                self.assertEqual(self.run_msg(v2(link=url), d=self.pdeps()), f"blocked:partner_link_invalid:{reason}")
        self.assertEqual(self.run_msg(v2(link=12345), d=self.pdeps()), "blocked:partner_link_invalid:missing")
        self.assert_nothing()

    def test_09_unapproved_redirect_format(self):
        m = v2()
        dest = self.dest(m)
        # the shipped list is empty: every partner link fails closed
        self.assertEqual(pl.APPROVED_FORMATS, ())
        self.assertEqual(self.run_msg(v2(link=partner(dest)), d=self.pdeps(formats=pl.APPROVED_FORMATS)),
                         "blocked:partner_link_invalid:no_approved_format")
        # a Travelpayouts-looking host is not trusted because of its name
        tp_like = f"https://tp.media/r?marker={MARKER}&u=" + urllib.parse.quote(dest, safe="")
        self.assertEqual(self.run_msg(v2(link=tp_like), d=self.pdeps()),
                         "blocked:partner_link_invalid:unapproved_host_or_path")
        # a short link (opaque destination) is rejected
        self.assertEqual(self.run_msg(v2(link=f"https://{HOST}/abc123"), d=self.pdeps()),
                         "blocked:partner_link_invalid:unapproved_host_or_path")
        self.assertEqual(self.run_msg(v2(link=f"https://{HOST}/r"), d=self.pdeps()),
                         "blocked:partner_link_invalid:unexpected_query_keys")
        self.assert_nothing()

    def test_10_direct_marker_fallback_is_prohibited(self):
        m = v2()
        direct = service.search_links(m, NOW, self.deps()).aviasales.url        # ?marker=000000
        self.assertEqual(self.run_msg(v2(link=direct), d=self.pdeps()),
                         "blocked:partner_link_invalid:unapproved_host_or_path")
        self.assertEqual(self.run_msg(v2(), d=self.pdeps()), "blocked:partner_link_missing")
        self.assertEqual(self.run_msg(v2(link=partner(self.dest(m))), d=self.pdeps(formats=())),
                         "blocked:partner_link_invalid:no_approved_format")
        self.assert_nothing()

    def test_marker_fail_closed_is_preserved(self):
        m = v2(link=partner(self.dest(v2())))
        d = dataclasses.replace(self.pdeps(), marker=None)
        self.assertEqual(self.run_msg(m, d=d), "blocked:no_valid_aviasales_marker")
        self.assert_nothing()

    def test_11_legacy_m1_template_rejected(self):
        m = v2()
        m["aviasales_partner_url"] = partner(self.dest(m))
        self.assertEqual(self.run_msg(m, d=self.pdeps(email_template="m1")), "blocked:legacy_m1_template_disabled")
        self.assert_nothing()

    def test_12_month_mismatch_rejected(self):
        m = v2()
        link = partner(self.dest(m))
        bad = dedup.msg(depart="2027-02-10T08:00:00+08:00", ret="2027-02-15", month="2027-01", as_of=AS_OF)
        bad["aviasales_partner_url"] = link
        with self.assertRaises(keys.InvalidMessage):
            self.run_msg(bad, d=self.pdeps())
        self.assert_nothing()

    def test_disabled_mode_blocks_everything(self):
        m = v2()
        m["aviasales_partner_url"] = partner(self.dest(m))
        self.assertEqual(self.run_msg(m, d=self.deps(affiliate_link_mode="disabled")),
                         "blocked:affiliate_links_disabled")
        self.assert_nothing()


class UnchangedBehaviourTests(PartnerBase):
    def test_13_event_id_ignores_the_partner_link(self):
        pk = keys.stream_pk(keys.subscriber_id(A, PEPPER), "KHH-TYO", "2027-01")
        base = v2()
        ids = {keys.event_id(pk, dict(base, aviasales_partner_url=x), "d")
               for x in (None, "https://a.example.test/1", "https://b.example.test/2")}
        ids.add(keys.event_id(pk, base, "d"))
        self.assertEqual(len(ids), 1)

    def test_13_dedup_rules_are_unchanged_in_partner_mode(self):
        d = self.pdeps()
        m1 = v2()
        dest = self.dest(m1)
        m1["aviasales_partner_url"] = partner(dest)
        self.assertEqual(self.run_msg(m1, d=d), "sent")
        # next schedule, same price, inside the 24 h floor: deduped by the M1 rule, as before
        m2 = v2(link=partner(dest), as_of="2026-11-20T04:00:00Z")
        self.assertEqual(self.run_msg(m2, NOW + timedelta(hours=1), d=d), "skipped:deduped")
        rows = [i for i in self.items() if i["sent_at"] != keys.STATE_SK]
        self.assertEqual(len(rows), 2)                                    # v2 log row + M1 dual-write row

    def test_13_direct_mode_ignores_the_field(self):
        m = v2(link="https://anything.example.test/not-validated")
        self.assertEqual(self.run_msg(m, d=self.deps()), "sent")           # default mode: direct
        text = self.resend.requests[0]["body"]["text"]
        self.assertIn("?marker=000000", text)
        self.assertNotIn("anything.example.test", text)
        self.assertNotIn("link_url", self.state())

    def test_old_message_shapes_are_still_handled(self):
        m1_shape = dedup.msg()                                            # no month, no as_of, no link
        self.assertEqual(self.run_msg(m1_shape), "sent")
        self.assertEqual(self.run_msg(dedup.msg(email=dedup.B), d=self.pdeps()), "blocked:partner_link_missing")


@unittest.skipUnless(dedup.AVAILABLE, "boto3/moto not available")
class ValidatorUnitTests(unittest.TestCase):
    DEST = "https://www.aviasales.com/search/KHH1001TYO15011"

    def check(self, url, **kw):
        args = dict(expected_destination=self.DEST, marker=MARKER, trs=TRS, formats=(test_format(),))
        args.update(kw)
        return pl.check_format(url, **args)

    def test_accepts_only_an_exact_match(self):
        self.assertEqual(self.check(partner(self.DEST)), "synthetic-test")
        with self.assertRaises(pl.PartnerLinkError) as e:
            self.check(partner(self.DEST), trs=None)
        self.assertEqual(e.exception.reason, "trs_mismatch")

    def test_format_list_must_be_enforceable(self):
        f = test_format()
        bad = [
            dataclasses.replace(f, params=(("marker", "marker"),)),                         # no destination
            dataclasses.replace(f, params=(("u", "destination"),)),                         # no marker check
            dataclasses.replace(f, params=f.params + (("v", "destination"),)),              # two destinations
            dataclasses.replace(f, params=f.params + (("x", "anything"),)),
            dataclasses.replace(f, params=f.params + (("x", "fixed"),)),
            dataclasses.replace(f, params=f.params + (("x", ("fixed", "a b")),)),
            dataclasses.replace(f, params=f.params + (("u", "marker"),)),                   # duplicate key
            dataclasses.replace(f, host="https://links.example.test"),
            dataclasses.replace(f, host="LINKS.example.test"),
            dataclasses.replace(f, path="r"),
            dataclasses.replace(f, evidence=""),
        ]
        for fmt in bad:
            with self.subTest(fmt=fmt), self.assertRaises(pl.PartnerLinkError):
                pl.validate_formats((fmt,))
        with self.assertRaises(pl.PartnerLinkError):
            pl.validate_formats((f, f))                                                     # duplicate name
        with self.assertRaises(pl.PartnerLinkError):
            pl.validate_formats([f])
        self.assertEqual(pl.validate_formats(()), ())

    def test_expected_destination_strips_exactly_one_marker(self):
        self.assertEqual(pl.expected_destination(self.DEST + "?marker=000000", "000000"), self.DEST)
        self.assertEqual(pl.expected_destination("https://www.aviasales.com/?marker=000000", "000000"),
                         "https://www.aviasales.com/")
        for bad in (self.DEST, self.DEST + "?marker=1", self.DEST + "?marker=000000&x=1",
                    "https://evil.example.test/?marker=000000", "http://www.aviasales.com/?marker=000000"):
            with self.subTest(bad=bad), self.assertRaises(pl.PartnerLinkError):
                pl.expected_destination(bad, "000000")

    def test_layer_a_api_response_contract(self):
        req = [self.DEST, "https://www.aviasales.com/"]
        ok = {"code": "success", "status": 200,
              "result": {"trs": 0, "marker": 0, "shorten": False,
                         "links": [{"url": req[1], "code": "success", "partner_url": "https://p.example.test/2"},
                                   {"url": req[0], "code": "success", "partner_url": "https://p.example.test/1"}]}}
        ok["result"].update(trs=TRS, marker=MARKER)
        self.assertEqual(pl.check_api_response(ok, req, marker=MARKER, trs=TRS),
                         {req[0]: "https://p.example.test/1", req[1]: "https://p.example.test/2"})

        def mutated(fn):
            b = json.loads(json.dumps(ok))
            fn(b)
            return b
        bad = {
            "api_not_success": mutated(lambda b: b.update(code="error")),
            "api_no_result": mutated(lambda b: b.pop("result")),
            "api_marker_mismatch": mutated(lambda b: b["result"].update(marker="1")),
            "api_trs_mismatch": mutated(lambda b: b["result"].update(trs="1")),
            "api_short_links": mutated(lambda b: b["result"].update(shorten=True)),
            "api_link_count": mutated(lambda b: b["result"]["links"].pop()),
            "api_link_failed": mutated(lambda b: b["result"]["links"][0].update(code="failed")),
            "api_no_partner_url": mutated(lambda b: b["result"]["links"][0].update(partner_url="")),
            "api_unexpected_url": mutated(lambda b: b["result"]["links"][0].update(url=req[0])),
            "api_link_shape": mutated(lambda b: b["result"]["links"].__setitem__(0, "x")),
        }
        for reason, body in bad.items():
            with self.subTest(reason), self.assertRaises(pl.PartnerLinkError) as e:
                pl.check_api_response(body, req, marker=MARKER, trs=TRS)
            self.assertEqual(e.exception.reason, reason)
        with self.assertRaises(pl.PartnerLinkError):
            pl.check_api_response(ok, [f"https://www.aviasales.com/{i}" for i in range(11)], marker=MARKER, trs=TRS)
        with self.assertRaises(pl.PartnerLinkError):
            pl.check_api_response("not json", req, marker=MARKER, trs=TRS)

    def test_mailer_accepts_a_partner_link_only_when_marked_validated(self):
        from notify2 import searchlinks as sl
        av = sl.Link("aviasales", "Aviasales", "https://p.example.test/x", sl.ITINERARY, "primary", True)
        m = dedup.msg()
        with self.assertRaises(mailer.AffiliateLinkUnavailable):
            mailer.render_reference(m, {}, {}, sl.LinkSet((av,), True))
        ok = dataclasses.replace(av, via="partner_api")
        subject, html, text = mailer.render_reference(m, {}, {}, sl.LinkSet((ok,), True))
        self.assertIn("https://p.example.test/x", text)


@unittest.skipUnless(dedup.AVAILABLE, "boto3/moto not available")
class ModeConfigTests(unittest.TestCase):
    def test_modes(self):
        self.assertEqual(config.Settings.from_env({}).affiliate_link_mode, "direct")            # offline default
        self.assertEqual(config.Settings.from_env({}, link_mode_default="disabled").affiliate_link_mode, "disabled")
        self.assertEqual(config.Settings.from_env({"AFFILIATE_LINK_MODE": " Partner_API "}).affiliate_link_mode,
                         "partner_api")
        for bad in ("on", "true", "partner", "direct,partner_api"):
            with self.subTest(bad), self.assertRaises(config.ConfigError):
                config.Settings.from_env({"AFFILIATE_LINK_MODE": bad})

    def test_handler_defaults_to_disabled_and_never_logs_identifiers(self):
        src = (dedup.PKG / "handler.py").read_text(encoding="utf-8")
        self.assertIn('Settings.from_env(link_mode_default="disabled")', src)
        notify_handler = load_handler()
        logs = []
        for mode in ("disabled", "direct", "partner_api"):
            notify_handler.link_mode_notice(mode, log=logs.append)
        self.assertTrue(logs[0].startswith("links: ERROR AFFILIATE_LINK_MODE=disabled"))
        self.assertIn("ticket #257588", logs[1])
        self.assertIn("no partner-link format is approved", logs[2])
        notify_handler.link_mode_notice("partner_api", formats=(test_format(),), log=logs.append)
        self.assertEqual(len(logs), 3)
        out = []
        d = notify_handler.link_deps({"TP_MARKER": "000000", "TP_TRS": "123456"}, log=out.append)
        self.assertEqual(d["partner_trs"], "123456")
        d = notify_handler.link_deps({"TP_MARKER": "000000", "TP_TRS": "12a"}, log=out.append)
        self.assertIsNone(d["partner_trs"])
        self.assertFalse(any("123456" in x or "12a" in x or "000000" in x for x in out), out)

    def test_handler_prints_nothing_secret_at_cold_start(self):
        notify_handler = load_handler()
        buf = io.StringIO()
        with redirect_stdout(buf):
            notify_handler.link_mode_notice("direct")
        self.assertNotIn("000000", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
