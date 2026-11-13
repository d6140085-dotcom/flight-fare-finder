"""Subscription operations (contract §4, §5, §8.2). Every method returns (status, body) or
raises ApiError. The clock, sleep and randomness are injectable for tests."""
import hashlib
import json
import random
import secrets
import time
from datetime import datetime, timedelta, timezone

from . import subkeys
from . import validation as v
from .errors import ApiError
from .months import current_month, effective_month, is_expired, selectable_months
from .store import TOKEN_ATTR, ConditionFailed, TxCancelled, WriteConflict, token_current

MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 0.025
BACKOFF_CAP_S = 0.2
LEGACY_PLANS = {"tokyo": ("TPE", "TYO"), "seoul": ("TPE", "SEL")}


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class Service:
    def __init__(self, store, cfg, clock=None, sleep=time.sleep, rng=None, new_token=None):
        self.store = store
        self.cfg = cfg
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.rng = rng or random.Random()
        # Row version tokens: 128 random bits from the OS CSPRNG, never derived from the clock.
        self.new_token = new_token or (lambda: secrets.token_urlsafe(16))

    @staticmethod
    def version_of(row):
        """The public, opaque row version (contract §4.5): the stored token when M1.4 wrote the
        row last, otherwise (M1 row, or a row the M1 Lambda changed since) ``legacy-`` + a digest
        of every stored attribute."""
        if token_current(row):
            return row[TOKEN_ATTR]
        body = json.dumps({k: v for k, v in row.items() if k != "email"}, sort_keys=True, default=str)
        return "legacy-" + hashlib.sha256(body.encode()).hexdigest()[:32]

    # ---- helpers -----------------------------------------------------------------------
    def _stamp(self, after=None):
        """A new updated_at, strictly later than ``after`` (it doubles as the concurrency token)."""
        now = self.clock()
        s = _iso(now)
        if after and s <= after:
            try:
                prev = datetime.strptime(after, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
                s = _iso(prev + timedelta(milliseconds=1))
            except ValueError:
                pass  # legacy second-precision stamps compare lower than any new stamp
        return s

    def _backoff(self, attempt):
        if attempt:
            self.sleep(self.rng.uniform(0, min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2**attempt)))

    def _name(self, code):
        p = self.cfg.place(code)
        return {"zh": p["name_zh"], "en": p["name_en"]} if p else None

    def view(self, row, now):
        """Normalise a stored row (M1 or M1.4) to the API shape (§3.1, §8.1). `id` is the public,
        URL-safe subscription ID (subkeys.public_id of the sort key); `route` is the route pair."""
        key = row["route"]
        parts = subkeys.parse_key(key) or (None, None, None)
        o, d = row.get("origin") or parts[0], row.get("destination") or parts[1]
        month = row.get("departure_month")
        route = subkeys.pair(o, d)
        paused = bool(row.get("alerts_paused", False))
        status = "expired" if is_expired(month, now) else ("paused" if paused else "active")
        return {
            "id": subkeys.public_id(key),
            "route": route,
            "origin": o,
            "destination": d,
            "origin_name": self._name(o),
            "destination_name": self._name(d),
            "departure_month": month,
            "month_mode": "fixed" if month else "rolling",
            "effective_month": effective_month(month, now),
            "status": status,
            "target_price": int(row["target_price"]),
            "currency": row.get("currency", "TWD"),
            "alerts_paused": paused,
            "plan_name": row.get("plan_name"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "version": self.version_of(row),
            "fare_check": None,  # Phase 2 (contract §3.4)
        }

    # ---- GET /subscriptions ------------------------------------------------------------
    def list(self, email):
        now = self.clock()
        rows = sorted(self.store.list_rows(email),
                      key=lambda r: (r.get("route", "").split("#")[0], r.get("departure_month") or "",
                                     r.get("created_at") or "", r["route"]))
        return 200, {
            "subscriptions": [self.view(r, now) for r in rows],
            "limits": {"max": self.cfg.max_subscriptions, "used": len(rows)},
            "months": {
                "current": current_month(now),
                "selectable": selectable_months(now, self.cfg.first_offset_months, self.cfg.window_months),
            },
        }

    # ---- POST /subscriptions (quota-checked create, §5.2) ------------------------------
    def create(self, email, body):
        now = self.clock()
        o, d, month, target = v.create_body(self.cfg, body, now)
        key = subkeys.sort_key(o, d, month)  # one row per (route, fixed month) — R-MM-1
        dup = {"route": subkeys.pair(o, d), "departure_month": month}
        for attempt in range(MAX_ATTEMPTS):
            self._backoff(attempt)
            quota = self.store.get_quota(email)  # 1. version FIRST
            rows = self.store.list_rows(email)  # 2. then the strongly consistent count
            if any(r["route"] == key for r in rows):
                raise ApiError(409, "duplicate_subscription", dup)
            if len(rows) >= self.cfg.max_subscriptions:
                raise ApiError(409, "limit_reached", {"max": self.cfg.max_subscriptions, "used": len(rows)})
            stamp = self._stamp()
            row = {"route": key, "origin": o, "destination": d, "departure_month": month,
                   "target_price": target, "currency": "TWD", "created_at": stamp, "updated_at": stamp,
                   TOKEN_ATTR: self.new_token()}
            try:
                self.store.create(email, row, quota, len(rows) + 1, stamp)
                return 201, self.view(row, now)
            except TxCancelled as e:
                if e.failed(0):
                    raise ApiError(409, "duplicate_subscription", dup) from None
                # quota version moved or TransactionConflict: re-read and retry
            except WriteConflict:
                raise ApiError(409, "conflict") from None  # outcome unknown: never retry blindly
        raise ApiError(409, "conflict")

    # ---- DELETE /subscriptions/{id} ----------------------------------------------------
    def delete(self, email, sub_id, if_version=None):
        route = v.id_param(sub_id)  # the sort key; always within the caller's own partition
        # The client must say which row version it deletes (contract §4.5): a stale or missing
        # version is never replaced by a freshly read one, so a request aimed at subscription A
        # can never delete a subscription B that later reused A's ID.
        expected = v.if_version(if_version)
        for attempt in range(MAX_ATTEMPTS):
            self._backoff(attempt)
            quota = self.store.get_quota(email)
            rows = self.store.list_rows(email)
            row = next((r for r in rows if r["route"] == route), None)
            if row is None:
                raise ApiError(404, "not_found")
            if self.version_of(row) != expected:
                raise ApiError(409, "conflict")
            stamp = self._stamp()
            try:
                self.store.delete(email, row, quota, len(rows) - 1, stamp)
                return 204, None
            except TxCancelled as e:
                if e.failed(0):
                    if self.store.get_row(email, route) is None:
                        raise ApiError(404, "not_found") from None
                    raise ApiError(409, "conflict") from None
                # quota contention only: re-read; the row must still be exactly the expected version
            except WriteConflict:
                raise ApiError(409, "conflict") from None  # outcome unknown: never retry blindly
        raise ApiError(409, "conflict")

    # ---- PATCH /subscriptions/{id} (never touches the quota, §5.4) --------------------
    def patch(self, email, sub_id, body):
        now = self.clock()
        route = v.id_param(sub_id)  # the sort key
        v.patch_body(body)  # includes the required if_version (428 / 400)
        row = self.store.get_row(email, route)
        if row is None:
            raise ApiError(404, "not_found")
        stored_at = row.get("updated_at")
        if self.version_of(row) != body["if_version"]:
            raise ApiError(409, "conflict")
        cur_o, cur_d, _ = subkeys.parse_key(route)
        o, d = row.get("origin") or cur_o, row.get("destination") or cur_d
        if "origin" in body:
            o = v.origin(self.cfg, body["origin"])
            d = v.destination(self.cfg, body["destination"])
            v.different(o, d)
        stored_month = row.get("departure_month")
        month = v.patched_month(self.cfg, body["departure_month"], stored_month, now) if "departure_month" in body else stored_month
        target = v.target_price(self.cfg, body["target_price"]) if "target_price" in body else int(row["target_price"])
        paused = body["alerts_paused"] if "alerts_paused" in body else bool(row.get("alerts_paused", False))
        new_key = subkeys.sort_key(o, d, month)

        if new_key != route:
            # Route and/or month changed: the sort key changes, so Delete old + Put new in one
            # transaction (record count unchanged). Includes rolling -> fixed and reactivating
            # an expired month.
            return self._change_key(email, row, new_key, o, d, month, target, paused, now)

        if (month, target, paused) == (stored_month, int(row["target_price"]), bool(row.get("alerts_paused", False))):
            return 200, self.view(row, now)  # nothing to change, no write
        stamp = self._stamp(after=stored_at)
        sets = {"target_price": target, "updated_at": stamp, TOKEN_ATTR: self.new_token()}
        removes = []
        if month is not None:
            sets["departure_month"] = month
        if paused:
            sets["alerts_paused"] = True
        else:
            removes.append("alerts_paused")  # stored only when true (§3.1)
        try:
            # Only if the stored row is still exactly the version the client named (guard).
            new = self.store.update(email, row, sets, removes)
        except ConditionFailed:
            if self.store.get_row(email, route) is None:
                raise ApiError(404, "not_found") from None
            raise ApiError(409, "conflict") from None
        except WriteConflict:
            # Another request's transaction is changing this row right now (a create, delete or
            # route change). The edit was based on the state read before it, so it is not
            # retried: the client reloads and decides (contract §4.3).
            raise ApiError(409, "conflict") from None
        return 200, self.view(new, now)

    def _change_key(self, email, row, new_key, o, d, month, target, paused, now):
        dup = {"route": subkeys.pair(o, d), "departure_month": month}
        if self.store.get_row(email, new_key) is not None:
            raise ApiError(409, "duplicate_subscription", dup)
        stamp = self._stamp(after=row.get("updated_at"))
        same_route = subkeys.pair_of_key(row["route"]) == subkeys.pair(o, d)
        new_row = {"route": new_key, "origin": o, "destination": d, "departure_month": month,
                   "target_price": target, "currency": "TWD", "alerts_paused": True if paused else None,
                   # plan_name marks the two M1 plan rows, whose key is the rolling pair: never carried
                   # to another key. A month-only change keeps created_at (same subscription).
                   "previous_route": row["route"],
                   "created_at": row.get("created_at") if same_route and row.get("created_at") else stamp,
                   "updated_at": stamp, TOKEN_ATTR: self.new_token()}
        try:
            self.store.change_route(email, row, new_row)
        except TxCancelled as e:
            if e.failed(1):
                raise ApiError(409, "duplicate_subscription", dup) from None
            if e.failed(0):
                if self.store.get_row(email, row["route"]) is None:
                    raise ApiError(404, "not_found") from None
            raise ApiError(409, "conflict") from None
        except WriteConflict:
            raise ApiError(409, "conflict") from None
        return 200, self.view(new_row, now)

    # ---- POST /subscribe (legacy M1.1, same quota path, §8.2) --------------------------
    def legacy_subscribe(self, email, body):
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_json")
        plan = body.get("plan_name")
        if plan not in LEGACY_PLANS:
            raise ApiError(400, "invalid_plan")
        target = v.legacy_target_price(self.cfg, body.get("target_price"))
        o, d = LEGACY_PLANS[plan]
        route = subkeys.sort_key(o, d, None)  # the legacy rolling row; fixed-month rows are separate
        for attempt in range(MAX_ATTEMPTS):
            self._backoff(attempt)
            quota = self.store.get_quota(email)
            rows = self.store.list_rows(email)
            existing = next((r for r in rows if r["route"] == route), None)
            if existing is not None:
                stamp = self._stamp(after=existing.get("updated_at"))
                try:
                    # Upsert semantics of M1.1: keeps created_at, pause state and departure_month.
                    self.store.update(email, existing, {"plan_name": plan, "origin": o, "destination": d,
                                                        "target_price": target, "currency": "TWD",
                                                        "updated_at": stamp, TOKEN_ATTR: self.new_token()}, [],
                                      # Only on top of the state just read: a concurrent edit
                                      # makes this a ConditionFailed and the loop re-reads, so
                                      # updated_at never moves backwards and nothing unseen is
                                      # overwritten (M1 upsert semantics, internal guard only).
                                      )
                except (ConditionFailed, WriteConflict) as e:
                    if isinstance(e, WriteConflict) and not e.retryable:
                        raise ApiError(409, "conflict") from None
                    # Deleted meanwhile, or a transaction is changing the row right now
                    # (TransactionConflictException, B4 Stage C s10): nothing was written, so
                    # re-read and retry within the bounded attempts (jittered backoff above).
                    continue
                return 200, {"route": route, "plan_name": plan, "target_price": target, "currency": "TWD",
                             "updated_at": stamp}
            if len(rows) >= self.cfg.max_subscriptions:
                raise ApiError(409, "limit_reached", {"max": self.cfg.max_subscriptions, "used": len(rows)})
            stamp = self._stamp()
            row = {"route": route, "origin": o, "destination": d, "plan_name": plan, "target_price": target,
                   "currency": "TWD", "created_at": stamp, "updated_at": stamp,  # rolling: no month
                   TOKEN_ATTR: self.new_token()}
            try:
                self.store.create(email, row, quota, len(rows) + 1, stamp)
                return 200, {"route": route, "plan_name": plan, "target_price": target, "currency": "TWD",
                             "updated_at": stamp}
            except TxCancelled:
                continue  # created meanwhile (→ update next time) or version moved
            except WriteConflict:
                raise ApiError(409, "conflict") from None  # outcome unknown: never retry blindly
        raise ApiError(409, "conflict")
