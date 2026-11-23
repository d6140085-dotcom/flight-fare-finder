"""B6-1 revision: the v2 notification rejects a message whose fare departs outside its month.

A message that names its subscription month (`month`, set by the M1.4 scheduler) must carry a
fare departing in that month, year and month both. Otherwise keys.resolve_month raises
keys.InvalidMessage before the service reads history, renders, calls Resend or writes anything,
and the SQS handler drops it (no batch-item failure, so no retry). M1-shape messages (no month)
are unchanged.

Fakes only: DynamoDB Local / moto via test_m14_notify_dedup.Base and its FakeResend. Fixed dates.
Run: python3 -m unittest aws/tests/test_m14_notify_month_guard.py

The file name sorts after test_m14_notify_dedup.py on purpose: test_lambdas.py and
test_m14_phase0.py stub boto3, and test_m14_notify_dedup.Base swaps the stubs back for the real
package. If real boto3 were first imported before those stubs (as a test_b6_* name would do), the
M1 notification tests in test_m14_notify_dedup would mix two boto3 imports.
"""
import importlib
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# Reuse the very module object the test loader already uses (aws.tests.test_m14_notify_dedup when
# run as `python -m unittest aws/tests/test_*.py`), never a second copy under another name: a copy
# would re-import boto3 behind the other tests' backs.
dedup = importlib.import_module(f"{__package__}.test_m14_notify_dedup" if __package__ else "test_m14_notify_dedup")

if dedup.AVAILABLE:
    from notify2 import keys, service  # noqa: E402

NOW = dedup.NOW                                  # 2026-11-20 12:00 Taipei
A = dedup.A
MISSING = object()                               # depart_date key absent


def message(month, depart, ret=None):
    m = dedup.msg(depart=depart, ret=ret or "2026-12-15", month=month,
                  as_of=NOW.strftime("%Y-%m-%dT%H:%M:%SZ"))
    if depart is MISSING:
        del m["cheapest"]["depart_date"]
    return m


@unittest.skipUnless(dedup.AVAILABLE, "boto3/moto not available")
class MonthGuardTests(dedup.Base):
    def setUp(self):
        super().setUp()
        self.renders = []
        real_render = service.render

        def spy(m, now, deps, t):
            self.renders.append(m)
            return real_render(m, now, deps, t)
        self._patch = mock.patch.object(service, "render", spy)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def assert_rejected(self, m, why):
        d = self.deps(recheck=True)
        self.sub(departure_month=m.get("month"))
        before = self.subs.scan(ConsistentRead=True)["Items"]
        with self.assertRaises(keys.InvalidMessage) as ctx:
            service.process(m, "delivery-1", NOW, d)
        self.assertIn(why, str(ctx.exception))
        self.assertEqual(self.renders, [])                       # no email rendered
        self.assertEqual(self.resend.requests, [])               # Resend never called
        self.assertEqual(self.items(), [])                       # no claim, STATE, log or M1 row
        self.assertEqual(self.subs.scan(ConsistentRead=True)["Items"], before)   # subscription untouched

    # --- the rule ---------------------------------------------------------------------------

    def test_resolve_month_compares_year_and_month(self):
        self.assertEqual(keys.resolve_month(message("2026-12", "2026-12-31T23:30:00+08:00")), ("2026-12", "message"))
        self.assertEqual(keys.resolve_month(message("2027-01", "2027-01-01")), ("2027-01", "message"))
        for month, dep, why in (("2026-12", "2027-01-01T00:10:00+08:00", "outside the message month"),
                                ("2026-12", "2027-12-10T08:00:00+08:00", "outside the message month"),
                                ("2027-01", "2026-12-31", "outside the message month"),
                                ("2026-12", "bad", "malformed"),
                                ("2026-12", "2026-13-01", "malformed"),
                                ("2026-12", "2026-02-30", "malformed"),
                                ("2026-12", "20261210", "malformed"),
                                ("2026-12", None, "malformed"),
                                ("2026-12", MISSING, "malformed")):
            with self.subTest(month=month, dep=dep), self.assertRaises(keys.InvalidMessage) as ctx:
                keys.resolve_month(message(month, dep))
            self.assertIn(why, str(ctx.exception))

    # --- end to end through service.process -------------------------------------------------

    def test_valid_month_is_sent(self):
        d = self.deps(recheck=True)
        self.sub(departure_month="2026-12")                     # re-read before sending (recheck on)
        m = message("2026-12", "2026-12-10T08:00:00+08:00", "2026-12-15T19:00:00+09:00")
        self.assertEqual(service.process(m, "delivery-1", NOW, d), "sent")
        self.assertEqual(len(self.resend.requests), 1)
        self.assertEqual(len(self.renders), 1)
        self.assertTrue(any(i["pk"].endswith("#KHH-TYO#2026-12") for i in self.items()))

    def test_wrong_month_is_rejected_before_anything_happens(self):
        self.assert_rejected(message("2026-12", "2027-01-10T08:00:00+08:00", "2027-01-15T19:00:00+09:00"),
                             "outside the message month")

    def test_wrong_year_same_month_number_is_rejected(self):
        self.assert_rejected(message("2026-12", "2027-12-10T08:00:00+08:00", "2027-12-15T19:00:00+09:00"),
                             "outside the message month")

    def test_malformed_departure_date_is_rejected(self):
        for dep in ("bad", "2026-12-32", None, MISSING):
            with self.subTest(dep=dep):
                self.tearDown()
                self.setUp()
                self.assert_rejected(message("2026-12", dep, "2026-12-15"), "malformed")

    def test_legacy_message_without_month_is_unchanged(self):
        m = dedup.msg(depart="2026-12-05T08:00:00+08:00", ret="2026-12-09T19:00:00+09:00")
        self.assertNotIn("month", m)
        self.assertEqual(keys.resolve_month(m), ("2026-12", "depart_date"))
        self.assertEqual(service.process(m, "delivery-1", NOW, self.deps()), "sent")
        self.assertEqual(len(self.resend.requests), 1)
        # unchanged: a legacy message with no usable date has no month at all and was already invalid
        with self.assertRaises(keys.InvalidMessage):
            keys.resolve_month(dedup.msg(depart="bad"))

    # --- SQS: dropped, never retried --------------------------------------------------------

    def test_handler_drops_without_retry(self):
        import handler as h
        bad = message("2026-12", "2027-12-10T08:00:00+08:00", "2027-12-15T19:00:00+09:00")
        good = message("2027-01", "2027-01-10T08:00:00+08:00", "2027-01-15T19:00:00+09:00")
        ts = str(int(NOW.timestamp() * 1000))
        recs = [{"messageId": "bad", "body": json.dumps(bad), "attributes": {"SentTimestamp": ts}},
                {"messageId": "good", "body": json.dumps(good), "attributes": {"SentTimestamp": ts}}]
        d = self.deps()
        for attempt in range(2):                                 # a redelivery is dropped the same way
            out = h.handle({"Records": recs[:1] if attempt else recs}, d, now_fn=lambda: NOW)
            self.assertEqual(out, {"batchItemFailures": []})     # nothing returned to SQS for retry
        self.assertEqual(len(self.resend.requests), 1)           # only the valid message was sent
        self.assertEqual([m["month"] for m in self.renders], ["2027-01"])
        self.assertEqual(sum("dropped bad: invalid message (fare departs outside the message month)" in x
                             for x in self.logs), 2)
        self.assertFalse(any(i["pk"].endswith("#2026-12") for i in self.items()))
        for x in self.logs:
            self.assertNotIn(A, x)                               # no address in logs


if __name__ == "__main__":
    unittest.main()
