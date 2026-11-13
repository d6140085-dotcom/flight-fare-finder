"""B4 concurrency validation scenarios (aws/integration/m14_scenarios.py) run locally.

Engine: moto's DynamoDB-compatible server (default), or Amazon DynamoDB Local when
DDB_ENDPOINT=http://127.0.0.1:8000 is set (aws/scripts/test_dynamodb_local.sh). The same
scenarios run against real DynamoDB only through the temporary, owner-approved test stack
(aws/integration/README.md). Skips without boto3/moto: pip install -r aws/requirements-test.txt
"""
import os
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ddb_local  # noqa: E402  (before anything imports boto3)

sys.path.insert(0, str(HERE.parent / "lambdas" / "flight-subscriptions-api"))
sys.path.insert(0, str(HERE.parent / "integration"))
if ddb_local.boto3 is not None:
    import m14_scenarios as sc  # noqa: E402
else:
    sc = None


@unittest.skipUnless(ddb_local.available(), "boto3/moto (or DDB_ENDPOINT) not available")
class B4Scenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = ddb_local.client()
        cls.subs, cls.quota = ddb_local.create_tables(cls.client)
        engine = "dynamodb-local" if os.environ.get("DDB_ENDPOINT") else "moto"
        cls.ctx = sc.Ctx(cls.client, cls.subs, cls.quota, uuid.uuid4().hex[:8], engine)
        cls.bystander = cls.ctx.user("bystander")
        cls.ctx.seed(cls.bystander, "TPE-TYO", plan_name="tokyo")
        cls.ctx.seed_quota(cls.bystander, 1, 3)
        cls.before = cls.ctx.raw_items(cls.bystander)

    @classmethod
    def tearDownClass(cls):
        ddb_local.drop_tables(cls.client, cls.subs, cls.quota)

    def run_scenario(self, fn):
        result = fn(self.ctx)
        self.assertTrue(result["passed"], f"{result['scenario']}: {result['failures']} {result['metrics']}")
        self.assertEqual(self.ctx.raw_items(self.bystander), self.before, "I5 bystander changed")

    def test_parallelism_is_at_least_50(self):
        self.assertGreaterEqual(sc.PARALLEL, 50)

    # ---- s10 regression: TransactionConflictException (real DynamoDB, B4 Stage C) -------------
    def fresh_ctx(self):
        """New run id = new test users, so these extra runs never collide with the main run."""
        return sc.Ctx(self.client, self.subs, self.quota, uuid.uuid4().hex[:8], self.ctx.engine)

    def inject_conflicts(self, every=3):
        """Make every n-th UpdateItem fail exactly like real DynamoDB did in Stage C."""
        import threading
        from unittest import mock
        from botocore.exceptions import ClientError

        real, lock, n = self.client.update_item, threading.Lock(), {"i": 0}

        def flaky(**kw):
            with lock:
                n["i"] += 1
                hit = n["i"] % every == 0
            if hit:
                raise ClientError({"Error": {"Code": "TransactionConflictException",
                                             "Message": "Transaction is ongoing for the item"}}, "UpdateItem")
            return real(**kw)

        return mock.patch.object(self.client, "update_item", side_effect=flaky), n

    def test_s15_passes_with_injected_transaction_conflicts(self):
        ctx = self.fresh_ctx()
        patcher, n = self.inject_conflicts()
        with patcher:
            result = sc.s15_same_row_write_storm(ctx)
        self.assertGreater(n["i"], 3)  # conflicts were actually injected
        self.assertTrue(result["passed"], f"{result['failures']} {result['metrics']}")
        self.assertFalse(any("unexpected" in k for k in result["metrics"]["responses"]))

    def test_s10_passes_with_injected_transaction_conflicts(self):
        ctx = self.fresh_ctx()
        patcher, _ = self.inject_conflicts()
        with patcher:
            result = sc.s10_legacy_and_new_endpoints(ctx)
        self.assertTrue(result["passed"], f"{result['failures']} {result['metrics']}")

    def test_harness_reports_the_original_bug_instead_of_crashing(self):
        """Put the pre-fix behaviour back (WriteConflict re-raised as the raw ClientError): the
        scenario must finish, check integrity and fail with 'unexpected 500s' — not crash."""
        from unittest import mock
        from botocore.exceptions import ClientError

        ctx = self.fresh_ctx()
        orig = ctx.store.update

        def pre_fix_update(*a, **kw):
            try:
                return orig(*a, **kw)
            except sc.store_mod.WriteConflict:
                raise ClientError({"Error": {"Code": "TransactionConflictException", "Message": "x"}}, "UpdateItem")

        patcher, _ = self.inject_conflicts()
        with patcher, mock.patch.object(ctx.store, "update", side_effect=pre_fix_update):
            result = sc.s15_same_row_write_storm(ctx)
        self.assertFalse(result["passed"])
        self.assertTrue(any("unexpected 500s" in f and "TransactionConflictException" in f for f in result["failures"]),
                        result["failures"])
        self.assertFalse(any(f.startswith("I1") or f.startswith("I2") or f.startswith("I4") for f in result["failures"]),
                         "integrity must still hold: the rejected writes wrote nothing")

    # ---- s19 / Stage B regression: subscription ID reuse (ABA) -----------------------------------
    def aba_setup(self):
        """KHH-TYO 2027-01 (A), 2027-03 (B) and one sibling route, all created at the fixed clock."""
        ctx = self.fresh_ctx()
        u, svc = ctx.user("aba"), ctx.service(sleep=lambda s: None)
        for m in ("2027-01", "2027-03"):
            svc.create(u, sc.create_body("TYO", month=m, origin="KHH"))
        svc.create(u, sc.create_body("SEL"))
        a = ctx.rows(u)[sc.fkey("TYO", "2027-01", "KHH")]
        return ctx, u, svc, a

    def assert_consistent(self, ctx, u, rows_expected):
        chk = sc.Check("aba")
        rows, q = chk.invariants(ctx, u, expect_rows=rows_expected)
        result = chk.result()
        self.assertTrue(result["passed"], result["failures"])  # I1-I7
        return rows, q

    def test_stale_delete_never_deletes_a_row_moved_into_the_same_id(self):
        """The exact Stage B s19 ordering: delete A, move B into A's ID, then a DELETE that still
        carries A's version (what the page sends) arrives. It must be refused; a fresh DELETE with
        B's version then succeeds. Quota and invariants exact at every step."""
        ctx, u, svc, a = self.aba_setup()
        a_id, b_id, v_a = sc.fid("TYO", "2027-01", "KHH"), sc.fid("TYO", "2027-03", "KHH"), a["updated_at"]
        self.assertEqual(svc.delete(u, a_id, v_a), (204, None))                      # 1. A deleted
        self.assert_consistent(ctx, u, 2)
        status, body = svc.patch(u, b_id, {"departure_month": "2027-01"})              # 2. B moves in
        self.assertEqual((status, body["id"]), (200, a_id))
        rows, q = self.assert_consistent(ctx, u, 2)
        b = rows[sc.fkey("TYO", "2027-01", "KHH")]
        self.assertNotEqual(b["updated_at"], v_a)
        self.assertEqual(b["previous_route"], sc.fkey("TYO", "2027-03", "KHH"))
        self.assertNotIn(sc.fkey("TYO", "2027-03", "KHH"), rows)                     # no partial move
        before = (ctx.rows(u), ctx.quota(u))
        with self.assertRaises(sc.ApiError) as e:                                      # 3. stale DELETE
            svc.delete(u, a_id, v_a)
        self.assertEqual((e.exception.status, e.exception.code), (409, "conflict"))
        self.assertEqual((ctx.rows(u), ctx.quota(u)), before, "a refused delete wrote something")
        self.assertEqual(svc.delete(u, a_id, b["updated_at"]), (204, None))            # 4. fresh DELETE
        rows, q = self.assert_consistent(ctx, u, 1)
        self.assertEqual((q["count"], q["version"]), (1, 5))  # 3 creates + 2 deletes; the move: none

    def test_versionless_delete_never_switches_rows_between_attempts(self):
        """A DELETE without if_updated_at reads A, then its transaction is cancelled because another
        request deleted A (quota version moved) and B moved into the same ID before the retry. The
        retry must not delete B: the request is bound to the row version it first read."""
        ctx, u, svc, a = self.aba_setup()
        a_id, b_id = sc.fid("TYO", "2027-01", "KHH"), sc.fid("TYO", "2027-03", "KHH")
        other = ctx.service(sleep=lambda s: None)
        real, calls = ctx.store.delete, {"n": 0}

        def racing_delete(*args, **kw):
            calls["n"] += 1
            if calls["n"] == 1:   # the request under test, first attempt: others commit first
                other.delete(u, a_id, a["updated_at"])
                other.patch(u, b_id, {"departure_month": "2027-01"})
                raise sc.store_mod.TxCancelled(["None", "ConditionalCheckFailed"])
            return real(*args, **kw)

        from unittest import mock
        with mock.patch.object(ctx.store, "delete", side_effect=racing_delete):
            with self.assertRaises(sc.ApiError) as e:
                svc.delete(u, a_id)
        self.assertEqual((e.exception.status, e.exception.code), (409, "conflict"))
        rows, q = self.assert_consistent(ctx, u, 2)
        moved = rows.get(sc.fkey("TYO", "2027-01", "KHH"))
        self.assertIsNotNone(moved, "the request deleted a different subscription (B) on retry")
        self.assertEqual(moved["previous_route"], sc.fkey("TYO", "2027-03", "KHH"))
        self.assertEqual((q["count"], q["version"]), (2, 4))

    def test_parallel_records_unexpected_exceptions(self):
        def boom():
            raise RuntimeError("programming error")

        res = sc.parallel([boom, lambda: (200, {})])
        self.assertEqual(res[0], (500, {"error": "unexpected", "exception": "RuntimeError:None"}))
        chk = sc.Check("probe")
        chk.responses(res)
        self.assertFalse(chk.result()["passed"])


if sc is not None:
    for _fn in sc.SCENARIOS:
        setattr(B4Scenarios, f"test_{_fn.__name__}", lambda self, fn=_fn: self.run_scenario(fn))


if __name__ == "__main__":
    unittest.main()
