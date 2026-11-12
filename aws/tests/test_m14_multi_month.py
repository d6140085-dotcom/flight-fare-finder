"""R-MM-1: several departure months per route — identity, fare-check selection and the
rolling/fixed overlap policy (docs/m1.4-r-mm-1-multi-month.md).

Offline. The fare-check part is pure (fixture rows, a fake Travelpayouts transport, a fake SQS).
The notification part runs the v2 service on moto's local DynamoDB server with a fake Resend
(test_m14_notify_dedup.Base). No AWS, no email. Run:
    python3 -m unittest aws/tests/test_m14_multi_month.py
"""
import json
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
AWS = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(AWS / "lambdas" / "flight-fare-check"))
sys.path.insert(0, str(AWS / "lambdas" / "flight-subscriptions-api"))

from farecheck import config as fc_config  # noqa: E402
from farecheck import effects, matching, runner, subkeys as fc_subkeys, workitems  # noqa: E402
from subsapi import subkeys as api_subkeys  # noqa: E402

import test_m14_notify_dedup as dedup  # noqa: E402

CATALOG = fc_config.load_catalog()
VECTORS = json.loads((HERE / "vectors" / "subscription-ids.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 11, 20, 4, 0, tzinfo=timezone.utc)  # 12:00 Taipei; rolling rows -> 2026-12
A, B = "alice@example.com", "bob@example.com"


def row(email=A, key="KHH-TYO", target=9000, **kw):
    parts = fc_subkeys.parse_key(key)
    r = {"email": email, "route": key, "origin": parts[0], "destination": parts[1], "target_price": Decimal(target)}
    if parts[2]:
        r["departure_month"] = parts[2]
    r.update(kw)
    return r


class Offer:
    def __init__(self, price):
        self.price = price

    def message_dict(self):
        return {"price": self.price, "currency": "TWD", "airline": "BR",
                "depart_date": "2026-12-10T08:00:00+08:00", "return_date": "2026-12-15T19:00:00+09:00"}


ITEM = workitems.WorkItem("KHH", "TYO", "2026-12")


# =============================================================================================
# Identity
# =============================================================================================
class IdentityTests(unittest.TestCase):
    def test_every_lambda_has_the_same_identity_module(self):
        copies = [AWS / "lambdas" / p / "subkeys.py" for p in
                  ("flight-subscriptions-api/subsapi", "flight-fare-check/farecheck", "flight-fare-notification-v2/notify2")]
        texts = {c.read_bytes() for c in copies}
        self.assertEqual(len(texts), 1, "subkeys.py copies differ")

    def test_shared_vectors(self):
        """The same file drives the TypeScript tests (src/lib/m14/subscription-id.test.ts)."""
        for case in VECTORS["valid"]:
            o, d, m = case["origin"], case["destination"], case["departure_month"]
            key = api_subkeys.sort_key(o, d, m)
            self.assertEqual((key, api_subkeys.public_id(key)), (case["sort_key"], case["id"]), case)
            self.assertEqual(api_subkeys.key_from_id(case["id"]), case["sort_key"])
            self.assertEqual(api_subkeys.parse_key(case["sort_key"]), (o, d, m))
            self.assertEqual(api_subkeys.pair_of_key(case["sort_key"]), f"{o}-{d}")
        for bad in VECTORS["invalid_ids"]:
            self.assertIsNone(api_subkeys.key_from_id(bad), bad)
        for bad in VECTORS["invalid_keys"]:
            self.assertIsNone(api_subkeys.parse_key(bad), bad)

    def test_ids_are_url_safe_and_reversible(self):
        import urllib.parse
        for case in VECTORS["valid"]:
            self.assertEqual(urllib.parse.quote(case["id"], safe=""), case["id"])  # never percent-encoded
            self.assertNotIn("#", case["id"])


# =============================================================================================
# Fare-check selection
# =============================================================================================
class SelectionTests(unittest.TestCase):
    def test_key_and_attributes_must_agree(self):
        ok = row(key="KHH-TYO#2027-01")
        self.assertEqual(workitems.classify(ok, NOW, CATALOG)[0], workitems.ACTIVE)
        for bad in (row(key="KHH-TYO#2027-01", departure_month="2027-02"),     # key month != attribute
                    row(key="KHH-TYO#2027-01", departure_month=None),
                    row(key="KHH-TYO#2027-01", origin="TPE"),                  # key pair != attribute
                    {**row(), "route": "KHH-TYO#2027"}, {**row(), "route": "KHH-TYO#"}):
            self.assertEqual(workitems.classify(bad, NOW, CATALOG)[0], workitems.INVALID, bad)

    def test_three_months_of_one_route_are_three_work_items(self):
        rows = [row(key=f"KHH-TYO#{m}") for m in ("2027-01", "2027-02", "2027-03")]
        plan = workitems.build(rows, NOW, CATALOG)
        self.assertEqual([i.key for i in plan.items], ["KHH-TYO#2027-01", "KHH-TYO#2027-02", "KHH-TYO#2027-03"])

    def test_rolling_and_fixed_of_the_same_month_share_one_work_item(self):
        rows = [row(), row(key="KHH-TYO#2026-12", target=8000), row(B, key="KHH-TYO#2026-12")]
        self.assertEqual([i.key for i in workitems.build(rows, NOW, CATALOG).items], ["KHH-TYO#2026-12"])

    def test_readers_return_every_row_of_the_pair_and_nothing_else(self):
        rows = [row(), row(key="KHH-TYO#2027-01"), row(key="KHH-OKA#2027-01"),
                row(key="TPE-TYO#2027-01"), row(B, key="KHH-TYO#2027-02")]
        got = sorted(r["route"] for r in effects.ListReader(rows).rows_for_route("KHH-TYO"))
        self.assertEqual(got, ["KHH-TYO", "KHH-TYO#2027-01", "KHH-TYO#2027-02"])
        table = mock.Mock()
        table.scan.return_value = {"Items": []}
        list(effects.TableReader(table).rows_for_route("KHH-TYO"))
        kw = table.scan.call_args.kwargs
        self.assertEqual(kw["FilterExpression"], "#r = :r OR begins_with(#r, :p)")
        self.assertEqual(kw["ExpressionAttributeValues"], {":r": "KHH-TYO", ":p": "KHH-TYO#"})

    def test_only_the_items_month_matches(self):
        rows = [row(key=f"KHH-TYO#{m}") for m in ("2026-12", "2027-01")] + [row()]
        matched, skipped = matching.match(rows, ITEM, Offer(8000), NOW, CATALOG)
        self.assertEqual(sorted(r["route"] for r in matched), ["KHH-TYO", "KHH-TYO#2026-12"])
        self.assertEqual(skipped["other_month"], 1)


class OverlapMatchingTests(unittest.TestCase):
    """The rolling/fixed overlap policy, fare-check side (policy §4 a–e)."""

    def msgs(self, rows, price, as_of=NOW, item=ITEM):
        matched, _ = matching.match(rows, item, Offer(price), as_of, CATALOG)
        return matching.messages(matched, item, Offer(price), None, as_of)

    def test_a_both_eligible_one_message_listing_both(self):
        out = self.msgs([row(target=9000), row(key="KHH-TYO#2026-12", target=8500)], 8000)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["subscription_keys"], ["KHH-TYO", "KHH-TYO#2026-12"])
        self.assertEqual(out[0]["target_price"], 8500)  # the strictest target that the fare meets
        self.assertEqual((out[0]["month"], out[0]["route"]), ("2026-12", "KHH-TYO"))

    def test_b_targets_differ_the_met_one_alerts(self):
        rows = [row(target=9000), row(key="KHH-TYO#2026-12", target=7000)]
        out = self.msgs(rows, 8000)  # meets the rolling target only
        self.assertEqual([(m["subscription_keys"], m["target_price"]) for m in out], [(["KHH-TYO"], 9000)])
        out = self.msgs([row(target=7000), row(key="KHH-TYO#2026-12", target=9000)], 8000)
        self.assertEqual([(m["subscription_keys"], m["target_price"]) for m in out], [(["KHH-TYO#2026-12"], 9000)])
        self.assertEqual(self.msgs(rows, 9500), [])  # neither target met: no alert

    def test_c_month_end_moves_only_the_rolling_row(self):
        before = datetime(2026, 11, 30, 15, 59, tzinfo=timezone.utc)  # 23:59 Taipei: rolling -> 2026-12
        after = datetime(2026, 11, 30, 16, 1, tzinfo=timezone.utc)    # 00:01 Taipei: rolling -> 2027-01
        rows = [row(), row(key="KHH-TYO#2026-12"), row(key="KHH-TYO#2027-01")]
        dec, jan = ITEM, workitems.WorkItem("KHH", "TYO", "2027-01")
        self.assertEqual(self.msgs(rows, 8000, before, dec)[0]["subscription_keys"], ["KHH-TYO", "KHH-TYO#2026-12"])
        self.assertEqual(self.msgs(rows, 8000, before, jan)[0]["subscription_keys"], ["KHH-TYO#2027-01"])
        self.assertEqual(self.msgs(rows, 8000, after, dec)[0]["subscription_keys"], ["KHH-TYO#2026-12"])
        self.assertEqual(self.msgs(rows, 8000, after, jan)[0]["subscription_keys"], ["KHH-TYO", "KHH-TYO#2027-01"])
        # The scheduler's work items at each instant: the set of months is consistent per run.
        self.assertEqual([i.month for i in workitems.build(rows, before, CATALOG).items], ["2026-12", "2027-01"])
        self.assertEqual([i.month for i in workitems.build(rows, after, CATALOG).items], ["2026-12", "2027-01"])

    def test_d_paused_or_expired_never_suppress_the_other(self):
        for other in (row(key="KHH-TYO#2026-12", alerts_paused=True, target=99999),):
            out = self.msgs([row(), other], 8000)
            self.assertEqual(out[0]["subscription_keys"], ["KHH-TYO"])
        out = self.msgs([row(alerts_paused=True), row(key="KHH-TYO#2026-12")], 8000)
        self.assertEqual(out[0]["subscription_keys"], ["KHH-TYO#2026-12"])
        out = self.msgs([row(alerts_paused=True), row(key="KHH-TYO#2026-12", alerts_paused=True)], 8000)
        self.assertEqual(out, [])
        expired = row(key="KHH-TYO#2026-10")  # expired at NOW; also another month
        self.assertEqual(self.msgs([row(), expired], 8000)[0]["subscription_keys"], ["KHH-TYO"])

    def test_e_users_never_merge(self):
        out = self.msgs([row(), row(B, key="KHH-TYO#2026-12")], 8000)
        self.assertEqual([(m["email"], m["subscription_keys"]) for m in out],
                         [(A, ["KHH-TYO"]), (B, ["KHH-TYO#2026-12"])])

    def test_v1_messages_keep_the_m1_shape(self):
        matched, _ = matching.match([row(), row(key="KHH-TYO#2026-12", target=8500)], ITEM, Offer(8000), NOW, CATALOG)
        (body,) = matching.messages(matched, ITEM, Offer(8000), None, None)
        self.assertEqual(set(body), {"email", "route", "plan_name", "target_price", "cheapest"})
        self.assertEqual(body["target_price"], 8500)

    def test_parser_publishes_one_message_per_user_and_stream(self):
        body = {"success": True, "currency": "twd", "data": [{"price": 8000, "airline": "BR", "origin": "KHH",
                "destination": "TYO", "departure_at": "2026-12-10T08:00:00+08:00", "return_at": "2026-12-15T19:00:00+09:00"}]}
        rows = [row(), row(key="KHH-TYO#2026-12", target=8500), row(key="KHH-TYO#2027-01"), row(B, key="KHH-TYO#2026-12")]
        sent, sqs = [], mock.Mock()
        sqs.send_message.side_effect = lambda **kw: sent.append(json.loads(kw["MessageBody"]))
        st = fc_config.Settings(mode="live", fare_api="v3", fetch_usd=False, queue_url="https://sqs.example/q",
                                message_version=2)
        out = runner.run_parser({"origin": "KHH", "destination": "TYO", "month": "2026-12", "as_of": "2026-11-20T04:00:00Z"},
                                settings=st, catalog=CATALOG, token="t" * 32, reader=effects.ListReader(rows), now=NOW,
                                transport=lambda u, h, t: body, client_factory=lambda name: sqs, log=lambda m: None)
        self.assertEqual(out["matched"], 2)
        self.assertEqual([(m["email"], m["subscription_keys"], m["target_price"]) for m in sent],
                         [(A, ["KHH-TYO", "KHH-TYO#2026-12"], 8500), (B, ["KHH-TYO#2026-12"], 9000)])


# =============================================================================================
# Notification side (v2 service on local DynamoDB)
# =============================================================================================
@unittest.skipUnless(dedup.AVAILABLE, "boto3/moto not available")
class OverlapNotificationTests(dedup.Base):
    """Policy §4 on the notification side: one alert per (user, route, effective month) under the
    M1 re-alert rule; sent if ANY listed subscription is still eligible at send time."""

    def sub_key(self, key, email=A, target=9000, **kw):
        parts = fc_subkeys.parse_key(key)
        item = {"email": email, "route": key, "origin": parts[0], "destination": parts[1],
                "target_price": Decimal(target), **kw}
        if parts[2]:
            item["departure_month"] = parts[2]
        self.subs.put_item(Item=item)

    def m(self, keys_, target=9000, price=8000, month="2026-12", **kw):
        out = dedup.month_msg(month, target=target, price=price, as_of="2026-11-20T04:00:00Z", **kw)
        out["subscription_keys"] = keys_
        return out

    def sent_texts(self):
        return [r["body"]["text"] for r in self.resend.requests]

    def test_both_eligible_one_email(self):
        self.sub_key("KHH-TYO")
        self.sub_key("KHH-TYO#2026-12", target=8500)
        out = self.run_msg(self.m(["KHH-TYO", "KHH-TYO#2026-12"], target=8500), d=self.deps(recheck=True))
        self.assertEqual(out, "sent")
        self.assertEqual(len(self.resend.deliveries), 1)
        self.assertIn("你的目標價：NT$8,500", self.sent_texts()[0])

    def test_strictest_one_paused_since_the_check_still_sends_with_the_other_target(self):
        self.sub_key("KHH-TYO", target=9000)
        self.sub_key("KHH-TYO#2026-12", target=8500, alerts_paused=True)
        out = self.run_msg(self.m(["KHH-TYO", "KHH-TYO#2026-12"], target=8500), d=self.deps(recheck=True))
        self.assertEqual(out, "sent")  # a valid alert is never suppressed by the paused row
        self.assertIn("你的目標價：NT$9,000", self.sent_texts()[0])

    def test_all_listed_rows_ineligible_skips_and_records_nothing(self):
        self.sub_key("KHH-TYO", alerts_paused=True)
        self.sub_key("KHH-TYO#2026-12", target=7000)  # target lowered below the fare since the check
        out = self.run_msg(self.m(["KHH-TYO", "KHH-TYO#2026-12"]), d=self.deps(recheck=True))
        self.assertEqual(out, "skipped:paused+target_changed")
        self.assertEqual(self.resend.deliveries, [])
        st = self.state()
        self.assertTrue(st is None or not st.get("last_sent_at"), st)  # no success entry

    def test_deleted_rows_and_another_users_keys(self):
        self.sub_key("KHH-TYO#2026-12", email=B)  # Bob's row with the same key
        out = self.run_msg(self.m(["KHH-TYO#2026-12"]), d=self.deps(recheck=True))
        self.assertEqual(out, "skipped:subscription_gone")  # keys are read in Alice's partition only

    def test_second_message_for_the_same_stream_is_deduped(self):
        """Two separate messages for one user, route and month (e.g. an older fare check before
        the consolidation): one email, by the stream's M1 rule — intentional, not silent."""
        self.sub_key("KHH-TYO")
        self.sub_key("KHH-TYO#2026-12", target=8500)
        self.assertEqual(self.run_msg(self.m(["KHH-TYO"])), "sent")
        later = NOW.replace(minute=5)
        self.assertEqual(self.run_msg(self.m(["KHH-TYO#2026-12"], target=8500, price=7900), at=later), "skipped:deduped")
        # ... but a real drop (>= 20 % or NT$2,000) re-alerts, as M1 does.
        self.assertEqual(self.run_msg(self.m(["KHH-TYO#2026-12"], target=8500, price=6000), at=later.replace(minute=10)),
                         "sent")
        self.assertEqual(len(self.resend.deliveries), 2)

    def test_other_months_of_the_route_are_independent_streams(self):
        for mth in ("2026-12", "2027-01", "2027-02"):
            self.sub_key(f"KHH-TYO#{mth}")
            self.assertEqual(self.run_msg(self.m([f"KHH-TYO#{mth}"], month=mth)), "sent", mth)
        self.assertEqual(len(self.resend.deliveries), 3)

    def test_month_end_rolling_row_starts_a_new_stream(self):
        self.sub_key("KHH-TYO")
        self.sub_key("KHH-TYO#2026-12")
        d = self.deps(recheck=True)
        self.assertEqual(self.run_msg(self.m(["KHH-TYO", "KHH-TYO#2026-12"]), d=d), "sent")
        # After midnight Taipei on 1 Dec the rolling row resolves to January: a January fare for it
        # is a different stream, not suppressed by December's alert.
        after = datetime(2026, 11, 30, 16, 5, tzinfo=timezone.utc)
        jan = self.m(["KHH-TYO"], month="2027-01")
        jan["as_of"] = "2026-11-30T16:01:00Z"
        self.assertEqual(self.run_msg(jan, at=after, d=d), "sent")
        # A message still listing the rolling row for December (fare check from before midnight,
        # sent after) is checked against the row's month at the scheduler's as_of: still December.
        late = self.m(["KHH-TYO"], price=6000)
        late["as_of"] = "2026-11-30T15:59:00Z"
        self.assertEqual(self.run_msg(late, at=after.replace(minute=7), d=d), "sent")

    def test_invalid_keys_are_dropped_before_any_write(self):
        for keys_ in (["TPE-TYO#2026-12"], ["KHH-TYO#2027-01"], ["KHH-TYO", "KHH-TYO"], [], "KHH-TYO",
                      ["KHH-TYO#2026-1"], [f"KHH-TYO#2027-{i:02d}" for i in range(1, 12)]):
            with self.assertRaises(dedup.keys.InvalidMessage, msg=keys_):
                self.run_msg(self.m(keys_))
        self.assertEqual(self.items(), [])
        self.assertEqual(self.resend.deliveries, [])

    def test_messages_without_keys_still_recheck_the_legacy_key(self):
        self.sub_key("KHH-TYO", alerts_paused=True)
        m = dedup.month_msg("2026-12", as_of="2026-11-20T04:00:00Z")
        self.assertEqual(self.run_msg(m, d=self.deps(recheck=True)), "skipped:paused")


if __name__ == "__main__":
    unittest.main()
