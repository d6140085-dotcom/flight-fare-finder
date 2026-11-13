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
  I5  other users' rows and quota records are byte-for-byte unchanged;
  I6  (R-MM-1) every sort key agrees with the stored origin, destination and departure month;
  I7  (R-MM-1) no two rows of a user share (origin, destination, departure month).

Scenarios only ever use addresses in the reserved example.com domain and only the tables they
are given; the runner refuses table names that aren't temporary test tables.

R-MM-1 (several departure months per route): fixed-month rows have the sort key
"ORIG-DEST#YYYY-MM" and the public ID "ORIG-DEST_YYYY-MM"; the legacy rolling row keeps
"ORIG-DEST". s16–s21 cover concurrent creates across months, duplicate months, month changes,
edits/deletes of sibling months, rolling/fixed overlap and quota consistency after conflicts.
"""
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from subsapi import config as config_mod
from subsapi import store as store_mod
from subsapi import subkeys
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
MONTHS = ["2026-12"] + [f"2027-{m:02d}" for m in range(1, 12)]  # the 12 selectable months at NOW


def fid(dest, month="2027-01", origin="TPE"):
    """Public ID of a fixed-month subscription."""
    return f"{origin}-{dest}_{month}"


def fkey(dest, month="2027-01", origin="TPE"):
    """Sort key of a fixed-month subscription."""
    return f"{origin}-{dest}#{month}"


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
        """`route` is the sort key ("TPE-TYO" rolling, "TPE-LON#2027-02" fixed month)."""
        o, d, month = subkeys.parse_key(route)
        if month is not None:
            fields.setdefault("departure_month", month)
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
        self.quota_committed = 0  # committed transactions that changed a quota record (create/delete)
        self.per_user = {}
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
            if any(body["TableName"] == self.quota for item in items for body in item.values()):
                self.quota_committed += 1
            whos = {(body.get("Key") or body.get("Item") or {}).get("email", {}).get("S")
                    for item in items for body in item.values()} - {None}
            for who in whos:
                self.per_user[who] = self.per_user.get(who, 0) + 1

    def committed_for(self, email):
        """Committed transactions that touched this user's rows or quota."""
        with self._lock:
            return self.per_user.get(email, 0)


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
            parts = subkeys.parse_key(r["route"])
            # I6 (R-MM-1): the sort key agrees with the stored route and month
            self.that(parts is not None and parts[:2] == (r.get("origin"), r.get("destination"))
                      and (parts[2] is None or parts[2] == r.get("departure_month")),
                      f"I6 {email} {r['route']}: key and attributes disagree")
        # I7 (R-MM-1): no duplicate (route, fixed month), at most one rolling row per route —
        # checked on the stored attributes, independently of how the keys are built
        identities = [(r.get("origin"), r.get("destination"), r.get("departure_month")) for r in rows.values()]
        self.that(len(identities) == len(set(identities)), f"I7 {email}: duplicate route/month {sorted(map(str, identities))}")
        if expect_rows is not None:
            self.that(len(rows) == expect_rows, f"{email}: expected {expect_rows} rows, found {len(rows)}")
        return rows, q

    def responses(self, res, allowed=None):
        """Record the response tally; fail on any unexpected 500, and on any status outside
        ``allowed`` when given."""
        self.metrics["responses"] = tally(res)
        unexpected = [b.get("exception") for s, b in res if s >= 500]
        self.that(not unexpected, f"unexpected 500s: {sorted(set(unexpected))} x{len(unexpected)}")
        if allowed is not None:
            bad = sorted({s for s, _ in res if s not in allowed})
            self.that(not bad, f"statuses outside {sorted(allowed)}: {bad}")

    def result(self):
        return {"scenario": self.name, "passed": not self.failures, "failures": self.failures[:20],
                "metrics": self.metrics}


def parallel(calls):
    """Release every call at the same instant; return results in order.

    ApiError becomes its HTTP status and code (expected outcomes such as 409). Any other
    exception is what the handler would turn into an HTTP 500: it is recorded as
    (500, {"error": "unexpected", "exception": "<type>:<AWS code>"}) instead of crashing the
    scenario, so the scenario still checks database integrity and reports the failure.
    """
    barrier = threading.Barrier(len(calls))

    def go(fn):
        barrier.wait()
        try:
            return fn()
        except ApiError as e:
            return e.status, {"error": e.code}
        except Exception as e:  # noqa: BLE001 - recorded, then failed by Check.responses
            code = e.response.get("Error", {}).get("Code") if hasattr(e, "response") else None
            return 500, {"error": "unexpected", "exception": f"{type(e).__name__}:{code}"}

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(go, calls))


def tally(results):
    out = {}
    for status, body in results:
        k = str(status) if status < 400 else f"{status} {body['error']}"
        if body and body.get("exception"):
            k += f" ({body['exception']})"
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
        ctx.seed(u, fkey(d, extra["departure_month"]), **extra)
    statuses = {svc.view(r, NOW)["status"] for r in ctx.rows(u).values()}
    chk.that(statuses == {"active", "paused", "expired"}, f"seed statuses {statuses}")
    res = parallel([lambda d=d: svc.create(u, create_body(d)) for d in DESTS[10:10 + PARALLEL]])
    chk.responses(res)
    chk.that(all(s == 409 for s, _ in res), "a create succeeded at 10 records")
    chk.invariants(ctx, u, expect_rows=10)
    return chk.result()


def s02_concurrent_creates_from_empty(ctx):
    """60 simultaneous creates by a new user (no quota record)."""
    chk, u, svc = Check("s02_concurrent_creates_from_empty"), ctx.user("s02"), ctx.service()
    res = parallel([lambda d=d: svc.create(u, create_body(d)) for d in DESTS[:PARALLEL]])
    created = [subkeys.key_from_id(b["id"]) for s, b in res if s == 201]
    chk.responses(res)
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
    chk.responses(res)
    chk.that(sum(s == 201 for s, _ in res) <= 1, "more than one create succeeded at 9")
    chk.invariants(ctx, u)
    fill_to_limit(ctx, svc, u, chk)
    return chk.result()


def s04_same_route_created_concurrently(ctx):
    chk, u, svc = Check("s04_same_route_created_concurrently"), ctx.user("s04"), ctx.service()
    res = parallel([lambda: svc.create(u, create_body("LON")) for _ in range(PARALLEL)])
    chk.responses(res)
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
    routes = [fid(d) for d in DESTS[:10]]
    calls = [lambda r=r: svc.delete(u, r) for r in routes for _ in range(6)]
    res = parallel(calls)
    chk.responses(res)
    per_route = {r: 0 for r in routes}
    for (status, _), call_route in zip(res, [r for r in routes for _ in range(6)]):
        if status == 204:
            per_route[call_route] += 1
    chk.that(all(n <= 1 for n in per_route.values()), f"a row was deleted twice: {per_route}")
    rows, q = chk.invariants(ctx, u, expect_rows=10 - sum(per_route.values()))
    chk.that(q["version"] == 10 + sum(per_route.values()), f"I3 version {q['version']}")
    for r in list(rows):
        svc.delete(u, subkeys.public_id(r))
    rows, q = chk.invariants(ctx, u, expect_rows=0)
    chk.that(q["count"] == 0 and q["version"] == 20, f"after cleanup {q}")
    return chk.result()


def s06_deletes_and_creates_at_limit(ctx):
    chk, u, svc = Check("s06_deletes_and_creates_at_limit"), ctx.user("s06"), ctx.service()
    for d in DESTS[:10]:
        svc.create(u, create_body(d))
    deletes = [lambda d=d: svc.delete(u, fid(d)) for d in DESTS[:5]]
    creates = [lambda d=d: svc.create(u, create_body(d)) for d in DESTS[10:65]]
    res = parallel(deletes + creates)
    deleted = sum(s == 204 for s, _ in res[:5])
    created = sum(s == 201 for s, _ in res[5:])
    chk.responses(res)
    chk.metrics.update(deleted=deleted, created=created)
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
    res = parallel([lambda d=d: svc.patch(u, fid("OSA"), {"origin": "KHH", "destination": d}) for d in targets])
    chk.responses(res)
    winners = [subkeys.key_from_id(b["id"]) for s, b in res if s == 200]
    chk.that(len(winners) == 1, f"{len(winners)} route edits succeeded")
    chk.that(all(s == 200 or b["error"] in ("not_found", "conflict") for s, b in res), "bad error")
    rows, q = chk.invariants(ctx, u, expect_rows=9)
    chk.that(fkey("OSA") not in rows and all(w in rows for w in winners), "old route remains or winner missing")
    chk.that(q["version"] == 9, f"route edits touched the quota: version {q['version']}")
    return chk.result()


def s08_route_edits_onto_one_target(ctx):
    """10 rows; 60 route changes (6 per row) all onto TPE-LON: exactly one row becomes TPE-LON."""
    chk, u, svc = Check("s08_route_edits_onto_one_target"), ctx.user("s08"), ctx.service()
    for d in DESTS[:10]:
        svc.create(u, create_body(d))
    calls = [lambda d=d: svc.patch(u, fid(d), {"origin": "TPE", "destination": "LON"})
             for d in DESTS[:10] for _ in range(6)]
    res = parallel(calls)
    chk.responses(res)
    chk.that(sum(s == 200 for s, _ in res) == 1, "not exactly one row moved to TPE-LON")
    chk.that(all(s == 200 or b["error"] in ("duplicate_subscription", "not_found", "conflict") for s, b in res),
             "bad error")
    rows, q = chk.invariants(ctx, u, expect_rows=10)
    chk.that(fkey("LON") in rows, "TPE-LON 2027-01 missing")
    chk.that(q["version"] == 10, f"version {q['version']}")
    return chk.result()


def s09_edits_pauses_and_creates_at_limit(ctx):
    """At 10: 20 pauses/resumes + 20 in-place edits (target, and the unchanged month) + 20 creates.
    Edits never need a slot. Unchanged from the 2026-11-06 suite: every edit and pause addresses a
    row that stays at its key, so only 200/409 are acceptable (never 404). Month edits, which
    move a row since R-MM-1, race pauses in s22."""
    chk, u, svc = Check("s09_edits_pauses_and_creates_at_limit"), ctx.user("s09"), ctx.service()
    for d in DESTS[:10]:
        svc.create(u, create_body(d))
    pauses = [lambda i=i: svc.patch(u, fid(DESTS[i % 10]), {"alerts_paused": i % 2 == 0}) for i in range(20)]
    edits = [lambda i=i: svc.patch(u, fid(DESTS[i % 10]), {"target_price": 5000 + i, "departure_month": "2027-01"})
             for i in range(20)]
    creates = [lambda d=d: svc.create(u, create_body(d)) for d in DESTS[10:30]]
    res = parallel(pauses + edits + creates)
    chk.responses(res)
    chk.that(all(s in (200, 409) for s, _ in res[:40]), "edit/pause returned an unexpected status")
    chk.that(all(s == 409 for s, _ in res[40:]), "a create succeeded at 10")
    rows, q = chk.invariants(ctx, u, expect_rows=10)
    chk.that(sorted(rows) == sorted(fkey(d) for d in DESTS[:10]), "an in-place edit moved or lost a row")
    chk.that(q["version"] == 10, f"edits touched the quota: version {q['version']}")
    chk.that(ctx.store.committed_for(u) == 10, "an in-place edit or pause used a transaction")
    return chk.result()


def s10_legacy_and_new_endpoints(ctx):
    """At 9 (TPE-TYO absent): 30 legacy POST /subscribe (tokyo) + 30 POST /subscriptions."""
    chk, u, svc = Check("s10_legacy_and_new_endpoints"), ctx.user("s10"), ctx.service()
    for d in DESTS[:9]:
        svc.create(u, create_body(d))
    legacy = [lambda: svc.legacy_subscribe(u, {"plan_name": "tokyo", "target_price": 8000}) for _ in range(30)]
    creates = [lambda d=d: svc.create(u, create_body(d)) for d in DESTS[9:39]]
    res = parallel(legacy + creates)
    chk.responses(res)
    chk.that(all(st in (200, 409) for st, _ in res[:30]), "legacy: status other than 200/409")
    chk.that(all(st in (201, 409) for st, _ in res[30:]), "new endpoint: status other than 201/409")
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
    chk.responses(res)
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
    calls = [lambda d=d: svc.delete(u, fid(d)) for d in DESTS[:5]]
    calls += [lambda d=d: svc.create(u, create_body(d)) for d in DESTS[5:60]]
    res = parallel(calls)
    reasons = {k: v - before.get(k, 0) for k, v in ctx.store.reasons.items() if v - before.get(k, 0)}
    chk.responses(res)
    chk.metrics.update(cancellation_reasons=reasons,
                       committed=ctx.store.committed - before_committed)
    rows, q = chk.invariants(ctx, u)
    ok = sum(s in (201, 204) for s, _ in res)
    chk.that(q["version"] == 5 + ok, f"I3 version {q['version']} != {5 + ok}")
    created = {subkeys.key_from_id(b["id"]) for s, b in res if s == 201}
    deleted = {fkey(DESTS[i]) for i, (s, _) in enumerate(res[:5]) if s == 204}
    expected = ({fkey(d) for d in DESTS[:5]} - deleted) | created
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
    row = {"route": fkey("LON"), "origin": "TPE", "destination": "LON", "departure_month": "2027-01",
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
    chk.that(rows[fkey("LON")]["target_price"] == 9000, "mismatched replay changed the row")
    if ctx.engine == "aws":  # documented DynamoDB behaviour, required in the cloud run
        chk.that(outcome == "success", f"replay outcome {outcome}")
        chk.that(mismatch == "IdempotentParameterMismatchException", f"mismatch outcome {mismatch}")
    return chk.result()


def s15_same_row_write_storm(ctx):
    """60 simultaneous writes to ONE row (TPE-TYO): 20 legacy upserts, 20 PATCH edits (target,
    month, pause/resume), 10 deletes and 10 new-endpoint creates. On real DynamoDB the
    single-item updates collide with the create/delete transactions on that row
    (TransactionConflictException — the s10 finding of 2026-11-06). Every response must be an
    expected status, never an unexpected 500, and rows and quota must stay consistent.

    Since R-MM-1 the creates (TPE-TYO 2027-02) are a different record from the rolling row, and a
    month edit moves the rolling row to TPE-TYO#2027-04 (a key-change transaction that does not
    touch the quota); a later legacy upsert may then create a new rolling row."""
    chk, u, svc = Check("s15_same_row_write_storm"), ctx.user("s15"), ctx.service()
    svc.legacy_subscribe(u, {"plan_name": "tokyo", "target_price": 9000})
    for d in DESTS[:5]:
        svc.create(u, create_body(d))
    committed_before, quota_before = ctx.store.committed, ctx.store.quota_committed
    legacy = [lambda i=i: svc.legacy_subscribe(u, {"plan_name": "tokyo", "target_price": 7000 + i}) for i in range(20)]
    edits = [lambda i=i: svc.patch(u, "TPE-TYO", [{"target_price": 6000 + i}, {"departure_month": "2027-04"},
                                                  {"alerts_paused": True}, {"alerts_paused": False}][i % 4])
             for i in range(20)]
    deletes = [lambda: svc.delete(u, "TPE-TYO") for _ in range(10)]
    creates = [lambda: svc.create(u, create_body("TYO", month="2027-02")) for _ in range(10)]
    res = parallel(legacy + edits + deletes + creates)
    chk.responses(res)
    chk.that(all(st in (200, 409) for st, _ in res[:20]), "legacy: status other than 200/409")
    chk.that(all(st in (200, 404, 409) for st, _ in res[20:40]), "PATCH: status other than 200/404/409")
    chk.that(all(st in (204, 404, 409) for st, _ in res[40:50]), "DELETE: status other than 204/404/409")
    chk.that(all(st in (201, 409) for st, _ in res[50:]), "create: status other than 201/409")
    rows, q = chk.invariants(ctx, u)
    # I3 exactly as in the original s15: the quota version advanced by exactly the committed
    # count-changing transactions (creates, legacy creates, deletes). Before R-MM-1 every
    # committed transaction here was count-changing, so "all commits" and "count-changing
    # commits" were the same number; a month edit is now a key-change transaction that must NOT
    # move the version, so the two are counted separately and both are checked.
    commits = ctx.store.quota_committed - quota_before
    key_changes = (ctx.store.committed - committed_before) - commits
    deleted = sum(st == 204 for st, _ in res[40:50])
    created = sum(st == 201 for st, _ in res[50:])
    moved = sum(st == 200 for i, (st, _) in enumerate(res[20:40]) if i % 4 == 1 and _ and _.get("id") != "TPE-TYO")
    chk.that(q["version"] == 6 + commits, f"I3 version {q['version']} != 6 + {commits}")
    chk.that(key_changes == moved, f"key-change commits {key_changes} != successful month moves {moved}")
    # Exact row accounting: every count-changing commit is +1 (create) or -1 (delete).
    chk.that(len(rows) == 6 + commits - 2 * deleted, f"rows {len(rows)} != 6 + {commits} - 2*{deleted}")
    chk.that(created <= 1, "TPE-TYO 2027-02 created twice")
    chk.that(moved <= 1, "the rolling row moved twice")
    # No partial move: a moved row exists exactly once, at its new key, with its provenance.
    if moved:
        chk.that(rows.get("TPE-TYO#2027-04", {}).get("previous_route") == "TPE-TYO", "move without its new row")
    chk.that(5 <= len(rows) <= 8, f"{len(rows)} rows")  # 6 - 1 delete .. 6 + create + move-then-legacy-create
    chk.metrics.update(committed=ctx.store.committed - committed_before, quota_committed=commits,
                       key_changes=key_changes, deleted=deleted, created=created, moved=moved)
    return chk.result()


# ======================================================================================
# R-MM-1 scenarios: several departure months per route
# ======================================================================================
def s16_concurrent_creates_across_months(ctx):
    """60 simultaneous creates of ONE route across all 12 months (5 per month), from empty:
    each month is created at most once, never more than 10 records; then exactly 10, the 11th
    month refused."""
    chk, u, svc = Check("s16_concurrent_creates_across_months"), ctx.user("s16"), ctx.service()
    res = parallel([lambda i=i: svc.create(u, create_body("TYO", month=MONTHS[i % 12], origin="KHH"))
                    for i in range(PARALLEL)])
    chk.responses(res, allowed={201, 409})
    created = [subkeys.key_from_id(b["id"]) for s, b in res if s == 201]
    chk.that(len(created) == len(set(created)), f"a month was created twice: {sorted(created)}")
    chk.that(1 <= len(created) <= 10, f"{len(created)} creates succeeded")
    chk.that(all(s == 201 or b["error"] in ("duplicate_subscription", "limit_reached", "conflict") for s, b in res),
             "bad error")
    rows, q = chk.invariants(ctx, u)
    chk.that(sorted(rows) == sorted(created), "I4 rows don't match the 201 responses")
    chk.that(q["version"] == len(created), f"I3 version {q['version']} != {len(created)}")
    for m in MONTHS:  # sequential follow-up
        try:
            svc.create(u, create_body("TYO", month=m, origin="KHH"))
        except ApiError:
            pass
    rows, q = chk.invariants(ctx, u, expect_rows=10)
    chk.that(all(k.startswith("KHH-TYO#") for k in rows), "unexpected keys")
    return chk.result()


def s17_duplicate_month_beside_other_months(ctx):
    """KHH-TYO 2027-01 and 2027-02 exist; 60 simultaneous creates of 2027-03: exactly one wins,
    the other months are untouched."""
    chk, u, svc = Check("s17_duplicate_month_beside_other_months"), ctx.user("s17"), ctx.service()
    for m in ("2027-01", "2027-02"):
        svc.create(u, create_body("TYO", month=m, origin="KHH"))
    before = {k: r["updated_at"] for k, r in ctx.rows(u).items()}
    res = parallel([lambda: svc.create(u, create_body("TYO", month="2027-03", origin="KHH")) for _ in range(PARALLEL)])
    chk.responses(res, allowed={201, 409})
    chk.that(sum(s == 201 for s, _ in res) == 1, "not exactly one create succeeded")
    chk.that(all(s == 201 or b["error"] in ("duplicate_subscription", "conflict") for s, b in res), "bad error")
    rows, q = chk.invariants(ctx, u, expect_rows=3)
    chk.that(all(rows[k]["updated_at"] == v for k, v in before.items()), "another month was modified")
    chk.that(q["version"] == 3, f"I3 version {q['version']}")
    return chk.result()


def s18_concurrent_month_changes(ctx):
    """5 months of one route (2027-01..05); 60 simultaneous month changes (12 per row) onto
    2027-06 and 2027-07: at most one row lands on each target month, the record count and the
    quota never change."""
    chk, u, svc = Check("s18_concurrent_month_changes"), ctx.user("s18"), ctx.service()
    src = ["2027-01", "2027-02", "2027-03", "2027-04", "2027-05"]
    for m in src:
        svc.create(u, create_body("TYO", month=m, origin="KHH"))
    quota_before = ctx.quota(u)
    calls = [lambda m=m, i=i: svc.patch(u, fid("TYO", m, "KHH"), {"departure_month": ["2027-06", "2027-07"][i % 2]})
             for m in src for i in range(12)]
    res = parallel(calls)
    chk.responses(res, allowed={200, 404, 409})
    chk.that(all(s == 200 or b["error"] in ("duplicate_subscription", "not_found", "conflict") for s, b in res),
             "bad error")
    rows, q = chk.invariants(ctx, u, expect_rows=5)
    months_now = sorted(r["departure_month"] for r in rows.values())
    chk.that(len(months_now) == len(set(months_now)), f"two rows share a month: {months_now}")
    chk.that(sum(m in ("2027-06", "2027-07") for m in months_now) == sum(s == 200 for s, _ in res),
             "200 responses don't match the moved rows")
    chk.that(q == quota_before, f"month changes touched the quota: {q} vs {quota_before}")
    return chk.result()


def s19_edits_and_deletes_of_sibling_months(ctx):
    """KHH-TYO 2027-01 (A), 02, 03 (B) + 7 other routes (10 rows). 60 simultaneous requests:
    15 deletes of A carrying A's version (as the page sends it), target/pause edits of 2027-02,
    15 moves of B onto 2027-01 (racing A's delete: possible only once A is gone, so B then
    reuses A's ID), and creates of more months. A delete aimed at A must never remove B
    (subscription ID reuse / ABA, Stage B 2026-11-13): at most one delete succeeds and it is
    A's; a moved B survives at A's ID with a new version and its provenance; no record lost or
    duplicated."""
    chk, u, svc = Check("s19_edits_and_deletes_of_sibling_months"), ctx.user("s19"), ctx.service()
    for m in ("2027-01", "2027-02", "2027-03"):
        svc.create(u, create_body("TYO", month=m, origin="KHH"))
    for d in DESTS[:7]:
        svc.create(u, create_body(d))
    key_a, key_b = fkey("TYO", "2027-01", "KHH"), fkey("TYO", "2027-03", "KHH")
    v_a = ctx.rows(u)[key_a]["updated_at"]
    deletes = [lambda: svc.delete(u, fid("TYO", "2027-01", "KHH"), v_a) for _ in range(15)]
    edits = [lambda i=i: svc.patch(u, fid("TYO", "2027-02", "KHH"),
                                   [{"target_price": 4000 + i}, {"alerts_paused": True}, {"alerts_paused": False}][i % 3])
             for i in range(15)]
    moves = [lambda: svc.patch(u, fid("TYO", "2027-03", "KHH"), {"departure_month": "2027-01"}) for _ in range(15)]
    creates = [lambda i=i: svc.create(u, create_body("TYO", month=MONTHS[4 + i % 6], origin="KHH")) for i in range(15)]
    res = parallel(deletes + edits + moves + creates)
    chk.responses(res)
    deleted = sum(s == 204 for s, _ in res[:15])
    moved = sum(s == 200 for s, _ in res[30:45])
    created = sum(s == 201 for s, _ in res[45:])
    chk.that(all(s in (204, 404, 409) for s, _ in res[:15]), "delete: unexpected status")
    chk.that(deleted <= 1, "A was deleted twice, or a delete aimed at A removed another subscription")
    chk.that(moved <= 1, "2027-03 moved twice")
    chk.that(moved <= deleted, "B moved into A's ID while A still existed")
    chk.that(all(s in (200, 404, 409) for s, _ in res[15:45]), "edit/move: unexpected status")
    chk.that(all(s in (201, 409) for s, _ in res[45:]), "create: unexpected status")
    chk.that(created <= deleted, "more creates than freed slots")
    rows, q = chk.invariants(ctx, u, expect_rows=10 - deleted + created)
    at_a = rows.get(key_a)
    if moved:      # B lives at A's ID: a different subscription, with a new version and provenance
        chk.that(at_a is not None and at_a.get("previous_route") == key_b and at_a.get("updated_at") != v_a,
                 "the subscription moved into A's ID was lost or is indistinguishable from A")
        chk.that(key_b not in rows, "partial move: B still at 2027-03")
    elif deleted:  # A gone, B untouched at 2027-03
        chk.that(at_a is None and key_b in rows, "after A's delete: wrong rows at 2027-01/2027-03")
    else:          # nothing deleted, so nothing could move in
        chk.that(at_a is not None and at_a.get("updated_at") == v_a and key_b in rows, "A or B changed without a 2xx")
    chk.that(fkey("TYO", "2027-02", "KHH") in rows, "the edited sibling month disappeared")
    chk.that(q["version"] == 10 + deleted + created, f"I3 version {q['version']}")
    chk.metrics.update(deleted=deleted, moved=moved, created=created)
    return chk.result()


def s20_rolling_and_fixed_overlap(ctx):
    """An M1 user (rolling TPE-TYO + TPE-SEL, no quota record). 60 simultaneous requests: creates
    of TPE-TYO for 2026-12 (the rolling row's effective month) and 2027-01, legacy upserts of
    the rolling row, and moves of the rolling row onto 2026-12. Rolling and fixed rows of one
    route coexist; the rolling row can never be merged into an existing fixed month."""
    chk, u, svc = Check("s20_rolling_and_fixed_overlap"), ctx.user("s20"), ctx.service()
    ctx.seed(u, "TPE-TYO", plan_name="tokyo")
    ctx.seed(u, "TPE-SEL", plan_name="seoul", target_price=7000)
    creates = [lambda i=i: svc.create(u, create_body("TYO", month=["2026-12", "2027-01"][i % 2])) for i in range(20)]
    legacy = [lambda i=i: svc.legacy_subscribe(u, {"plan_name": "tokyo", "target_price": 8000 + i}) for i in range(20)]
    moves = [lambda: svc.patch(u, "TPE-TYO", {"departure_month": "2026-12"}) for _ in range(20)]
    quota_before = ctx.store.quota_committed
    res = parallel(creates + legacy + moves)
    chk.responses(res)
    chk.that(all(s in (201, 409) for s, _ in res[:20]), "create: unexpected status")
    chk.that(all(s in (200, 409) for s, _ in res[20:40]), "legacy: unexpected status")
    chk.that(all(s in (200, 404, 409) for s, _ in res[40:]), "move: unexpected status")
    rows, q = chk.invariants(ctx, u)
    created = {subkeys.key_from_id(b["id"]) for s, b in res[:20] if s == 201}
    chk.that(len(created) == sum(s == 201 for s, _ in res[:20]), "a fixed month was created twice")
    moved = sum(s == 200 for s, _ in res[40:])
    chk.that(moved <= 1, f"the rolling row moved {moved} times")
    chk.that(not (moved and "TPE-TYO#2026-12" in created), "the rolling row was merged into an existing month")
    if moved:
        chk.that(rows.get("TPE-TYO#2026-12", {}).get("previous_route") == "TPE-TYO",
                 "move reported but the row isn't at 2026-12")
    chk.that("TPE-SEL" in rows and "departure_month" not in rows["TPE-SEL"], "the other M1 row changed")
    if "TPE-TYO" in rows:
        chk.that("departure_month" not in rows["TPE-TYO"], "the rolling row gained a month")
    commits = ctx.store.quota_committed - quota_before
    if q is not None:
        chk.that(q["version"] == commits, f"I3 version {q['version']} != {commits}")
    else:
        chk.that(commits == 0, "quota commits without a quota record")
    chk.metrics.update(rows=sorted(rows), moved=moved, quota_commits=commits)
    return chk.result()


def s21_quota_consistency_after_conflicts(ctx):
    """One user, 60 simultaneous mixed requests: creates of distinct months of two routes, deletes,
    month changes and route changes. Afterwards the quota version equals the number of committed
    count-changing transactions, the count equals the rows, and every row matches a successful
    response or the seed."""
    chk, u, svc = Check("s21_quota_consistency_after_conflicts"), ctx.user("s21"), ctx.service()
    for m in ("2027-01", "2027-02", "2027-03", "2027-04"):
        svc.create(u, create_body("TYO", month=m, origin="KHH"))
    quota_before = ctx.store.quota_committed
    calls = [lambda m=m: svc.delete(u, fid("TYO", m, "KHH")) for m in ("2027-01", "2027-02")]
    calls += [lambda: svc.patch(u, fid("TYO", "2027-03", "KHH"), {"departure_month": "2027-09"}) for _ in range(6)]
    calls += [lambda: svc.patch(u, fid("TYO", "2027-04", "KHH"), {"origin": "TPE", "destination": "TYO"}) for _ in range(6)]
    calls += [lambda i=i: svc.create(u, create_body(["OKA", "SEL"][i % 2], month=MONTHS[i % 12], origin="KHH"))
              for i in range(46)]
    res = parallel(calls)
    chk.responses(res)
    rows, q = chk.invariants(ctx, u)
    commits = ctx.store.quota_committed - quota_before
    chk.that(q["version"] == 4 + commits, f"I3 version {q['version']} != 4 + {commits}")
    chk.that(len(rows) <= 10, "I1")
    deleted = sum(s == 204 for s, _ in res[:2])
    created = sum(s == 201 for s, _ in res[14:])
    chk.that(len(rows) == 4 - deleted + created, f"rows {len(rows)} != 4 - {deleted} + {created}")
    chk.that(sum(s == 200 for s, _ in res[2:8]) <= 1 and sum(s == 200 for s, _ in res[8:14]) <= 1,
             "a row was moved twice")
    chk.metrics.update(deleted=deleted, created=created, quota_commits=commits)
    return chk.result()


def s22_month_moves_race_pauses_at_limit(ctx):
    """The month-moving half of the original s09, with attributed outcomes. At 10: 20 pauses + 20
    month moves (2 per row, to DIFFERENT months, 2027-03 and 2027-04) + 20 creates. A 404 is
    accepted ONLY for a request whose row was moved by another request; every original route
    exists exactly once afterwards, at its old month or at one of the two new months — never two
    of them, never none. (Two destinations make a move that skipped its updated_at guard visible:
    both moves would succeed and the row would be duplicated.)"""
    chk, u, svc = Check("s22_month_moves_race_pauses_at_limit"), ctx.user("s22"), ctx.service()
    for d in DESTS[:10]:
        svc.create(u, create_body(d))
    pauses = [lambda i=i: svc.patch(u, fid(DESTS[i % 10]), {"alerts_paused": i % 2 == 0}) for i in range(20)]
    moves = [lambda i=i: svc.patch(u, fid(DESTS[i % 10]), {"target_price": 5000 + i,
                                                          "departure_month": "2027-03" if i < 10 else "2027-04"})
             for i in range(20)]
    creates = [lambda d=d: svc.create(u, create_body(d)) for d in DESTS[10:30]]
    res = parallel(pauses + moves + creates)
    chk.responses(res)
    chk.that(all(s == 409 for s, _ in res[40:]), "a create succeeded at 10")
    rows, q = chk.invariants(ctx, u, expect_rows=10)
    for j, d in enumerate(DESTS[:10]):
        old = fkey(d)
        present = [k for k in (old, fkey(d, "2027-03"), fkey(d, "2027-04")) if k in rows]
        chk.that(len(present) == 1, f"{d}: row at {present} (partial or duplicated move)")
        new = present[0] if present and present[0] != old else None
        moved_ok = sum(1 for i in range(20) if i % 10 == j and res[20 + i][0] == 200)
        chk.that(moved_ok == int(new is not None), f"{d}: {moved_ok} successful moves but moved to {new}")
        for i in range(20):
            if i % 10 != j:
                continue
            for st, label in ((res[i][0], "pause"), (res[20 + i][0], "move")):
                chk.that(st in (200, 409) or (st == 404 and new is not None), f"{d}: {label} {st} on a row that never moved")
        if new is not None:
            chk.that(rows[new].get("previous_route") == old, f"{d}: moved row lost its provenance")
    chk.that(q["version"] == 10, f"moves or pauses touched the quota: version {q['version']}")
    return chk.result()


def s23_month_moves_race_deletes_of_one_row(ctx):
    """30 month moves and 30 deletes of the SAME row, beside 4 sibling months. Either the row is
    deleted (from its old or new key) or it ends up at exactly one key; quota and rows agree."""
    chk, u, svc = Check("s23_month_moves_race_deletes_of_one_row"), ctx.user("s23"), ctx.service()
    for m in ("2027-01", "2027-02", "2027-03", "2027-04", "2027-05"):
        svc.create(u, create_body("TYO", month=m, origin="KHH"))
    target = fid("TYO", "2027-01", "KHH")
    quota_before = ctx.store.quota_committed
    moves = [lambda: svc.patch(u, target, {"departure_month": "2027-09"}) for _ in range(30)]
    deletes = [lambda: svc.delete(u, target) for _ in range(30)]
    res = parallel(moves + deletes)
    chk.responses(res)
    chk.that(all(s in (200, 404, 409) for s, _ in res[:30]), "move: unexpected status")
    chk.that(all(s in (204, 404, 409) for s, _ in res[30:]), "delete: unexpected status")
    moved, deleted = sum(s == 200 for s, _ in res[:30]), sum(s == 204 for s, _ in res[30:])
    chk.that(moved <= 1 and deleted <= 1, f"moved {moved}, deleted {deleted}")
    rows, q = chk.invariants(ctx, u, expect_rows=5 - deleted)
    old, new = fkey("TYO", "2027-01", "KHH"), fkey("TYO", "2027-09", "KHH")
    chk.that(not (old in rows and new in rows), "the row exists at both keys")
    chk.that((old in rows or new in rows) == (deleted == 0), "row presence disagrees with the delete result")
    chk.that(new not in rows or moved == 1, "a row at the new key without a successful move")
    chk.that(q["count"] == 5 - deleted and q["version"] == 5 + deleted, f"quota {q}")
    chk.that(ctx.store.quota_committed - quota_before == deleted, "quota commits != deletes")
    return chk.result()


def s24_failed_key_changes_write_nothing(ctx):
    """Every month/route-change transaction is cancelled (TransactionConflict, or the Put's
    condition): 409, no row moved, nothing half-written, quota unchanged; the row is then still
    editable. The atomic counterpart of s13 for key changes."""
    chk, u = Check("s24_failed_key_changes_write_nothing"), ctx.user("s24")
    svc = ctx.service(sleep=lambda s: None)
    for m in ("2027-01", "2027-02"):
        svc.create(u, create_body("TYO", month=m, origin="KHH"))
    before_rows, before_q = ctx.rows(u), ctx.quota(u)
    real = ctx.store.change_route
    outcomes = []
    for reasons in (["TransactionConflict", "None"], ["None", "TransactionConflict"], ["ConditionalCheckFailed", "None"]):
        ctx.store.change_route = lambda *a, r=reasons, **kw: (_ for _ in ()).throw(store_mod.TxCancelled(r))
        for body in ({"departure_month": "2027-06"}, {"origin": "TPE", "destination": "TYO"}):
            try:
                svc.patch(u, fid("TYO", "2027-01", "KHH"), body)
                outcomes.append("200")
            except ApiError as e:
                outcomes.append(e.code)
    ctx.store.change_route = real
    chk.that(outcomes == ["conflict"] * 6, f"outcomes {outcomes}")
    chk.that(ctx.rows(u) == before_rows and ctx.quota(u) == before_q, "a failed key change wrote something")
    # Real (not injected) cancellation: a move onto an occupied sibling month. The API refuses it
    # up front (409); the transaction itself must refuse it too when that pre-check is raced, so
    # the store is also called directly — its Put condition fails and the Delete must not happen.
    old_key = fkey("TYO", "2027-01", "KHH")
    try:
        svc.patch(u, fid("TYO", "2027-01", "KHH"), {"departure_month": "2027-02"})
        chk.that(False, "a move onto an occupied month succeeded")
    except ApiError as e:
        chk.that(e.code in ("duplicate_subscription", "conflict"), f"occupied month: {e.code}")
    try:
        ctx.store.change_route(u, old_key, before_rows[old_key]["updated_at"],
                               {**before_rows[old_key], "route": fkey("TYO", "2027-02", "KHH"), "departure_month": "2027-02"})
        chk.that(False, "the transaction moved a row onto an occupied month")
    except store_mod.TxCancelled:
        pass
    chk.that(ctx.rows(u) == before_rows and ctx.quota(u) == before_q, "a refused move deleted or overwrote a row")
    # Stale guard: a key change carrying an outdated updated_at (another write landed after the
    # read) must be cancelled, not overwrite that write.
    stale = before_rows[old_key]["updated_at"]
    try:
        svc.patch(u, fid("TYO", "2027-01", "KHH"), {"alerts_paused": True})
    except ApiError as e:
        chk.that(False, f"the row is gone after refused moves: {e.code}")
        return chk.result()
    try:
        ctx.store.change_route(u, old_key, stale, {**before_rows[old_key], "route": fkey("TYO", "2027-07", "KHH"),
                                                   "departure_month": "2027-07"})
        chk.that(False, "a stale key change committed")
    except store_mod.TxCancelled:
        pass
    after = ctx.rows(u)
    chk.that(sorted(after) == sorted(before_rows) and after[old_key].get("alerts_paused") is True,
             "a stale key change lost the concurrent pause")
    # A successful move changes the key and nothing in the quota (count and version).
    try:
        body = svc.patch(u, fid("TYO", "2027-01", "KHH"), {"departure_month": "2027-06"})[1]
        chk.that(body["id"] == "KHH-TYO_2027-06", "the follow-up move failed")
    except ApiError as e:
        chk.that(False, f"follow-up move: {e.code}")
    chk.that(ctx.quota(u) == before_q, f"a successful move touched the quota: {before_q} -> {ctx.quota(u)}")
    chk.invariants(ctx, u, expect_rows=2)
    return chk.result()


SCENARIOS = [s01_limit_counts_paused_and_expired, s02_concurrent_creates_from_empty,
             s03_concurrent_creates_at_nine, s04_same_route_created_concurrently, s05_concurrent_deletes,
             s06_deletes_and_creates_at_limit, s07_route_edits_of_one_row, s08_route_edits_onto_one_target,
             s09_edits_pauses_and_creates_at_limit, s10_legacy_and_new_endpoints, s11_m1_quota_initialisation,
             s12_conflict_storm_on_one_quota, s13_bounded_retries_write_nothing, s14_idempotent_transaction_replay,
             s15_same_row_write_storm, s16_concurrent_creates_across_months,
             s17_duplicate_month_beside_other_months, s18_concurrent_month_changes,
             s19_edits_and_deletes_of_sibling_months, s20_rolling_and_fixed_overlap,
             s21_quota_consistency_after_conflicts, s22_month_moves_race_pauses_at_limit,
             s23_month_moves_race_deletes_of_one_row, s24_failed_key_changes_write_nothing]


def run_all(ctx, names=None):
    """Seed a bystander, run the scenarios, then check the bystander is byte-for-byte unchanged (I5)."""
    bystander = ctx.user("bystander")
    ctx.seed(bystander, "TPE-TYO", plan_name="tokyo")
    ctx.seed(bystander, "KHH-OKA#2027-02", alerts_paused=True)
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
