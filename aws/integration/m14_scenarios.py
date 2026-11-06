"""M1.4 release blocker B4: concurrency validation scenarios for flight-subscriptions-api.

The same scenarios run against three engines, unchanged:
  * a local DynamoDB-compatible server (moto)        -> aws/tests/test_m14_ddb_scenarios.py
  * Amazon DynamoDB Local (Docker)                   -> DDB_ENDPOINT=... same unittest file
  * real DynamoDB, isolated temporary test tables    -> aws/integration/runner/handler.py

Each scenario drives the real ``Service``/``Store`` code with >= 50 simultaneous requests where
concurrency matters, then checks the invariants:
  I1  no user ever holds more than ``max`` (10) records — active, paused and expired all count;
  I2  if a quota record exists, ``count`` equals the true number of rows;
  I3  ``version`` equals the number of committed count-changing transactions (no double init);
  I4  every row is either untouched seed data or the result of a successful response — failed
      requests leave nothing behind and nothing half-written;
  I5  other users' rows and quota records are byte-for-byte unchanged.

Scenarios only ever use addresses in the reserved example.com domain and only the tables they
are given; the runner refuses table names that aren't temporary test tables.
"""
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from subsapi import config as config_mod
from subsapi import store as store_mod
from subsapi.errors import ApiError
from subsapi.service import Service

NOW = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)  # fixed clock: window 2026-12 .. 2027-11
PARALLEL = 60
DESTS = ["OSA", "NGO", "FUK", "OKA", "SPK", "KMJ", "PUS", "CJU", "HKG", "MFM", "BKK", "CNX", "HKT",
         "SIN", "KUL", "PEN", "BKI", "MNL", "CEB", "SGN", "HAN", "DAD", "DPS", "LON", "PAR", "FRA",
         "MUC", "AMS", "ZRH", "VIE", "ROM", "MIL", "NYC", "LAX", "SFO", "SEA", "SYD", "MEL", "AKL",
         "ZQN", "BER", "GVA", "VCE", "FLR", "MAD", "BCN", "LIS", "OPO", "PRG", "BUD", "CPH", "STO",
         "OSL", "HEL", "DUB", "ATH", "IST", "CHI", "BOS", "WAS", "YVR", "YYC", "YTO", "YMQ", "MEX"]
REQUIRED_ATTRS = {"email", "route", "origin", "destination", "target_price", "currency", "created_at", "updated_at"}


class Ctx:
    """Everything a scenario needs: one DynamoDB client, two tables, a run id."""

    def __init__(self, client, subs_table, quota_table, run_id, engine):
        self.client, self.subs_table, self.quota_table = client, subs_table, quota_table
        self.run_id, self.engine = run_id, engine
        self.cfg = config_mod.load({})
        self.store = InstrumentedStore(client, subs_table, quota_table)

    def service(self, sleep=time.sleep):
        return Service(self.store, self.cfg, clock=lambda: NOW, sleep=sleep, rng=random.Random())

    def user(self, scenario, i=0):
        return f"b4-{scenario}-{self.run_id}-{i}@example.com"

    # ---- direct table access (seed / inspect) ------------------------------------------
    def seed(self, email, route, **fields):
        o, _, d = route.partition("-")
        item = {"email": email, "route": route, "origin": o, "destination": d, "target_price": 9000,
                "currency": "TWD", "created_at": "2026-10-30T00:00:00+00:00",
                "updated_at": "2026-10-30T00:00:00+00:00", **fields}
        self.client.put_item(TableName=self.subs_table, Item=store_mod.to_item(item))
        return item

    def seed_quota(self, email, count, version):
        self.client.put_item(TableName=self.quota_table, Item=store_mod.to_item(
            {"email": email, "count": count, "version": version, "updated_at": "2026-10-30T00:00:00.000Z"}))

    def rows(self, email):
        return {r["route"]: r for r in self.store.list_rows(email)}

    def quota(self, email):
        return self.store.get_quota(email)

    def raw_items(self, email):
        """Exact stored items (DynamoDB JSON) for bystander comparisons."""
        subs = self.client.query(TableName=self.subs_table, KeyConditionExpression="email = :e",
                                 ExpressionAttributeValues={":e": {"S": email}}, ConsistentRead=True)["Items"]
        q = self.client.get_item(TableName=self.quota_table, Key={"email": {"S": email}}, ConsistentRead=True)
        return sorted(subs, key=lambda i: i["route"]["S"]), q.get("Item")


class InstrumentedStore(store_mod.Store):
    """The production Store plus thread-safe counters of committed and cancelled transactions."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._lock = threading.Lock()
        self.committed = 0
        self.reasons = {}

    def _transact(self, items):
        try:
            super()._transact(items)
        except store_mod.TxCancelled as e:
            with self._lock:
                for r in e.reasons:
                    self.reasons[r] = self.reasons.get(r, 0) + 1
            raise
        with self._lock:
            self.committed += 1


class Check:
    def __init__(self, name):
        self.name, self.failures, self.metrics = name, [], {}

    def that(self, cond, msg):
        if not cond:
            self.failures.append(msg)

    def invariants(self, ctx, email, expect_rows=None):
        rows = ctx.rows(email)
        q = ctx.quota(email)
        self.that(len(rows) <= ctx.cfg.max_subscriptions, f"I1 {email}: {len(rows)} rows")
        if q is not None:
            self.that(q["count"] == len(rows), f"I2 {email}: quota count {q['count']} != rows {len(rows)}")
        for r in rows.values():
            missing = REQUIRED_ATTRS - set(r) - {"email"}
            self.that(not missing, f"I4 {email} {r['route']}: missing {sorted(missing)}")
        if expect_rows is not None:
            self.that(len(rows) == expect_rows, f"{email}: expected {expect_rows} rows, found {len(rows)}")
        return rows, q

    def result(self):
        return {"scenario": self.name, "passed": not self.failures, "failures": self.failures[:20],
                "metrics": self.metrics}


def parallel(calls):
    """Release every call at the same instant; return results in order."""
    barrier = threading.Barrier(len(calls))

    def go(fn):
        barrier.wait()
        try:
            return fn()
        except ApiError as e:
            return e.status, {"error": e.code}

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(go, calls))


def tally(results):
    out = {}
    for status, body in results:
        k = str(status) if status < 400 else f"{status} {body['error']}"
        out[k] = out.get(k, 0) + 1
    return out


def create_body(dest, month="2027-01", origin="TPE"):
    return {"origin": origin, "destination": dest, "departure_month": month, "target_price": 9000}


def fill_to_limit(ctx, svc, email, chk):
    """After a storm: sequential creates must reach exactly the limit, then be refused."""
    for d in DESTS:
        if len(ctx.rows(email)) >= ctx.cfg.max_subscriptions:
            break
        try:
            svc.create(email, create_body(d))
        except ApiError:
            pass
    try:
        svc.create(email, create_body("CNS"))
        chk.that(False, f"{email}: 11th record accepted")
    except ApiError as e:
        chk.that(e.code == "limit_reached", f"{email}: expected limit_reached, got {e.code}")
    chk.invariants(ctx, email, expect_rows=ctx.cfg.max_subscriptions)


# ======================================================================================
# Scenarios
# ======================================================================================
def s01_limit_counts_paused_and_expired(ctx):
    """4 active + 3 paused + 3 expired = 10: 60 simultaneous creates must all be refused."""
    chk, u, svc = Check("s01_limit_counts_paused_and_expired"), ctx.user("s01"), ctx.service()
    for i, d in enumerate(DESTS[:10]):
        extra = {"departure_month": "2027-02"}
        if 4 <= i < 7:
            extra["alerts_paused"] = True
        if i >= 7:
            extra["departure_month"] = "2026-03"  # expired
        ctx.seed(u, f"TPE-{d}", **extra)
    statuses = {svc.view(r, NOW)["status"] for r in ctx.rows(u).values()}
    chk.that(statuses == {"active", "paused", "expired"}, f"seed statuses {statuses}")
    res = parallel([lambda d=d: svc.create(u, create_body(d)) for d in DESTS[10:10 + PARALLEL]])
    chk.metrics["responses"] = tally(res)
    chk.that(all(s == 409 for s, _ in res), "a create succeeded at 10 records")
    chk.invariants(ctx, u, expect_rows=10)
    return chk.result()


def s02_concurrent_creates_from_empty(ctx):
    """60 simultaneous creates by a new user (no quota record)."""
    chk, u, svc = Check("s02_concurrent_creates_from_empty"), ctx.user("s02"), ctx.service()
    res = parallel([lambda d=d: svc.create(u, create_body(d)) for d in DESTS[:PARALLEL]])
    created = [b["route"] for s, b in res if s == 201]
    chk.metrics["responses"] = tally(res)
    chk.that(1 <= len(created) <= 10, f"{len(created)} creates succeeded")
    chk.that(all(s in (201, 409) for s, _ in res), "unexpected status")
    rows, q = chk.invariants(ctx, u)
    chk.that(sorted(rows) == sorted(created), "I4 rows don't match the 201 responses")
    chk.that(q is not None and q["version"] == len(created), f"I3 version {q and q['version']} != {len(created)}")
    fill_to_limit(ctx, svc, u, chk)
    return chk.result()


def s03_concurrent_creates_at_nine(ctx):
    chk, u, svc = Check("s03_concurrent_creates_at_nine"), ctx.user("s03"), ctx.service()
    for d in DESTS[:9]:
        svc.create(u, create_body(d))
    res = parallel([lambda d=d: svc.create(u, create_body(d)) for d in DESTS[9:9 + PARALLEL]])
    chk.metrics["responses"] = tally(res)
    chk.that(sum(s == 201 for s, _ in res) <= 1, "more than one create succeeded at 9")
    chk.invariants(ctx, u)
    fill_to_limit(ctx, svc, u, chk)
    return chk.result()


def s04_same_route_created_concurrently(ctx):
    chk, u, svc = Check("s04_same_route_created_concurrently"), ctx.user("s04"), ctx.service()
    res = parallel([lambda: svc.create(u, create_body("LON")) for _ in range(PARALLEL)])
    chk.metrics["responses"] = tally(res)
    chk.that(sum(s == 201 for s, _ in res) == 1, "not exactly one create succeeded")
    chk.that(all(s == 201 or b["error"] in ("duplicate_subscription", "conflict") for s, b in res), "bad error")
    rows, q = chk.invariants(ctx, u, expect_rows=1)
    chk.that(q is not None and q["version"] == 1, f"I3 version {q and q['version']}")
    return chk.result()


def s05_concurrent_deletes(ctx):
    """10 rows, 60 deletes (6 per row): each row is deleted at most once; follow-up deletes finish."""
    chk, u, svc = Check("s05_concurrent_deletes"), ctx.user("s05"), ctx.service()
    for d in DESTS[:10]:
        svc.create(u, create_body(d))
    routes = [f"TPE-{d}" for d in DESTS[:10]]
    calls = [lambda r=r: svc.delete(u, r) for r in routes for _ in range(6)]
    res = parallel(calls)
    chk.metrics["responses"] = tally(res)
    per_route = {r: 0 for r in routes}
    for (status, _), call_route in zip(res, [r for r in routes for _ in range(6)]):
        if status == 204:
            per_route[call_route] += 1
    chk.that(all(n <= 1 for n in per_route.values()), f"a row was deleted twice: {per_route}")
    rows, q = chk.invariants(ctx, u, expect_rows=10 - sum(per_route.values()))
    chk.that(q["version"] == 10 + sum(per_route.values()), f"I3 version {q['version']}")
    for r in list(rows):
        svc.delete(u, r)
    rows, q = chk.invariants(ctx, u, expect_rows=0)
    chk.that(q["count"] == 0 and q["version"] == 20, f"after cleanup {q}")
    return chk.result()


def s06_deletes_and_creates_at_limit(ctx):
    chk, u, svc = Check("s06_deletes_and_creates_at_limit"), ctx.user("s06"), ctx.service()
    for d in DESTS[:10]:
        svc.create(u, create_body(d))
    deletes = [lambda d=d: svc.delete(u, f"TPE-{d}") for d in DESTS[:5]]
    creates = [lambda d=d: svc.create(u, create_body(d)) for d in DESTS[10:65]]
    res = parallel(deletes + creates)
    deleted = sum(s == 204 for s, _ in res[:5])
    created = sum(s == 201 for s, _ in res[5:])
    chk.metrics.update(responses=tally(res), deleted=deleted, created=created)
    chk.that(created <= deleted, "more creates than freed slots")
    chk.invariants(ctx, u, expect_rows=10 - deleted + created)
    fill_to_limit(ctx, svc, u, chk)
    return chk.result()


def s07_route_edits_of_one_row(ctx):
    """60 simultaneous route changes of the same row to 60 different routes: exactly one wins."""
    chk, u, svc = Check("s07_route_edits_of_one_row"), ctx.user("s07"), ctx.service()
    for d in DESTS[:9]:
        svc.create(u, create_body(d))
    targets = DESTS[9:9 + PARALLEL]
    res = parallel([lambda d=d: svc.patch(u, "TPE-OSA", {"origin": "KHH", "destination": d}) for d in targets])
    chk.metrics["responses"] = tally(res)
    winners = [b["route"] for s, b in res if s == 200]
    chk.that(len(winners) == 1, f"{len(winners)} route edits succeeded")
    chk.that(all(s == 200 or b["error"] in ("not_found", "conflict") for s, b in res), "bad error")
    rows, q = chk.invariants(ctx, u, expect_rows=9)
    chk.that("TPE-OSA" not in rows and all(w in rows for w in winners), "old route remains or winner missing")
    chk.that(q["version"] == 9, f"route edits touched the quota: version {q['version']}")
    return chk.result()


def s08_route_edits_onto_one_target(ctx):
    """10 rows; 60 route changes (6 per row) all onto TPE-LON: exactly one row becomes TPE-LON."""
    chk, u, svc = Check("s08_route_edits_onto_one_target"), ctx.user("s08"), ctx.service()
    for d in DESTS[:10]:
        svc.create(u, create_body(d))
    calls = [lambda d=d: svc.patch(u, f"TPE-{d}", {"origin": "TPE", "destination": "LON"})
             for d in DESTS[:10] for _ in range(6)]
    res = parallel(calls)
    chk.metrics["responses"] = tally(res)
    chk.that(sum(s == 200 for s, _ in res) == 1, "not exactly one row moved to TPE-LON")
    chk.that(all(s == 200 or b["error"] in ("duplicate_subscription", "not_found", "conflict") for s, b in res),
             "bad error")
    rows, q = chk.invariants(ctx, u, expect_rows=10)
    chk.that("TPE-LON" in rows, "TPE-LON missing")
    chk.that(q["version"] == 10, f"version {q['version']}")
    return chk.result()


def s09_edits_pauses_and_creates_at_limit(ctx):
    """At 10: 20 pauses/resumes + 20 target/month edits + 20 creates. Edits never need a slot."""
    chk, u, svc = Check("s09_edits_pauses_and_creates_at_limit"), ctx.user("s09"), ctx.service()
    for d in DESTS[:10]:
        svc.create(u, create_body(d))
    pauses = [lambda i=i: svc.patch(u, f"TPE-{DESTS[i % 10]}", {"alerts_paused": i % 2 == 0}) for i in range(20)]
    edits = [lambda i=i: svc.patch(u, f"TPE-{DESTS[i % 10]}", {"target_price": 5000 + i, "departure_month": "2027-03"})
             for i in range(20)]
    creates = [lambda d=d: svc.create(u, create_body(d)) for d in DESTS[10:30]]
    res = parallel(pauses + edits + creates)
    chk.metrics["responses"] = tally(res)
    chk.that(all(s in (200, 409) for s, _ in res[:40]), "edit/pause returned an unexpected status")
    chk.that(all(s == 409 for s, _ in res[40:]), "a create succeeded at 10")
    rows, q = chk.invariants(ctx, u, expect_rows=10)
    chk.that(q["version"] == 10, f"edits touched the quota: version {q['version']}")
    return chk.result()


def s10_legacy_and_new_endpoints(ctx):
    """At 9 (TPE-TYO absent): 30 legacy POST /subscribe (tokyo) + 30 POST /subscriptions."""
    chk, u, svc = Check("s10_legacy_and_new_endpoints"), ctx.user("s10"), ctx.service()
    for d in DESTS[:9]:
        svc.create(u, create_body(d))
    legacy = [lambda: svc.legacy_subscribe(u, {"plan_name": "tokyo", "target_price": 8000}) for _ in range(30)]
    creates = [lambda d=d: svc.create(u, create_body(d)) for d in DESTS[9:39]]
    res = parallel(legacy + creates)
    chk.metrics["responses"] = tally(res)
    rows, q = chk.invariants(ctx, u, expect_rows=10)
    legacy_won = "TPE-TYO" in rows
    chk.that(int(legacy_won) + sum(s == 201 for s, _ in res[30:]) == 1, "not exactly one new row")
    if legacy_won:
        chk.that("departure_month" not in rows["TPE-TYO"], "legacy row isn't rolling")
    else:
        chk.that(all(s == 409 for s, _ in res[:30]), "a legacy request succeeded without its row")
    chk.that(q["version"] == 10, f"I3 version {q['version']}")
    return chk.result()


def s11_m1_quota_initialisation(ctx):
    """20 M1 users (2 rolling rows each, no quota record) x 3 simultaneous requests = 60:
    a create, a legacy upsert of the existing tokyo row, and a delete of the seoul row."""
    chk, svc = Check("s11_m1_quota_initialisation"), ctx.service()
    users = [ctx.user("s11", i) for i in range(20)]
    for u in users:
        ctx.seed(u, "TPE-TYO", plan_name="tokyo")
        ctx.seed(u, "TPE-SEL", plan_name="seoul", target_price=7000)
    calls = []
    for u in users:
        calls += [lambda u=u: svc.create(u, create_body("LON")),
                  lambda u=u: svc.legacy_subscribe(u, {"plan_name": "tokyo", "target_price": 7777}),
                  lambda u=u: svc.delete(u, "TPE-SEL")]
    res = parallel(calls)
    chk.metrics["responses"] = tally(res)
    for i, u in enumerate(users):
        c, l, d = res[3 * i: 3 * i + 3]
        changes = int(c[0] == 201) + int(d[0] == 204)
        rows, q = chk.invariants(ctx, u, expect_rows=2 + int(c[0] == 201) - int(d[0] == 204))
        if changes:
            chk.that(q is not None and q["version"] == changes, f"I3 {u}: version {q and q['version']} != {changes}")
        else:
            chk.that(q is None, f"{u}: quota record without a committed change")
        chk.that(l[0] in (200, 409), f"{u}: legacy status {l[0]}")
        tyo = rows["TPE-TYO"]
        chk.that("departure_month" not in tyo and tyo["created_at"] == "2026-10-30T00:00:00+00:00",
                 f"{u}: M1 row lost its rolling mode or created_at")
    return chk.result()


def s12_conflict_storm_on_one_quota(ctx):
    """60 creates and deletes against one user's quota item: counts cancellation reasons
    (real DynamoDB reports TransactionConflict here) and checks nothing is corrupted."""
    chk, u, svc = Check("s12_conflict_storm_on_one_quota"), ctx.user("s12"), ctx.service()
    for d in DESTS[:5]:
        svc.create(u, create_body(d))
    before_committed, before = ctx.store.committed, dict(ctx.store.reasons)
    calls = [lambda d=d: svc.delete(u, f"TPE-{d}") for d in DESTS[:5]]
    calls += [lambda d=d: svc.create(u, create_body(d)) for d in DESTS[5:60]]
    res = parallel(calls)
    reasons = {k: v - before.get(k, 0) for k, v in ctx.store.reasons.items() if v - before.get(k, 0)}
    chk.metrics.update(responses=tally(res), cancellation_reasons=reasons,
                       committed=ctx.store.committed - before_committed)
    rows, q = chk.invariants(ctx, u)
    ok = sum(s in (201, 204) for s, _ in res)
    chk.that(q["version"] == 5 + ok, f"I3 version {q['version']} != {5 + ok}")
    created = {b["route"] for s, b in res if s == 201}
    deleted = {f"TPE-{DESTS[i]}" for i, (s, _) in enumerate(res[:5]) if s == 204}
    expected = ({f"TPE-{d}" for d in DESTS[:5]} - deleted) | created
    chk.that(set(rows) == expected, "I4 rows differ from the successful responses")
    return chk.result()


def s13_bounded_retries_write_nothing(ctx):
    """Force every transaction to be cancelled: the service gives up after 3 attempts with
    409 conflict and leaves no row and no quota change behind."""
    chk, u = Check("s13_bounded_retries_write_nothing"), ctx.user("s13")
    sleeps = []
    svc = ctx.service(sleep=sleeps.append)
    real = ctx.store.create

    def always_conflict(*a, **kw):
        raise store_mod.TxCancelled(["None", "TransactionConflict"])

    ctx.store.create = always_conflict
    try:
        try:
            svc.create(u, create_body("LON"))
            chk.that(False, "create succeeded")
        except ApiError as e:
            chk.that(e.code == "conflict", f"expected conflict, got {e.code}")
    finally:
        ctx.store.create = real
    chk.that(len(sleeps) == 2 and all(0 <= s <= 0.2 for s in sleeps), f"backoff {sleeps}")
    chk.invariants(ctx, u, expect_rows=0)
    chk.that(ctx.quota(u) is None, "quota written by a failed request")
    return chk.result()


def s14_idempotent_transaction_replay(ctx):
    """ClientRequestToken: replaying the same create transaction (same token, same params) must not
    apply twice; the same token with different params must be rejected. Real DynamoDB returns
    success for the replay; local emulators may instead cancel it — both leave one row and
    version 1, which is what this scenario requires everywhere."""
    chk, u = Check("s14_idempotent_transaction_replay"), ctx.user("s14")
    st = ctx.store
    row = {"route": "TPE-LON", "origin": "TPE", "destination": "LON", "departure_month": "2027-01",
           "target_price": 9000, "currency": "TWD", "created_at": "2026-11-05T03:00:00.000Z",
           "updated_at": "2026-11-05T03:00:00.000Z"}
    items = [{"Put": {"TableName": st.subs, "Item": store_mod.to_item({**row, "email": u}),
                      "ConditionExpression": "attribute_not_exists(email)"}},
             st._quota_update(u, None, 1, "2026-11-05T03:00:00.000Z")]
    token = f"b4-{ctx.run_id}-replay"
    ctx.client.transact_write_items(TransactItems=items, ClientRequestToken=token)
    outcome = "success"
    try:
        ctx.client.transact_write_items(TransactItems=items, ClientRequestToken=token)
    except Exception as e:  # noqa: BLE001 - engine-dependent
        outcome = e.response.get("Error", {}).get("Code", "error") if hasattr(e, "response") else "error"
    chk.metrics["replay_same_params"] = outcome
    mismatch = "accepted"
    try:
        other = [dict(items[0]), items[1]]
        other[0] = {"Put": {**items[0]["Put"], "Item": store_mod.to_item({**row, "email": u, "target_price": 1})}}
        ctx.client.transact_write_items(TransactItems=other, ClientRequestToken=token)
    except Exception as e:  # noqa: BLE001
        mismatch = e.response.get("Error", {}).get("Code", "error") if hasattr(e, "response") else "error"
    chk.metrics["replay_different_params"] = mismatch
    rows, q = chk.invariants(ctx, u, expect_rows=1)
    chk.that(q["version"] == 1 and q["count"] == 1, f"replay applied twice: {q}")
    chk.that(rows["TPE-LON"]["target_price"] == 9000, "mismatched replay changed the row")
    if ctx.engine == "aws":  # documented DynamoDB behaviour, required in the cloud run
        chk.that(outcome == "success", f"replay outcome {outcome}")
        chk.that(mismatch == "IdempotentParameterMismatchException", f"mismatch outcome {mismatch}")
    return chk.result()


SCENARIOS = [s01_limit_counts_paused_and_expired, s02_concurrent_creates_from_empty,
             s03_concurrent_creates_at_nine, s04_same_route_created_concurrently, s05_concurrent_deletes,
             s06_deletes_and_creates_at_limit, s07_route_edits_of_one_row, s08_route_edits_onto_one_target,
             s09_edits_pauses_and_creates_at_limit, s10_legacy_and_new_endpoints, s11_m1_quota_initialisation,
             s12_conflict_storm_on_one_quota, s13_bounded_retries_write_nothing, s14_idempotent_transaction_replay]


def run_all(ctx, names=None):
    """Seed a bystander, run the scenarios, then check the bystander is byte-for-byte unchanged (I5)."""
    bystander = ctx.user("bystander")
    ctx.seed(bystander, "TPE-TYO", plan_name="tokyo")
    ctx.seed(bystander, "KHH-OKA", departure_month="2027-02", alerts_paused=True)
    ctx.seed_quota(bystander, 2, 5)
    before = ctx.raw_items(bystander)
    results = []
    for fn in SCENARIOS:
        if names and fn.__name__ not in names:
            continue
        started = time.monotonic()
        try:
            r = fn(ctx)
        except Exception as e:  # a crash is a failure, never a pass
            r = {"scenario": fn.__name__, "passed": False, "failures": [f"crashed: {e!r}"[:300]], "metrics": {}}
        r["seconds"] = round(time.monotonic() - started, 2)
        results.append(r)
    chk = Check("i5_bystander_unchanged")
    chk.that(ctx.raw_items(bystander) == before, "I5 another user's rows or quota changed")
    results.append(chk.result())
    return {"engine": ctx.engine, "run_id": ctx.run_id, "passed": all(r["passed"] for r in results),
            "transactions_committed": ctx.store.committed, "cancellation_reasons": ctx.store.reasons,
            "results": results}
