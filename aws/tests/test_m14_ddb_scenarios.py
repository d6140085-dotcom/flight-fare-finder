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
