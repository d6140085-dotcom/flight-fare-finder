"""Offline concurrency tests for the M1.4 strict quota protocol (aws/reference/subscriptions_quota.py).

Every interleaving of the concurrent requests is explored; after each, the invariants must hold.
No AWS calls. Run: python3 -m unittest discover -s aws/tests -v
"""
import random
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reference"))
import subscriptions_quota as m  # noqa: E402

EMAIL = "user@example.com"
MAX = 10
DESTS = ["OSA", "NGO", "FUK", "OKA", "SPK", "KMJ", "MNL", "PUS", "CJU", "HKG", "MFM", "BKK"]


def row(dest, **extra):
    return {"origin": "TPE", "destination": dest, "target_price": 9000, "departure_month": "2027-01", **extra}


def seed(n, *, with_quota=True, statuses=("active",)):
    """A user with n records cycling through the given statuses (all count toward the limit)."""
    store = m.Store()
    for i, dest in enumerate(DESTS[:n]):
        status = statuses[i % len(statuses)]
        extra = {"alerts_paused": True} if status == "paused" else {}
        if status == "expired":
            extra["departure_month"] = "2026-01"
        store.subs[(EMAIL, f"TPE-{dest}")] = row(dest, **extra)
    if with_quota:
        store.quota[EMAIL] = {"count": n, "version": 7}
    return store


class Invariants:
    def check(self, store, max_subs=MAX):
        rows = store.query(EMAIL)
        self.assertLessEqual(len(rows), max_subs, f"limit exceeded: {sorted(rows)}")
        q = store.get_quota(EMAIL)
        if q is not None:
            self.assertEqual(q["count"], len(rows), "quota count drifted from the true count")


class StoreTests(unittest.TestCase):
    def test_transaction_is_all_or_nothing(self):
        store = seed(1)
        with self.assertRaises(m.TransactionCanceled) as e:
            store.transact([("put_sub", (EMAIL, "TPE-TYO"), row("TYO")), ("quota", EMAIL, 2, 6)])
        self.assertEqual(e.exception.index, 1)
        self.assertNotIn((EMAIL, "TPE-TYO"), store.subs)

    def test_backoff_is_bounded(self):
        rng = random.Random(1)
        waits = [m.backoff(a, rng) for a in range(1, 20)]
        self.assertTrue(all(0 <= w <= m.BACKOFF_CAP_S for w in waits))
        self.assertEqual(m.MAX_ATTEMPTS, 3)


class SequentialTests(Invariants, unittest.TestCase):
    def run1(self, gen):
        return m.run_schedule([gen], [])[0]

    def test_total_limit_counts_paused_and_expired(self):
        store = seed(10, statuses=("active", "paused", "expired"))
        self.assertEqual(self.run1(m.create(store, EMAIL, "TPE-TYO", row("TYO"))), (409, "limit_reached"))
        self.check(store)

    def test_pause_and_resume_never_touch_quota(self):
        store = seed(10)
        before = store.get_quota(EMAIL)
        self.assertEqual(self.run1(m.update(store, EMAIL, "TPE-OSA", {"alerts_paused": True}))[0], 200)
        self.assertEqual(self.run1(m.create(store, EMAIL, "TPE-TYO", row("TYO"))), (409, "limit_reached"))
        self.assertEqual(self.run1(m.update(store, EMAIL, "TPE-OSA", {"alerts_paused": False}))[0], 200)
        self.assertEqual(store.get_quota(EMAIL), before)

    def test_delete_frees_exactly_one_slot(self):
        store = seed(10)
        self.assertEqual(self.run1(m.delete(store, EMAIL, "TPE-OSA")), (204, None))
        self.assertEqual(self.run1(m.create(store, EMAIL, "TPE-TYO", row("TYO"))), (201, "TPE-TYO"))
        self.assertEqual(self.run1(m.create(store, EMAIL, "TPE-TAK", row("TAK"))), (409, "limit_reached"))
        self.check(store)

    def test_m1_user_without_quota_record(self):
        store = seed(2, with_quota=False)
        self.assertEqual(self.run1(m.create(store, EMAIL, "TPE-TYO", row("TYO")))[0], 201)
        self.assertEqual(store.get_quota(EMAIL), {"count": 3, "version": 1})
        store = seed(2, with_quota=False)
        self.assertEqual(self.run1(m.delete(store, EMAIL, "TPE-OSA"))[0], 204)
        self.assertEqual(store.get_quota(EMAIL), {"count": 1, "version": 1})

    def test_legacy_subscribe_upserts_existing_and_quota_checks_new(self):
        store = seed(9)
        store.subs[(EMAIL, "TPE-TYO")] = row("TYO", plan_name="tokyo", departure_month=None)
        store.quota[EMAIL] = {"count": 10, "version": 3}
        self.assertEqual(self.run1(m.legacy_subscribe(store, EMAIL, "tokyo", 8000)), (200, "TPE-TYO"))
        self.assertEqual(store.subs[(EMAIL, "TPE-TYO")]["target_price"], 8000)
        self.assertEqual(store.get_quota(EMAIL)["version"], 3)  # update: no quota write
        self.assertEqual(self.run1(m.legacy_subscribe(store, EMAIL, "seoul", 8000)), (409, "limit_reached"))
        self.check(store)


class ConcurrencyTests(Invariants, unittest.TestCase):
    def explore(self, make, n_ops, sample=None):
        """Every interleaving; with ``sample``, that many seeded random ones (3-request cases)."""
        outcomes = Counter()
        schedules = m.random_schedules(make, n_ops, sample) if sample else m.all_schedules(make, n_ops)
        for store, results in schedules:
            self.check(store)
            self.assertNotIn(None, results)
            outcomes[tuple(results)] += 1
        self.assertGreater(sum(outcomes.values()), 1)
        return outcomes

    def test_two_creates_at_nine_only_one_wins(self):
        def make():
            s = seed(9)
            return s, [m.create(s, EMAIL, "TPE-TYO", row("TYO")), m.create(s, EMAIL, "TPE-TAK", row("TAK"))]

        for results in self.explore(make, 2):
            self.assertEqual(sorted(r[0] for r in results), [201, 409], results)
            loser = next(r for r in results if r[0] == 409)
            self.assertIn(loser[1], {"limit_reached", "conflict"})

    def test_three_creates_at_eight_with_no_quota_record(self):
        def make():
            s = seed(8, with_quota=False)
            return s, [m.create(s, EMAIL, f"TPE-{d}", row(d)) for d in ("TYO", "TAK", "OIT")]

        seen = self.explore(make, 3, sample=3000)
        for results in seen:
            self.assertLessEqual(sum(r[0] == 201 for r in results), 2)
        self.assertTrue(any(sum(r[0] == 201 for r in results) == 2 for results in seen))

    def test_same_route_created_twice(self):
        def make():
            s = seed(3)
            return s, [m.create(s, EMAIL, "TPE-TYO", row("TYO")), m.create(s, EMAIL, "TPE-TYO", row("TYO"))]

        for results in self.explore(make, 2):
            self.assertEqual(sorted(r[0] for r in results), [201, 409])
            self.assertIn("duplicate_subscription", [r[1] for r in results])

    def test_create_and_delete_at_limit(self):
        def make():
            s = seed(10)
            return s, [m.create(s, EMAIL, "TPE-TYO", row("TYO")), m.delete(s, EMAIL, "TPE-OSA")]

        seen = self.explore(make, 2)
        for results in seen:
            self.assertEqual(results[1], (204, None))
        # Depending on order the create either sees the freed slot or is refused; never both rows extra.
        self.assertTrue({r[0][0] for r in seen} <= {201, 409})

    def test_route_edit_races_create_onto_same_route(self):
        def make():
            s = seed(5)
            return s, [m.change_route(s, EMAIL, "TPE-OSA", "TPE-TYO", row("TYO")),
                       m.create(s, EMAIL, "TPE-TYO", row("TYO"))]

        for results in self.explore(make, 2):
            winners = [r for r in results if r[0] in (200, 201)]
            self.assertEqual(len(winners), 1, results)

    def test_route_edit_at_limit_does_not_need_a_slot(self):
        def make():
            s = seed(10)
            return s, [m.change_route(s, EMAIL, "TPE-OSA", "TPE-TYO", row("TYO")),
                       m.create(s, EMAIL, "TPE-TAK", row("TAK"))]

        for results in self.explore(make, 2):
            self.assertEqual(results[0], (200, "TPE-TYO"))
            self.assertEqual(results[1], (409, "limit_reached"))

    def test_pause_concurrent_with_create_and_delete(self):
        def make():
            s = seed(9)
            return s, [m.update(s, EMAIL, "TPE-OSA", {"alerts_paused": True}),
                       m.create(s, EMAIL, "TPE-TYO", row("TYO")),
                       m.delete(s, EMAIL, "TPE-NGO")]

        for results in self.explore(make, 3):
            self.assertEqual(results[0][0], 200)
            self.assertEqual(results[2], (204, None))

    def test_pause_races_delete_of_same_row(self):
        def make():
            s = seed(4)
            return s, [m.update(s, EMAIL, "TPE-OSA", {"alerts_paused": True}), m.delete(s, EMAIL, "TPE-OSA")]

        for results in self.explore(make, 2):
            self.assertEqual(results[1], (204, None))
            self.assertIn(results[0][0], (200, 404))

    def test_legacy_and_new_endpoint_share_the_quota_path(self):
        def make():
            s = seed(9)
            return s, [m.legacy_subscribe(s, EMAIL, "tokyo", 8000), m.create(s, EMAIL, "TPE-TAK", row("TAK"))]

        for results in self.explore(make, 2):
            self.assertEqual(sum(r[0] in (200, 201) for r in results), 1, results)

    def test_legacy_subscribe_twice_for_a_new_route(self):
        def make():
            s = seed(3, with_quota=False)
            return s, [m.legacy_subscribe(s, EMAIL, "seoul", 8000), m.legacy_subscribe(s, EMAIL, "seoul", 7000)]

        for results in self.explore(make, 2):
            # Second request becomes an update of the row the first created (M1 upsert semantics).
            self.assertTrue(all(r[0] in (200, 409) for r in results))
            self.assertIn(200, [r[0] for r in results])


class WhyTheRulesExistTests(Invariants, unittest.TestCase):
    """Negative controls: each unsafe variant has at least one schedule that breaks the limit."""

    def exceeds(self, make, n_ops):
        return any(len(s.query(EMAIL)) > MAX for s, _ in m.all_schedules(make, n_ops))

    def test_reading_count_before_quota_version_allows_a_bypass(self):
        def make():
            s = seed(9)
            return s, [m.create(s, EMAIL, "TPE-TYO", row("TYO"), order="count_first"),
                       m.create(s, EMAIL, "TPE-TAK", row("TAK"), order="count_first")]

        self.assertTrue(self.exceeds(make, 2))

    def test_unaware_legacy_lambda_alongside_new_api_allows_a_bypass(self):
        def make():
            s = seed(9)
            return s, [m.legacy_unaware_upsert(s, EMAIL, "tokyo", 8000), m.create(s, EMAIL, "TPE-TAK", row("TAK"))]

        self.assertTrue(self.exceeds(make, 2))


if __name__ == "__main__":
    unittest.main()
