"""Phase 2B-1: month-aware notification deduplication (aws/lambdas/flight-fare-notification-v2).

DynamoDB is moto's single-threaded local server (aws/tests/ddb_local.py), so conditional writes
and transactions are atomic as in DynamoDB. Resend is a fake that models idempotency keys. No
real email, no AWS. Run: python3 -m unittest aws/tests/test_m14_notify_dedup.py
"""
import importlib.util
import io
import json
import sys
import threading
import unittest
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
AWS = HERE.parent
sys.path.insert(0, str(HERE))
import ddb_local  # noqa: E402

PKG = AWS / "lambdas" / "flight-fare-notification-v2"
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(AWS / "lambdas" / "flight-fare-check"))

AVAILABLE = ddb_local.available()
if AVAILABLE:
    from notify2 import config, history, keys, mailer, months, rules, service  # noqa: E402

PEPPER = "test-pepper-0123456789abcdef0123456789abcdef"
NOW = datetime(2026, 11, 20, 4, 0, tzinfo=timezone.utc)          # 12:00 Taipei
A, B = "alice@example.com", "bob@example.com"


def msg(email=A, route="KHH-TYO", price=8000, depart="2027-01-10T08:00:00+08:00", ret="2027-01-15T19:00:00+09:00",
        target=9000, month=None, as_of=None, airline="BR"):
    m = {"email": email, "route": route, "plan_name": None, "target_price": target,
         "cheapest": {"price": price, "currency": "TWD", "airline": airline, "depart_date": depart, "return_date": ret}}
    if month:
        m["month"] = month
    if as_of:
        m["as_of"] = as_of
    return m


def month_msg(month, **kw):
    return msg(depart=f"{month}-10T08:00:00+08:00", ret=f"{month}-15T19:00:00+09:00", month=month, **kw)


class FakeResend:
    """Models Resend: an Idempotency-Key replays the first result without sending again (24 h)."""
    def __init__(self, statuses=None, hook=None):
        self.deliveries, self.requests, self.keys = [], [], {}
        self.statuses, self.hook, self.lock = list(statuses or []), hook, threading.Lock()

    def __call__(self, req, timeout):
        body = json.loads(req.data)
        key = dict(req.header_items()).get("Idempotency-key")
        if self.hook:
            self.hook(body)
        with self.lock:
            self.requests.append({"to": body["to"][0], "key": key, "subject": body["subject"], "body": body})
            status = self.statuses.pop(0) if self.statuses else 200
            if status == "timeout":                       # never reached Resend
                raise TimeoutError("timed out")
            if status == "lost":                          # Resend sent it, the response was lost
                if key not in self.keys:
                    self.keys[key] = body
                    self.deliveries.append(body["to"][0])
                raise TimeoutError("timed out")
            if isinstance(status, tuple):
                code, name = status
                raise urllib.error.HTTPError("https://api.resend.com/emails", code, "err", {},
                                             io.BytesIO(json.dumps({"name": name}).encode()))
            if status != 200:
                raise urllib.error.HTTPError("https://api.resend.com/emails", status, "err", {},
                                             io.BytesIO(json.dumps({"name": "x", "message": "to x@example.com"}).encode()))
            if key in self.keys:
                if self.keys[key] != body:
                    raise urllib.error.HTTPError("https://api.resend.com/emails", 409, "conflict", {},
                                                 io.BytesIO(b'{"name":"invalid_idempotent_request"}'))
            else:
                self.keys[key] = body
                self.deliveries.append(body["to"][0])
        r = mock.Mock()
        r.status = 200
        return r


@unittest.skipUnless(AVAILABLE, "boto3/moto not available")
class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Other test modules may leave stub boto3 modules in sys.modules; use the real package.
        for name in [n for n in sys.modules if (n == "boto3" or n.startswith("boto3."))
                     and not getattr(sys.modules[n], "__file__", None)]:
            del sys.modules[name]
        from boto3.session import Session
        cls.client = ddb_local.client()
        s = Session(aws_access_key_id="testing", aws_secret_access_key="testing",
                                  aws_session_token="testing", region_name="ap-southeast-2")
        cls.resource = s.resource("dynamodb", endpoint_url=ddb_local.endpoint())

    def setUp(self):
        sfx = uuid.uuid4().hex[:8]
        self.hist_name, self.subs_name = f"test-notification-history-{sfx}", f"test-subscriptions-{sfx}"
        self.client.create_table(TableName=self.hist_name, BillingMode="PAY_PER_REQUEST",
                                 AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                                       {"AttributeName": "sent_at", "AttributeType": "S"}],
                                 KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                                            {"AttributeName": "sent_at", "KeyType": "RANGE"}])
        self.client.create_table(TableName=self.subs_name, BillingMode="PAY_PER_REQUEST",
                                 AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"},
                                                       {"AttributeName": "route", "AttributeType": "S"}],
                                 KeySchema=[{"AttributeName": "email", "KeyType": "HASH"},
                                            {"AttributeName": "route", "KeyType": "RANGE"}])
        self.hist = self.resource.Table(self.hist_name)
        self.subs = self.resource.Table(self.subs_name)
        self.resend = FakeResend()
        self.logs = []

    def tearDown(self):
        for n in (self.hist_name, self.subs_name):
            self.client.delete_table(TableName=n)

    def deps(self, recheck=False, **settings):
        return service.Deps(settings=config.Settings(recheck_subscription=recheck, **settings),
                            history=history.History(self.hist, self.client, table_name=self.hist_name),
                            pepper=PEPPER, resend_cfg=lambda: {"key": "re_test", "from": "test@example.com"},
                            subscriptions=self.subs if recheck else None, opener=self.resend, log=self.logs.append)

    def run_msg(self, m, at=NOW, delivery=None, d=None, enqueued_at=None):
        return service.process(m, delivery or uuid.uuid4().hex, at, d or self.deps(), enqueued_at=enqueued_at)

    def state(self):
        rows = [i for i in self.items() if i["sent_at"] == "STATE"]
        return rows[0] if rows else None

    def sub(self, email=A, route="KHH-TYO", **kw):
        o, dst = route.split("-")
        item = {"email": email, "route": route, "origin": o, "destination": dst, "target_price": Decimal(9000), **kw}
        self.subs.put_item(Item=item)

    def items(self):
        return self.hist.scan(ConsistentRead=True)["Items"]


class SameStreamTests(Base):
    """1. Same user, same route, same month: M1 suppression rules within the stream."""
    def test_m1_rules_apply_within_a_stream(self):
        m = month_msg("2027-01")
        self.assertEqual(self.run_msg(m), "sent")
        self.assertEqual(self.run_msg(month_msg("2027-01"), NOW + timedelta(hours=1)), "skipped:deduped")
        self.assertEqual(self.run_msg(month_msg("2027-01", price=7900), NOW + timedelta(hours=2)), "skipped:deduped")
        self.assertEqual(self.run_msg(month_msg("2027-01", price=6000), NOW + timedelta(hours=3)), "sent")  # -2,000 TWD
        self.assertEqual(self.run_msg(month_msg("2027-01", price=6000), NOW + timedelta(hours=4)), "skipped:deduped")
        self.assertEqual(self.run_msg(month_msg("2027-01", price=4790), NOW + timedelta(hours=5)), "sent")  # -20.2 %
        self.assertEqual(self.run_msg(month_msg("2027-01", price=4790), NOW + timedelta(hours=29)), "sent")  # >= 24 h
        self.assertEqual(len(self.resend.deliveries), 4)

    def test_absolute_drop_alone_re_alerts(self):
        self.assertEqual(self.run_msg(month_msg("2027-01", price=12000, target=13000)), "sent")
        self.assertEqual(self.run_msg(month_msg("2027-01", price=10001, target=13000), NOW + timedelta(hours=1)),
                         "skipped:deduped")                                   # -1,999 and -16.7 %: no
        self.assertEqual(self.run_msg(month_msg("2027-01", price=10000, target=13000), NOW + timedelta(hours=2)),
                         "sent")                                              # -2,000 (-16.7 %): yes, M1 absolute rule

    def test_rule_parity_with_m1(self):
        m1 = load_m1_notification()
        s = config.Settings()
        cases = []
        for hours in (0, 1, 23.99, 24, 30):
            for prev, price in ((10000, 10000), (10000, 8001), (10000, 8000), (10000, 7999), (5000, 3001),
                                (5000, 3000), (3000, 2500), (12000, 10500)):
                last = {"sent_at": (NOW - timedelta(hours=hours)).isoformat(timespec="milliseconds"), "price": Decimal(prev)}
                cases.append((last, Decimal(price)))
        cases.append((None, Decimal(5000)))
        with mock.patch.dict("os.environ", {"NOTIFY_FLOOR_HOURS": "24", "REALERT_PCT": "20", "REALERT_ABS_TWD": "2000"}):
            for last, price in cases:
                self.assertEqual(rules.should_send(last, price, NOW, s), m1.should_send(last, price, NOW), (last, price))


class MonthStreamTests(Base):
    """2. Same user and route, different months: independent streams."""
    def test_jan_feb_mar_are_independent(self):
        for mth in ("2027-01", "2027-02", "2027-03"):
            self.assertEqual(self.run_msg(month_msg(mth)), "sent", mth)
        self.assertEqual(len(self.resend.deliveries), 3)
        for mth in ("2027-01", "2027-02", "2027-03"):                     # each month now suppressed on its own
            self.assertEqual(self.run_msg(month_msg(mth), NOW + timedelta(hours=1)), "skipped:deduped")
        state_pks = sorted(i["pk"] for i in self.items() if i["sent_at"] == "STATE")
        self.assertEqual([p.split("#")[-1] for p in state_pks], ["2027-01", "2027-02", "2027-03"])

    def test_one_month_dedup_does_not_touch_another(self):
        self.run_msg(month_msg("2027-01"))
        self.assertEqual(self.run_msg(month_msg("2027-02"), NOW + timedelta(minutes=5)), "sent")


class UserIsolationTests(Base):
    """3. Different users, same route and month: no cross-suppression; no email in keys."""
    def test_users_are_independent(self):
        self.assertEqual(self.run_msg(month_msg("2027-01", email=A)), "sent")
        self.assertEqual(self.run_msg(month_msg("2027-01", email=B)), "sent")
        self.assertEqual(self.run_msg(month_msg("2027-01", email=A), NOW + timedelta(hours=1)), "skipped:deduped")
        self.assertEqual(sorted(self.resend.deliveries), [A, B])

    def test_keys_never_contain_the_address(self):
        self.run_msg(month_msg("2027-01"), d=self.deps(legacy_dual_write=False))
        for item in self.items():
            self.assertNotIn("@", json.dumps(item, default=str))
            self.assertTrue(item["pk"].startswith("v2#s1_"))
        self.assertEqual(keys.subscriber_id(" Alice@Example.com ", PEPPER), keys.subscriber_id(A, PEPPER))
        self.assertNotEqual(keys.subscriber_id(A, PEPPER), keys.subscriber_id(A, PEPPER[::-1]))
        with self.assertRaises(ValueError):
            keys.subscriber_id(A, "short")


class LegacyRollingTests(Base):
    """4. Legacy rolling-month subscriptions: M1-shape messages (no month field)."""
    def test_m1_shape_message_uses_the_departure_month(self):
        m = msg(depart="2026-12-05T08:00:00+08:00", ret="2026-12-09T19:00:00+09:00")
        self.assertEqual(keys.resolve_month(m), ("2026-12", "depart_date"))
        self.assertEqual(self.run_msg(m), "sent")
        self.assertTrue(any(i["pk"].endswith("#KHH-TYO#2026-12") for i in self.items()))

    def test_v2_message_month_wins_and_invalid_months_are_dropped(self):
        self.assertEqual(keys.resolve_month(month_msg("2027-02")), ("2027-02", "message"))
        for bad in (dict(msg(), month="2027-13"), dict(msg(), cheapest={"price": 1}), {"email": A}):
            with self.assertRaises(keys.InvalidMessage):
                service.process(bad, "id", NOW, self.deps())

    def test_parser_v2_messages_carry_the_scheduler_month(self):
        from farecheck import config as fc_config, effects, runner, travelpayouts as tp
        body = {"success": True, "currency": "twd", "data": [{"price": 8000, "airline": "BR", "origin": "KHH",
                "destination": "TYO", "departure_at": "2026-12-10T08:00:00+08:00", "return_at": "2026-12-15T19:00:00+09:00"}]}
        rows = [{"email": A, "route": "KHH-TYO", "target_price": Decimal(9000)}]
        sent = []
        sqs = mock.Mock()
        sqs.send_message.side_effect = lambda **kw: sent.append(json.loads(kw["MessageBody"]))
        for version in (1, 2):
            sent.clear()
            st = fc_config.Settings(mode="live", fare_api="v3", fetch_usd=False, queue_url="https://sqs.example/q",
                                    message_version=version)
            runner.run_parser({"origin": "KHH", "destination": "TYO", "month": "2026-12", "as_of": "2026-11-20T04:00:00Z"},
                              settings=st, catalog=fc_config.load_catalog(), token="t" * 32,
                              reader=effects.ListReader(rows), now=NOW, transport=lambda u, h, t: body,
                              client_factory=lambda name: sqs, log=lambda m: None)
            self.assertEqual(len(sent), 1)
            if version == 1:
                self.assertNotIn("month", sent[0])          # M1 shape unchanged by default
            else:
                self.assertEqual((sent[0]["month"], sent[0]["as_of"]), ("2026-12", "2026-11-20T04:00:00Z"))
        self.assertEqual(self.run_msg(sent[0]), "sent")
        _ = tp


class MonthEndTests(Base):
    """5. Month-end transitions use the scheduler's as_of (Asia/Taipei), not the Lambda's clock."""
    BEFORE = "2026-11-30T15:59:59Z"                                     # 23:59:59 Taipei
    AFTER = datetime(2026, 11, 30, 16, 0, 1, tzinfo=timezone.utc)      # 00:00:01 Taipei

    def test_message_from_2359_processed_after_midnight(self):
        self.sub()                                                      # rolling row
        m = month_msg("2026-12", as_of=self.BEFORE)
        self.assertEqual(self.run_msg(m, self.AFTER, d=self.deps(recheck=True)), "sent")
        self.assertTrue(any(i["pk"].endswith("#2026-12") for i in self.items()))

    def test_next_run_after_midnight_is_the_next_month_stream(self):
        self.sub()
        self.run_msg(month_msg("2026-12", as_of=self.BEFORE), self.AFTER, d=self.deps(recheck=True))
        m = month_msg("2027-01", as_of="2026-11-30T16:30:00Z")
        self.assertEqual(self.run_msg(m, self.AFTER + timedelta(minutes=30), d=self.deps(recheck=True)), "sent")
        # a big drop passes the December rule, but the rolling row now means January
        self.assertEqual(self.run_msg(month_msg("2026-12", price=5000, as_of="2026-11-30T16:30:00Z"), self.AFTER,
                                      d=self.deps(recheck=True)), "skipped:month_changed")
        self.assertEqual(len(self.resend.deliveries), 2)

    def test_m1_shape_message_after_midnight_is_not_sent_for_the_old_month(self):
        self.sub()
        m = msg(depart="2026-12-10T08:00:00+08:00", ret="2026-12-15T19:00:00+09:00")   # no as_of
        self.assertEqual(self.run_msg(m, self.AFTER, d=self.deps(recheck=True)), "skipped:month_changed")
        self.assertEqual(self.resend.deliveries, [])


class EligibilityTests(Base):
    """6-7. Paused and expired subscriptions (re-checked just before sending)."""
    def test_paused(self):
        self.sub(departure_month="2027-01", alerts_paused=True)
        self.assertEqual(self.run_msg(month_msg("2027-01"), d=self.deps(recheck=True)), "skipped:paused")
        self.assertEqual(self.resend.deliveries, [])
        state = [i for i in self.items() if i["sent_at"] == "STATE"][0]
        self.assertNotIn("claim_event", state)                          # claim released
        self.sub(departure_month="2027-01")                             # resumed -> sends at once
        self.assertEqual(self.run_msg(month_msg("2027-01"), d=self.deps(recheck=True)), "sent")

    def test_expired(self):
        self.sub(departure_month="2026-10")
        m = month_msg("2026-10", as_of="2026-11-20T03:00:00Z")
        self.assertEqual(self.run_msg(m, d=self.deps(recheck=True)), "skipped:expired")

    def test_deleted_edited_or_target_raised_rows(self):
        self.assertEqual(self.run_msg(month_msg("2027-01"), d=self.deps(recheck=True)), "skipped:subscription_gone")
        self.sub(departure_month="2027-02")
        self.assertEqual(self.run_msg(month_msg("2027-01"), d=self.deps(recheck=True)), "skipped:month_changed")
        self.sub(departure_month="2027-01", target_price=Decimal(7000))
        self.assertEqual(self.run_msg(month_msg("2027-01"), d=self.deps(recheck=True)), "skipped:target_changed")
        self.assertEqual(self.resend.deliveries, [])


class NoDataTests(Base):
    """8. API no-data responses produce no message, so no dedup state and no email."""
    def test_no_data_sends_nothing(self):
        from farecheck import config as fc_config, effects, runner
        sqs = mock.Mock()
        out = runner.run_parser({"origin": "KHH", "destination": "TYO", "month": "2027-01", "as_of": "2026-11-20T04:00:00Z"},
                                settings=fc_config.Settings(mode="live", fare_api="v3", queue_url="https://sqs.example/q",
                                                            message_version=2),
                                catalog=fc_config.load_catalog(), token="t" * 32,
                                reader=effects.ListReader([{"email": A, "route": "KHH-TYO", "target_price": 9000}]),
                                now=NOW, transport=lambda u, h, t: {"success": True, "currency": "twd", "data": []},
                                client_factory=lambda name: sqs, log=lambda m: None)
        self.assertEqual((out["fare_check"]["kind"], out["matched"]), ("no_data", 0))
        sqs.send_message.assert_not_called()
        self.assertEqual(self.items(), [])


class ConcurrencyTests(Base):
    """9. Concurrent Lambda invocations on one stream."""
    def test_second_worker_during_a_send_is_told_to_retry(self):
        results = {}

        def during_send(body):
            if "b" not in results:
                try:
                    results["b"] = self.run_msg(month_msg("2027-01", price=7999))
                except service.Retry as e:
                    results["b"] = f"retry: {e}"
        self.resend.hook = during_send
        self.assertEqual(self.run_msg(month_msg("2027-01")), "sent")
        self.assertTrue(results["b"].startswith("retry"))
        self.resend.hook = None
        self.assertEqual(self.run_msg(month_msg("2027-01", price=7999), NOW + timedelta(seconds=30)), "skipped:deduped")
        self.assertEqual(len(self.resend.deliveries), 1)

    def test_parallel_invocations_send_exactly_once(self):
        outcomes, lock = [], threading.Lock()

        def worker(i):
            try:
                r = self.run_msg(month_msg("2027-01", price=8000 - i), delivery=f"d{i}")
            except service.Retry:
                r = "retry"
            with lock:
                outcomes.append(r)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(outcomes.count("sent"), 1, outcomes)
        self.assertEqual(len(self.resend.deliveries), 1)
        self.assertTrue(all(o in ("sent", "retry", "skipped:deduped") for o in outcomes))

    def test_stale_read_cannot_claim(self):
        d = self.deps()
        pk = keys.stream_pk(keys.subscriber_id(A, PEPPER), "KHH-TYO", "2027-01")
        d.history.claim(pk, None, "e1", NOW, 120)
        stale = d.history.state(pk)
        d.history.release(pk, "e1")
        d.history.claim(pk, d.history.state(pk), "e2", NOW, 120)       # version moved on
        with self.assertRaises(history.ClaimConflict):
            d.history.claim(pk, stale, "e3", NOW + timedelta(hours=1), 120)

    def test_crashed_claim_expires_before_redelivery(self):
        d = self.deps()
        pk = keys.stream_pk(keys.subscriber_id(A, PEPPER), "KHH-TYO", "2027-01")
        d.history.claim(pk, None, "crashed", NOW, 120)                  # worker died without releasing
        with self.assertRaises(service.Retry):
            self.run_msg(month_msg("2027-01"), NOW + timedelta(seconds=60))
        self.assertEqual(self.run_msg(month_msg("2027-01"), NOW + timedelta(seconds=180)), "sent")   # visibility 180 s

    def test_claim_lost_after_send_is_still_recorded(self):
        d = self.deps()
        real_commit = d.history.commit

        def steal_then_commit(**kw):
            d.history.table.update_item(Key={"pk": kw["pk"], "sent_at": keys.STATE_SK},
                                        UpdateExpression="SET claim_event = :o", ExpressionAttributeValues={":o": "other"})
            return real_commit(**kw)
        d.history.commit = steal_then_commit
        self.assertEqual(self.run_msg(month_msg("2027-01"), d=d), "sent:claim_lost")
        state = [i for i in self.items() if i["sent_at"] == "STATE"][0]
        self.assertEqual(state["last_price"], 8000)
        self.assertEqual(self.run_msg(month_msg("2027-01"), NOW + timedelta(hours=1)), "skipped:deduped")


class DuplicateDeliveryTests(Base):
    """10. Duplicate SQS deliveries and reprocessing of the same fare event."""
    def test_same_message_twice(self):
        m = month_msg("2027-01")
        self.assertEqual(self.run_msg(m, delivery="msg-1"), "sent")
        self.assertEqual(self.run_msg(m, NOW + timedelta(hours=30), delivery="msg-1"), "skipped:duplicate_event")
        self.assertEqual(len(self.resend.deliveries), 1)

    def test_parser_rerun_of_one_schedule_is_one_event(self):
        m = month_msg("2027-01", as_of="2026-11-20T03:30:00Z")
        self.assertEqual(self.run_msg(m, delivery="a"), "sent")
        self.assertEqual(self.run_msg(m, NOW + timedelta(hours=30), delivery="b"), "skipped:duplicate_event")

    def test_crash_between_send_and_commit_does_not_send_twice(self):
        d = self.deps()
        real_commit, calls = d.history.commit, []

        def crash_once(**kw):
            if not calls:
                calls.append(1)
                raise RuntimeError("Lambda killed after Resend accepted the email")
            return real_commit(**kw)
        d.history.commit = crash_once
        m = month_msg("2027-01")
        with self.assertRaises(RuntimeError):
            self.run_msg(m, delivery="msg-9", d=d)
        # the claim is still held: redelivery after the visibility timeout reuses the same Resend key
        self.assertEqual(self.run_msg(m, NOW + timedelta(seconds=180), delivery="msg-9", d=d), "sent")
        self.assertEqual(len(self.resend.deliveries), 1)                 # Resend replayed, didn't resend
        self.assertEqual(len(self.resend.requests), 2)
        self.assertEqual(self.resend.requests[0]["key"], self.resend.requests[1]["key"])

    def test_resend_outcomes(self):
        self.resend.statuses = [429]
        with self.assertRaises(service.Retry):
            self.run_msg(month_msg("2027-01"))
        self.resend.statuses = [422]
        self.assertEqual(self.run_msg(month_msg("2027-01")), "dropped:resend")
        self.assertEqual([i for i in self.items() if i["sent_at"] != "STATE"], [])   # nothing recorded
        self.assertEqual(self.run_msg(month_msg("2027-01")), "sent")                  # not suppressed by failures
        self.assertEqual(mailer.classify(409, '{"name":"invalid_idempotent_request"}'), mailer.DUPLICATE)
        self.assertEqual(mailer.classify(409, '{"name":"concurrent_idempotent_requests"}'), mailer.UNKNOWN)
        self.assertEqual(mailer.classify(500, ""), mailer.UNKNOWN)
        self.assertEqual(mailer.classify(None, "TimeoutError"), mailer.UNKNOWN)
        self.assertEqual(mailer.classify(429, ""), mailer.RETRY)


class IdempotencyTests(Base):
    """Resend idempotency and the remaining delivery edges."""
    def test_event_ids_are_deterministic(self):
        pk = keys.stream_pk(keys.subscriber_id(A, PEPPER), "KHH-TYO", "2027-01")
        m = month_msg("2027-01")
        self.assertEqual(keys.event_id(pk, m, "d1"), keys.event_id(pk, json.loads(json.dumps(m)), "d1"))
        self.assertNotEqual(keys.event_id(pk, m, "d1"), keys.event_id(pk, m, "d2"))          # no as_of: per delivery
        v2 = month_msg("2027-01", as_of="2026-11-20T03:30:00Z")
        self.assertEqual(keys.event_id(pk, v2, "d1"), keys.event_id(pk, v2, "d2"))         # as_of: per schedule
        self.assertNotEqual(keys.event_id(pk, v2, "d1"), keys.event_id(pk, dict(v2, cheapest=dict(v2["cheapest"], price=1)), "d1"))
        self.assertLessEqual(len(keys.idempotency_key(keys.event_id(pk, m, "d1"))), 256)

    def test_response_lost_after_send_redelivery_does_not_resend(self):
        self.resend.statuses = ["lost"]
        m = month_msg("2027-01")
        with self.assertRaises(service.Retry):
            self.run_msg(m, delivery="msg-1", enqueued_at=NOW)
        self.assertEqual(self.state()["claim_event"], self.resend_key_event())          # claim kept
        other = month_msg("2027-01", price=7000)
        with self.assertRaises(service.Retry):                                            # other events wait
            self.run_msg(other, NOW + timedelta(seconds=60), delivery="msg-2")
        self.assertEqual(self.run_msg(m, NOW + timedelta(seconds=60), delivery="msg-1"), "sent")   # re-enters its claim
        self.assertEqual(len(self.resend.deliveries), 1)
        self.assertEqual(self.resend.requests[0]["body"], self.resend.requests[-1]["body"])      # same payload
        self.assertEqual(self.resend.requests[0]["key"], self.resend.requests[-1]["key"])

    def resend_key_event(self):
        return self.resend.requests[-1]["key"].split("/", 1)[1]

    def test_timeout_before_reaching_resend_then_success(self):
        self.resend.statuses = ["timeout"]
        with self.assertRaises(service.Retry):
            self.run_msg(month_msg("2027-01"), delivery="m")
        self.assertEqual(self.run_msg(month_msg("2027-01"), NOW + timedelta(seconds=180), delivery="m"), "sent")
        self.assertEqual(len(self.resend.deliveries), 1)

    def test_409_concurrent_keeps_the_claim_and_retries(self):
        self.resend.statuses = [(409, "concurrent_idempotent_requests")]
        with self.assertRaises(service.Retry):
            self.run_msg(month_msg("2027-01"), delivery="m")
        self.assertIn("claim_event", self.state())
        self.assertEqual(self.run_msg(month_msg("2027-01"), NOW + timedelta(seconds=180), delivery="m"), "sent")

    def test_409_invalid_idempotent_request_is_recorded_as_sent(self):
        self.resend.statuses = [(409, "invalid_idempotent_request")]
        self.assertEqual(self.run_msg(month_msg("2027-01")), "sent:duplicate_key")
        self.assertEqual(self.resend.deliveries, [])                        # nothing new went out
        self.assertEqual(self.run_msg(month_msg("2027-01"), NOW + timedelta(hours=1)), "skipped:deduped")

    def test_retry_after_the_key_window_never_sends_again(self):
        self.resend.statuses = ["lost"]
        m = month_msg("2027-01")
        with self.assertRaises(service.Retry):
            self.run_msg(m, delivery="msg-1", enqueued_at=NOW)
        late = NOW + timedelta(hours=21)                                     # > MAX_MESSAGE_AGE_HOURS (20)
        self.assertEqual(self.run_msg(m, late, delivery="msg-1", enqueued_at=NOW), "recorded:unconfirmed")
        self.assertEqual(len(self.resend.requests), 1)                       # no second request at all
        self.assertEqual(self.run_msg(month_msg("2027-01"), late + timedelta(hours=1)), "skipped:deduped")

    def test_stale_messages_are_never_sent(self):
        self.assertEqual(self.run_msg(month_msg("2027-01"), NOW + timedelta(hours=21), enqueued_at=NOW), "skipped:stale")
        self.assertEqual(self.run_msg(month_msg("2027-01", as_of="2026-11-19T06:00:00Z")), "skipped:stale")
        self.assertEqual(self.resend.requests, [])
        with self.assertRaises(config.ConfigError):
            config.Settings(max_message_age_hours=Decimal("24"))

    def test_crash_then_subscription_paused_is_recorded_not_dropped(self):
        self.sub(departure_month="2027-01")
        d = self.deps(recheck=True)
        real_commit, calls = d.history.commit, []

        def crash_once(**kw):
            if not calls:
                calls.append(1)
                raise RuntimeError("killed after send")
            return real_commit(**kw)
        d.history.commit = crash_once
        with self.assertRaises(RuntimeError):
            self.run_msg(month_msg("2027-01"), delivery="m", d=d)
        self.sub(departure_month="2027-01", alerts_paused=True)
        self.assertEqual(self.run_msg(month_msg("2027-01"), NOW + timedelta(seconds=180), delivery="m", d=d),
                         "recorded:unconfirmed")
        self.assertEqual(len(self.resend.deliveries), 1)


class SqsHandlerTests(Base):
    def test_time_budget_returns_unstarted_messages(self):
        import handler as h
        left = iter([60_000, 20_000, 10_000, 5_000])
        recs = [{"messageId": f"m{i}", "body": json.dumps(month_msg(f"2027-0{i + 1}")),
                 "attributes": {"SentTimestamp": str(int(NOW.timestamp() * 1000))}} for i in range(4)]
        out = h.handle({"Records": recs}, self.deps(), now_fn=lambda: NOW, remaining_ms=lambda: next(left))
        self.assertEqual(out, {"batchItemFailures": [{"itemIdentifier": "m2"}, {"itemIdentifier": "m3"}]})
        self.assertEqual(len(self.resend.deliveries), 2)

    def test_sent_timestamp_drives_staleness(self):
        import handler as h
        old = {"messageId": "old", "body": json.dumps(month_msg("2027-01")),
               "attributes": {"SentTimestamp": str(int((NOW - timedelta(hours=21)).timestamp() * 1000))}}
        self.assertEqual(h.handle({"Records": [old]}, self.deps(), now_fn=lambda: NOW), {"batchItemFailures": []})
        self.assertEqual(self.resend.requests, [])
        self.assertIsNone(h.sent_timestamp({"attributes": {"SentTimestamp": "x"}}))


class LegacyHistoryTests(Base):
    """11. Existing M1 notification_history rows. They carry no departure month, so they are
    treated as covering every month of the route (switch-over can't resend within 24 h)."""
    def m1_row(self, hours_ago, price=8000, email=A, route="KHH-TYO", **override):
        t = (NOW - timedelta(hours=hours_ago)).isoformat(timespec="milliseconds")
        if isinstance(price, int):
            price = Decimal(price)
        item = {"pk": f"{email}#{route}", "sent_at": t, "email": email, "route": route, "price": price}
        item.update(override)
        self.hist.put_item(Item={k: v for k, v in item.items() if v is not None})

    def test_m1_rows_hold_no_month_information(self):
        m1 = load_m1_notification()
        m1._hist, m1._cfg = self.hist, {"key": "re_test", "from": "test@example.com"}
        with mock.patch.object(m1.urllib.request, "urlopen", self.resend):
            m1.process(msg(route="TPE-TYO"), NOW)
        row = self.hist.query(KeyConditionExpression="pk = :p", ExpressionAttributeValues={":p": f"{A}#TPE-TYO"})["Items"][0]
        self.assertEqual(set(row), {"pk", "sent_at", "email", "route", "price"})        # nothing about the month
        self.assertEqual(row["sent_at"], "2026-11-20T04:00:00.000+00:00")

    def test_recent_m1_alert_suppresses_every_month_of_the_route(self):
        self.m1_row(2)
        for mth in ("2026-12", "2027-01", "2027-06"):
            self.assertEqual(self.run_msg(month_msg(mth)), "skipped:deduped", mth)
        self.assertEqual(self.run_msg(month_msg("2027-01", route="KHH-OKA")), "sent")    # other route
        self.assertEqual(self.run_msg(month_msg("2027-01", price=5000)), "sent")         # M1 price-drop rule
        self.assertEqual(self.resend.deliveries, [A, A])

    def test_switch_over_never_resends_within_24_hours(self):
        for hours in (0.01, 1, 6, 12, 23.9):
            self.setUp()
            self.m1_row(hours, price=8000)
            for mth in ("2026-12", "2027-01"):
                for price in (8000, 7900, 6500):                                          # no 20 % / 2,000 drop
                    self.assertEqual(self.run_msg(month_msg(mth, price=price)), "skipped:deduped", (hours, mth, price))
            self.tearDown()
        self.setUp()
        self.m1_row(24.01)
        self.assertEqual(self.run_msg(month_msg("2026-12")), "sent")

    def test_old_m1_alert_or_other_user_does_not_suppress(self):
        self.m1_row(25)
        self.m1_row(1, email=B)
        self.assertEqual(self.run_msg(month_msg("2026-12")), "sent")

    def test_ambiguous_m1_rows(self):
        # no price: the 24 h floor still applies, and no price-drop re-alert against an unknown price
        self.m1_row(2, price=None)
        self.assertEqual(self.run_msg(month_msg("2027-01", price=3000)), "skipped:deduped")
        self.setUp()
        self.m1_row(2, price="not-a-number")
        self.assertEqual(self.run_msg(month_msg("2027-01", price=3000)), "skipped:deduped")

    def test_rows_m1_cannot_produce_are_ignored(self):
        for bad in ("garbage-but-sorts-high", "2026-11-20T03:00:00.000"):                  # unparseable / no offset
            self.setUp()
            self.hist.put_item(Item={"pk": f"{A}#KHH-TYO", "sent_at": bad, "email": A, "route": "KHH-TYO",
                                     "price": Decimal(8000)})
            self.assertEqual(self.run_msg(month_msg("2027-01")), "sent", bad)
            self.tearDown()
        self.setUp()

    def test_v2_copies_never_suppress_another_month(self):
        self.assertEqual(self.run_msg(month_msg("2027-01")), "sent")          # dual-writes an M1-shape copy
        self.assertEqual(self.run_msg(month_msg("2026-12"), NOW + timedelta(minutes=1)), "sent")

    def test_genuine_m1_row_behind_a_newer_v2_copy_is_still_found(self):
        self.m1_row(3)                                                        # genuine M1 alert
        self.m1_row(1, source="v2", month="2027-02")                         # newer v2 copy on the same pk
        self.assertEqual(self.run_msg(month_msg("2027-03")), "skipped:deduped")

    def test_same_millisecond_sends_for_two_months(self):
        d = self.deps()
        self.assertEqual(self.run_msg(month_msg("2027-01"), d=d), "sent")
        self.assertEqual(self.run_msg(month_msg("2027-02"), d=d), "sent")     # same NOW: no claim_lost

    def test_legacy_read_can_be_disabled(self):
        self.m1_row(2)
        self.assertEqual(self.run_msg(month_msg("2026-12"), d=self.deps(legacy_read=False)), "sent")


class MigrationRollbackTests(Base):
    """12. Switching M1 -> v2 -> M1 -> v2 never resends within the M1 window."""
    def m1(self):
        m1 = load_m1_notification()
        m1._hist, m1._cfg = self.hist, {"key": "re_test", "from": "test@example.com"}
        return m1

    def m1_process(self, m1, m, at):
        with mock.patch.object(m1.urllib.request, "urlopen", self.resend):
            m1.process(m, at)

    def test_rollback_to_m1_after_v2_send(self):
        m = msg(depart="2026-12-10T08:00:00+08:00", ret="2026-12-15T19:00:00+09:00")
        self.assertEqual(self.run_msg(m), "sent")
        legacy = [i for i in self.items() if i["pk"] == f"{A}#KHH-TYO"]
        self.assertEqual(len(legacy), 1)                                  # dual-written M1 row
        self.assertTrue({"pk", "sent_at", "email", "route", "price"} <= set(legacy[0]))   # what M1 reads
        self.assertEqual((legacy[0]["source"], legacy[0]["month"]), ("v2", "2026-12"))
        self.m1_process(self.m1(), m, NOW + timedelta(hours=1))          # rolled back to the M1 Lambda
        self.assertEqual(len(self.resend.deliveries), 1)

    def test_without_dual_write_a_rollback_would_resend(self):
        m = msg(depart="2026-12-10T08:00:00+08:00", ret="2026-12-15T19:00:00+09:00")
        self.run_msg(m, d=self.deps(legacy_dual_write=False))
        self.m1_process(self.m1(), m, NOW + timedelta(hours=1))
        self.assertEqual(len(self.resend.deliveries), 2)                  # why dual-write stays on during the window

    def test_roll_forward_after_m1_sent_during_rollback(self):
        m = msg(depart="2026-12-10T08:00:00+08:00", ret="2026-12-15T19:00:00+09:00")
        self.run_msg(m)                                                     # v2 at T
        self.m1_process(self.m1(), dict(m, cheapest=dict(m["cheapest"], price=5000)), NOW + timedelta(hours=25))  # M1 at T+25h
        self.assertEqual(self.run_msg(m, NOW + timedelta(hours=26)), "skipped:deduped")   # v2 sees the newer M1 row
        self.assertEqual(len(self.resend.deliveries), 2)

    def test_rollback_and_roll_forward_with_several_months(self):
        self.assertEqual(self.run_msg(month_msg("2026-12")), "sent")
        self.assertEqual(self.run_msg(month_msg("2027-01"), NOW + timedelta(minutes=5)), "sent")    # independent months
        m1 = self.m1()
        rolling = msg(depart="2026-12-10T08:00:00+08:00", ret="2026-12-15T19:00:00+09:00")
        self.m1_process(m1, rolling, NOW + timedelta(hours=2))      # rolled back: M1 suppresses the route
        self.assertEqual(len(self.resend.deliveries), 2)
        self.m1_process(m1, rolling, NOW + timedelta(hours=25))     # M1's own 24 h rule then allows one
        self.assertEqual(len(self.resend.deliveries), 3)
        # rolled forward within 24 h of that M1 alert: no month of the route resends
        for mth in ("2026-12", "2027-01", "2027-02"):
            self.assertEqual(self.run_msg(month_msg(mth), NOW + timedelta(hours=26)), "skipped:deduped", mth)
        self.assertEqual(self.run_msg(month_msg("2027-02"), NOW + timedelta(hours=49, minutes=1)), "sent")

    def test_existing_m1_rows_are_never_modified(self):
        t = (NOW - timedelta(hours=30)).isoformat(timespec="milliseconds")
        original = {"pk": f"{A}#KHH-TYO", "sent_at": t, "email": A, "route": "KHH-TYO", "price": Decimal(9000)}
        self.hist.put_item(Item=original)
        self.run_msg(msg(depart="2026-12-10T08:00:00+08:00", ret="2026-12-15T19:00:00+09:00"))
        self.assertIn(original, self.items())


class HandlerAndSafetyTests(Base):
    def test_handler_batch_semantics(self):
        import handler as h
        d = self.deps()
        recs = [{"messageId": "ok", "body": json.dumps(month_msg("2027-01"))},
                {"messageId": "bad", "body": json.dumps({"email": "x"})},
                {"messageId": "notjson", "body": "{"}]
        out = h.handle({"Records": recs}, d, now_fn=lambda: NOW)
        self.assertEqual(out, {"batchItemFailures": [{"itemIdentifier": "notjson"}]})
        self.assertFalse(any("@" in line for line in self.logs))

    def test_handler_fails_closed_without_pepper(self):
        import handler as h
        with mock.patch.object(h, "deps", side_effect=RuntimeError("ParameterNotFound")):
            out = h.handler({"Records": [{"messageId": "m1", "body": json.dumps(month_msg("2027-01"))}]}, None)
        self.assertEqual(out, {"batchItemFailures": [{"itemIdentifier": "m1"}]})
        self.assertEqual(self.resend.deliveries, [])

    def test_render_is_unchanged_from_m1(self):
        m1 = load_m1_notification()
        for m in (month_msg("2027-01"), dict(msg(), cheapest_usd={"price": 260}), msg(airline=None)):
            self.assertEqual(mailer.render(m, m1.CITY, m1.AIRLINES), m1.render(m))

    def test_settings_guard_claim_window(self):
        with self.assertRaises(config.ConfigError):
            config.Settings(claim_seconds=200)
        with self.assertRaises(config.ConfigError):
            config.Settings(claim_seconds=20)
        self.assertEqual(config.Settings.from_env({}).floor_hours, Decimal("24"))

    def test_months_parity(self):
        spec = importlib.util.spec_from_file_location("fc_months", AWS / "lambdas" / "flight-fare-check" / "farecheck" / "months.py")
        fc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fc)
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        while t < datetime(2028, 1, 1, tzinfo=timezone.utc):
            for dep in (None, "2026-05", "2027-12"):
                self.assertEqual(months.effective_month(dep, t), fc.effective_month(dep, t))
                self.assertEqual(months.is_expired(dep, t), fc.is_expired(dep, t))
            t += timedelta(hours=11, minutes=7)


class PackagingTests(unittest.TestCase):
    def test_zip_is_deterministic_and_m1_source_unchanged(self):
        import hashlib
        import tempfile
        import zipfile
        spec = importlib.util.spec_from_file_location("pkg_n2", AWS / "scripts" / "package_notification_v2.py")
        pkg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pkg)
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            r1, r2 = pkg.build(Path(a)), pkg.build(Path(b))
            names = zipfile.ZipFile(r1["zip"]).namelist()
        self.assertEqual(r1["sha256"], r2["sha256"])
        self.assertIn("handler.py", names)
        self.assertIn("notify2/service.py", names)
        self.assertFalse(any("test" in n for n in names))
        m1 = (AWS / "lambdas" / "flight-fare-notification" / "index.py").read_bytes()
        self.assertEqual(hashlib.sha256(m1).hexdigest(), "f1ccdf0292c03c6a595381ab027e0a893a2d5a5e9487b991496de311c80c969f")


def load_m1_notification():
    with mock.patch.dict("os.environ", dict(ddb_local.FAKE_ENV, RESEND_PARAM="/flight/resend", CITIES="{}",
                                            AIRLINES='{"BR": "長榮航空"}')):
        spec = importlib.util.spec_from_file_location(f"m1_notify_{uuid.uuid4().hex[:6]}",
                                                      AWS / "lambdas" / "flight-fare-notification" / "index.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    return m


if __name__ == "__main__":
    unittest.main()
