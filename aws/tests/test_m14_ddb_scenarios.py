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


@unittest.skipUnless(ddb_local.available(), "boto3/moto (or DDB_ENDPOINT) not available")
class RowVersionAba(unittest.TestCase):
    """Subscription identity under ID reuse (ABA), 2026-11-13. Every request names the row
    version it acts on (if_version); versions are random tokens, never timestamps. Runs on moto
    locally and on DynamoDB Local in the Stage B workflow."""

    @classmethod
    def setUpClass(cls):
        cls.client = ddb_local.client()
        cls.subs, cls.quota = ddb_local.create_tables(cls.client)
        cls.engine = "dynamodb-local" if os.environ.get("DDB_ENDPOINT") else "moto"

    @classmethod
    def tearDownClass(cls):
        ddb_local.drop_tables(cls.client, cls.subs, cls.quota)

    def setUp(self):
        self.ctx = sc.Ctx(self.client, self.subs, self.quota, uuid.uuid4().hex[:8], self.engine)
        self.u = self.ctx.user("aba")
        self.api = self.ctx.raw_service(sleep=lambda s: None)  # exactly what a client sends
        self.a_id, self.b_id = sc.fid("TYO", "2027-01", "KHH"), sc.fid("TYO", "2027-03", "KHH")
        self.a_key, self.b_key = sc.fkey("TYO", "2027-01", "KHH"), sc.fkey("TYO", "2027-03", "KHH")
        for m in ("2027-01", "2027-03"):
            self.api.create(self.u, sc.create_body("TYO", month=m, origin="KHH"))
        self.api.create(self.u, sc.create_body("SEL"))

    # ---- helpers ------------------------------------------------------------------------------
    def version(self, key):
        return sc.Service.version_of(self.ctx.rows(self.u)[key])

    def call(self, fn, *args):
        try:
            return fn(*args)
        except sc.ApiError as e:
            return e.status, e.code

    def state(self):
        return self.ctx.rows(self.u), self.ctx.quota(self.u)

    def consistent(self, rows_expected):
        chk = sc.Check("aba")
        rows, q = chk.invariants(self.ctx, self.u, expect_rows=rows_expected)
        result = chk.result()
        self.assertTrue(result["passed"], result["failures"])  # I1-I7 (+ token on every M1.4 row)
        return rows, q

    def a_deleted_and_b_moved_in(self):
        v_a = self.version(self.a_key)
        self.assertEqual(self.api.delete(self.u, self.a_id, v_a), (204, None))                     # A
        status, body = self.api.patch(self.u, self.b_id, {"departure_month": "2027-01",
                                                          "if_version": self.version(self.b_key)})  # B
        self.assertEqual((status, body["id"]), (200, self.a_id))
        return v_a, body

    # ---- A-D: the Stage B ordering --------------------------------------------------------------
    def test_stale_delete_with_a_version_is_refused_and_b_survives(self):
        v_a, b = self.a_deleted_and_b_moved_in()
        rows, _ = self.consistent(2)
        self.assertNotIn(self.b_key, rows)                                           # no partial move
        self.assertEqual(rows[self.a_key]["previous_route"], self.b_key)
        self.assertNotEqual(b["version"], v_a)
        before = self.state()
        self.assertEqual(self.call(self.api.delete, self.u, self.a_id, v_a), (409, "conflict"))     # C
        self.assertEqual(self.state(), before, "the stale delete wrote something")
        self.assertEqual(self.api.delete(self.u, self.a_id, b["version"]), (204, None))             # D
        _, q = self.consistent(1)
        self.assertEqual((q["count"], q["version"]), (1, 5))  # 3 creates + 2 deletes; the move: none

    def test_identical_updated_at_cannot_confuse_a_and_b(self):
        """Fixed clock: A is edited once (stamp NOW+1ms) and B's move also gets NOW+1ms, so the
        two subscriptions share updated_at exactly. A timestamp token cannot tell them apart; the
        random version can. (The pre-token implementation deletes B here.)"""
        status, a = self.api.patch(self.u, self.a_id, {"target_price": 7777, "if_version": self.version(self.a_key)})
        self.assertEqual(status, 200)
        self.assertEqual(self.api.delete(self.u, self.a_id, a["version"]), (204, None))
        status, b = self.api.patch(self.u, self.b_id, {"departure_month": "2027-01", "if_version": self.version(self.b_key)})
        self.assertEqual(b["updated_at"], a["updated_at"])                           # the collision
        self.assertNotEqual(b["version"], a["version"])
        self.assertEqual(self.call(self.api.delete, self.u, self.a_id, a["version"]), (409, "conflict"))
        self.assertEqual(self.call(self.api.patch, self.u, self.a_id, {"target_price": 1, "if_version": a["version"]}),
                         (409, "conflict"))
        rows, _ = self.consistent(2)
        self.assertEqual(rows[self.a_key]["previous_route"], self.b_key)             # B intact
        self.assertEqual(int(rows[self.a_key]["target_price"]), 9000)

    def test_versionless_requests_are_rejected_never_unconditional(self):
        before = self.state()
        self.assertEqual(self.call(self.api.delete, self.u, self.a_id, None), (428, "version_required"))
        self.assertEqual(self.call(self.api.patch, self.u, self.a_id, {"target_price": 1}), (428, "version_required"))
        self.assertEqual(self.call(self.api.delete, self.u, self.a_id, "not-a-version"), (400, "invalid_version"))
        self.assertEqual(self.state(), before)

    def test_retry_after_quota_contention_stays_on_the_named_version(self):
        """The DELETE for A loses its first transaction to quota contention; meanwhile A is
        deleted and B moved in. The retry must not delete B (409, never a re-read version)."""
        v_a, other = self.version(self.a_key), self.ctx.raw_service(sleep=lambda s: None)
        real, calls = self.ctx.store.delete, {"n": 0}

        def racing(*args, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                other.delete(self.u, self.a_id, v_a)
                other.patch(self.u, self.b_id, {"departure_month": "2027-01", "if_version": self.version(self.b_key)})
                raise sc.store_mod.TxCancelled(["None", "ConditionalCheckFailed"])
            return real(*args, **kw)

        from unittest import mock
        with mock.patch.object(self.ctx.store, "delete", side_effect=racing):
            self.assertEqual(self.call(self.api.delete, self.u, self.a_id, v_a), (409, "conflict"))
        rows, q = self.consistent(2)
        self.assertEqual(rows[self.a_key]["previous_route"], self.b_key)
        self.assertEqual((q["count"], q["version"]), (2, 4))

    # ---- concurrency ------------------------------------------------------------------------------
    def test_concurrent_patch_and_delete_of_one_version(self):
        v_a = self.version(self.a_key)
        res = sc.parallel([lambda i=i: self.api.patch(self.u, self.a_id, {"target_price": 5000 + i, "if_version": v_a})
                           for i in range(15)] + [lambda: self.api.delete(self.u, self.a_id, v_a) for _ in range(15)])
        patched, deleted = sum(s == 200 for s, _ in res[:15]), sum(s == 204 for s, _ in res[15:])
        self.assertEqual(patched + deleted, 1, res)  # exactly one request acted on version A
        self.assertTrue(all(s in (200, 404, 409) for s, _ in res[:15]) and all(s in (204, 404, 409) for s, _ in res[15:]), res)
        rows, q = self.consistent(3 - deleted)
        self.assertEqual(q["version"], 3 + deleted)

    def test_two_simultaneous_deletes(self):
        v_a = self.version(self.a_key)
        res = sc.parallel([lambda: self.api.delete(self.u, self.a_id, v_a) for _ in range(20)])
        self.assertLessEqual(sum(s == 204 for s, _ in res), 1, res)
        self.assertTrue(all(s in (204, 404, 409) for s, _ in res), res)
        deleted = sum(s == 204 for s, _ in res)
        _, q = self.consistent(3 - deleted)
        self.assertEqual((q["count"], q["version"]), (3 - deleted, 3 + deleted))

    def test_route_and_month_moves_issue_new_versions(self):
        v_a = self.version(self.a_key)
        status, moved = self.api.patch(self.u, self.a_id, {"departure_month": "2027-05", "if_version": v_a})
        self.assertEqual((status, moved["id"]), (200, "KHH-TYO_2027-05"))
        self.assertNotEqual(moved["version"], v_a)
        self.assertEqual(self.call(self.api.delete, self.u, moved["id"], v_a), (409, "conflict"))  # old version, new ID
        status, routed = self.api.patch(self.u, moved["id"], {"origin": "TPE", "destination": "TYO", "if_version": moved["version"]})
        self.assertEqual((status, routed["id"]), (200, "TPE-TYO_2027-05"))
        self.assertNotIn(routed["version"], (v_a, moved["version"]))
        self.assertEqual(self.call(self.api.patch, self.u, routed["id"], {"target_price": 1, "if_version": moved["version"]}),
                         (409, "conflict"))
        _, q = self.consistent(3)
        self.assertEqual((q["count"], q["version"]), (3, 3))  # moves never touch the quota

    # ---- legacy M1 rows ---------------------------------------------------------------------------
    def seed_m1(self, route="TPE-OSA", **fields):
        """A row exactly as the deployed M1 save Lambda writes it: no version_token, second-precision
        "+00:00" timestamps. ``updated_at=None`` drops it entirely."""
        item = {"route": route, "origin": route[:3], "destination": route[4:], "target_price": 9000, "currency": "TWD",
                "created_at": "2026-10-30T00:00:00+00:00", "updated_at": "2026-10-30T00:00:00+00:00", **fields}
        self.ctx.seed(self.u, route, **{k: v for k, v in item.items() if k != "route"})

    def m1_save_lambda_write(self, route, target, now="2026-11-13T02:00:00+00:00"):
        """What flight-save-subscription (M1, still live until cutover) does: an upsert that never
        touches version_token."""
        self.client.update_item(
            TableName=self.subs, Key=sc.store_mod.to_item({"email": self.u, "route": route}),
            UpdateExpression="SET target_price=:t, updated_at=:n, created_at=if_not_exists(created_at,:n)",
            ExpressionAttributeValues=sc.store_mod.to_item({":t": target, ":n": now}))

    def test_legacy_rows_get_a_legacy_version_and_adopt_a_token_on_first_write(self):
        self.seed_m1()
        self.seed_m1("TPE-NGO", updated_at=None)  # an M1 row without updated_at
        for key in ("TPE-OSA", "TPE-NGO"):
            self.assertRegex(self.version(key), r"^legacy-[0-9a-f]{32}$")
        v = self.version("TPE-OSA")
        status, body = self.api.patch(self.u, "TPE-OSA", {"target_price": 8000, "if_version": v})
        self.assertEqual(status, 200)
        self.assertRegex(body["version"], r"^[A-Za-z0-9_-]{22}$")                  # adopted a token
        self.assertEqual(self.call(self.api.patch, self.u, "TPE-OSA", {"target_price": 1, "if_version": v}), (409, "conflict"))
        self.assertEqual(self.api.delete(self.u, "TPE-NGO", self.version("TPE-NGO")), (204, None))
        self.consistent(4)

    def test_stale_legacy_version_cannot_delete_a_recreated_m1_row(self):
        self.seed_m1()
        v_old = self.version("TPE-OSA")
        self.assertEqual(self.api.delete(self.u, "TPE-OSA", v_old), (204, None))
        self.seed_m1(created_at="2026-11-13T01:00:00+00:00", updated_at="2026-11-13T01:00:00+00:00")  # M1 re-creates
        self.assertEqual(self.call(self.api.delete, self.u, "TPE-OSA", v_old), (409, "conflict"))
        self.assertIn("TPE-OSA", self.ctx.rows(self.u))

    def test_m1_save_lambda_write_on_a_token_row_invalidates_its_version(self):
        """The live M1 Lambda updates a row M1.4 wrote but leaves version_token unchanged; the
        updated_at part of the guard (M1 writes "+00:00" stamps, M1.4 never does) still refuses
        the stale M1.4 request."""
        status, body = self.api.legacy_subscribe(self.u, {"plan_name": "tokyo", "target_price": 9000})
        v, token = self.version("TPE-TYO"), self.ctx.rows(self.u)["TPE-TYO"]["version_token"]
        self.assertEqual(v, token)
        self.m1_save_lambda_write("TPE-TYO", 6000)
        self.assertEqual(self.ctx.rows(self.u)["TPE-TYO"]["version_token"], token)   # stored token unchanged…
        self.assertRegex(self.version("TPE-TYO"), r"^legacy-")                        # …but the public version moved
        self.assertEqual(self.call(self.api.delete, self.u, "TPE-TYO", v), (409, "conflict"))  # stale: refused
        self.assertEqual(self.call(self.api.patch, self.u, "TPE-TYO", {"target_price": 1, "if_version": v}), (409, "conflict"))
        self.assertEqual(int(self.ctx.rows(self.u)["TPE-TYO"]["target_price"]), 6000)
        # A client that reloaded sees the legacy version and can act on it; M1.4 then re-tokens the row.
        status, body = self.api.patch(self.u, "TPE-TYO", {"target_price": 5000, "if_version": self.version("TPE-TYO")})
        self.assertEqual(status, 200)
        self.assertNotIn(body["version"], (v, token))
        self.consistent(4)

    def test_documented_limit_identical_legacy_recreation(self):
        """The one remaining gap (contract §4.5): an M1 row deleted and re-created by the M1 Lambda
        with byte-identical content — same target, same second for created_at and updated_at — has
        the same legacy version. Closed by the B3 migration (token backfill, M1 write path off)."""
        self.seed_m1()
        v_old = self.version("TPE-OSA")
        self.assertEqual(self.api.delete(self.u, "TPE-OSA", v_old), (204, None))
        self.seed_m1()  # identical content
        self.assertEqual(self.version("TPE-OSA"), v_old)


if sc is not None:
    for _fn in sc.SCENARIOS:
        setattr(B4Scenarios, f"test_{_fn.__name__}", lambda self, fn=_fn: self.run_scenario(fn))


if __name__ == "__main__":
    unittest.main()
