"""Reference model of the M1.4 strict subscription-quota protocol (API contract §5).

Not deployed and not imported by any Lambda. It is an executable specification: an in-memory
store with DynamoDB-like conditional transactions, and the handler operations written as
generators that ``yield`` before every store call, so tests can interleave concurrent requests
step by step and check that no schedule breaks the invariants:

* a user never has more than ``max_subscriptions`` records (active, paused and expired count);
* a user never has two records for the same route;
* when a quota record exists, its ``count`` equals the user's true number of records.

The real handler (Phase 1B) must follow the same order of reads and the same write conditions.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 0.025
BACKOFF_CAP_S = 0.2
LEGACY_PLANS = {"tokyo": ("TPE", "TYO"), "seoul": ("TPE", "SEL")}


class TransactionCanceled(Exception):
    """Like DynamoDB's TransactionCanceledException: ``index`` is the failed item."""

    def __init__(self, index: int):
        super().__init__(f"condition failed on item {index}")
        self.index = index


@dataclass
class Store:
    subs: dict = field(default_factory=dict)  # (email, route) -> row
    quota: dict = field(default_factory=dict)  # email -> {"count": int, "version": int}

    # Strongly consistent reads.
    def get_quota(self, email):
        q = self.quota.get(email)
        return dict(q) if q else None

    def query(self, email):
        return {r: dict(row) for (e, r), row in self.subs.items() if e == email}

    def transact(self, items):
        """All conditions are checked first; then every item is applied, or none."""
        for i, item in enumerate(items):
            kind = item[0]
            if kind == "put_sub" and item[1] in self.subs:
                raise TransactionCanceled(i)
            if kind in ("delete_sub", "update_sub") and item[1] not in self.subs:
                raise TransactionCanceled(i)
            if kind == "quota":
                _, email, _, expected = item
                cur = self.quota.get(email)
                if (cur is None) != (expected is None) or (cur and cur["version"] != expected):
                    raise TransactionCanceled(i)
        for item in items:
            kind = item[0]
            if kind == "put_sub":
                self.subs[item[1]] = dict(item[2])
            elif kind == "delete_sub":
                del self.subs[item[1]]
            elif kind == "update_sub":
                self.subs[item[1]].update(item[2])
            elif kind == "quota":
                _, email, count, expected = item
                self.quota[email] = {"count": count, "version": (expected or 0) + 1}


def backoff(attempt: int, rng: random.Random) -> float:
    """Full-jitter exponential backoff, bounded: at most BACKOFF_CAP_S per wait."""
    return rng.uniform(0, min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2**attempt))


def _quota_item(email, q, new_count):
    # No record yet (an M1 user): condition attribute_not_exists(email) creates it.
    return ("quota", email, new_count, q["version"] if q else None)


def _read(store, email, order):
    """Quota version FIRST, then the strongly consistent count. Reading in the other order lets a
    write that commits between the two reads go unnoticed (see the wrong-order test)."""
    if order == "quota_first":
        q = store.get_quota(email)
        yield
        rows = store.query(email)
    else:  # "count_first": unsafe, kept only to demonstrate the race
        rows = store.query(email)
        yield
        q = store.get_quota(email)
    return q, rows


def create(store, email, route, row, *, max_subs=10, order="quota_first", sleeps=None, rng=None):
    rng = rng or random.Random(0)
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            (sleeps if sleeps is not None else []).append(backoff(attempt, rng))
        yield
        q, rows = yield from _read(store, email, order)
        if route in rows:
            return 409, "duplicate_subscription"
        if len(rows) >= max_subs:
            return 409, "limit_reached"
        yield
        try:
            store.transact([("put_sub", (email, route), row), _quota_item(email, q, len(rows) + 1)])
            return 201, route
        except TransactionCanceled as e:
            if e.index == 0:
                return 409, "duplicate_subscription"
            # Quota version moved: another create/delete committed. Re-read and retry.
    return 409, "conflict"


def delete(store, email, route, *, sleeps=None, rng=None):
    rng = rng or random.Random(0)
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            (sleeps if sleeps is not None else []).append(backoff(attempt, rng))
        yield
        q, rows = yield from _read(store, email, "quota_first")
        if route not in rows:
            return 404, "not_found"
        yield
        try:
            store.transact([("delete_sub", (email, route)), _quota_item(email, q, len(rows) - 1)])
            return 204, None
        except TransactionCanceled as e:
            if e.index == 0:
                return 404, "not_found"
    return 409, "conflict"


def change_route(store, email, old, new, row):
    """Delete old + Put new in one transaction. The record count is unchanged, so the quota
    record is not touched and the operation never conflicts with creates or deletes."""
    yield
    rows = store.query(email)
    if old not in rows:
        return 404, "not_found"
    if new in rows:
        return 409, "duplicate_subscription"
    yield
    try:
        store.transact([("delete_sub", (email, old)), ("put_sub", (email, new), row)])
        return 200, new
    except TransactionCanceled as e:
        return (404, "not_found") if e.index == 0 else (409, "duplicate_subscription")


def update(store, email, route, fields):
    """Target price, pause/resume or departure month: never touches the quota."""
    yield
    try:
        store.transact([("update_sub", (email, route), fields)])
        return 200, route
    except TransactionCanceled:
        return 404, "not_found"


def legacy_subscribe(store, email, plan, target, *, max_subs=10, sleeps=None, rng=None):
    """POST /subscribe after cutover: the same handler and the same quota path as
    POST /subscriptions. Existing route → update in place (no quota change, M1 upsert semantics
    kept); new route → a quota-checked create."""
    rng = rng or random.Random(0)
    o, d = LEGACY_PLANS[plan]
    route = f"{o}-{d}"
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            (sleeps if sleeps is not None else []).append(backoff(attempt, rng))
        yield
        q, rows = yield from _read(store, email, "quota_first")
        yield
        try:
            if route in rows:
                store.transact([("update_sub", (email, route), {"target_price": target, "plan_name": plan})])
                return 200, route
            if len(rows) >= max_subs:
                return 409, "limit_reached"
            row = {"origin": o, "destination": d, "target_price": target, "plan_name": plan,
                   "departure_month": None}
            store.transact([("put_sub", (email, route), row), _quota_item(email, q, len(rows) + 1)])
            return 200, route
        except TransactionCanceled:
            continue  # row appeared/vanished or version moved: re-read decides create vs update
    return 409, "conflict"


def legacy_unaware_upsert(store, email, plan, target):
    """The deployed M1.1 Lambda as it is today: an unconditional upsert that knows nothing about
    quotas. Only used by tests to show why both endpoints must switch to the quota path together."""
    o, d = LEGACY_PLANS[plan]
    yield
    store.subs.setdefault((email, f"{o}-{d}"), {}).update(
        {"origin": o, "destination": d, "target_price": target, "plan_name": plan})
    return 200, f"{o}-{d}"


def run_schedule(ops, schedule):
    """Advance generator ``ops[i]`` for each i in ``schedule``; then finish the rest in order.
    Returns the list of results."""
    results = [None] * len(ops)
    live = set(range(len(ops)))

    def step(i):
        try:
            next(ops[i])
        except StopIteration as stop:
            results[i] = stop.value
            live.discard(i)

    for i in schedule:
        if i in live:
            step(i)
    for i in range(len(ops)):
        while i in live:
            step(i)
    return results


def all_schedules(make, n_ops, max_len=40):
    """Depth-first exploration of every interleaving. ``make()`` returns (store, ops) fresh each
    time, so each schedule is replayed from the same initial state. Yields (store, results)."""

    def explore(prefix):
        store, ops = make()
        results = [None] * n_ops
        live = set(range(n_ops))
        for i in prefix:
            try:
                next(ops[i])
            except StopIteration as stop:
                results[i] = stop.value
                live.discard(i)
        if not live or len(prefix) >= max_len:
            for i in sorted(live):  # depth cap reached: finish the rest sequentially
                while True:
                    try:
                        next(ops[i])
                    except StopIteration as stop:
                        results[i] = stop.value
                        break
            yield store, results
            return
        for i in sorted(live):
            yield from explore(prefix + [i])

    yield from explore([])


def random_schedules(make, n_ops, count, seed=0):
    """Seeded random interleavings, for cases with too many schedules to enumerate."""
    rng = random.Random(seed)
    for _ in range(count):
        store, ops = make()
        results = [None] * n_ops
        live = list(range(n_ops))
        while live:
            i = rng.choice(live)
            try:
                next(ops[i])
            except StopIteration as stop:
                results[i] = stop.value
                live.remove(i)
        yield store, results
