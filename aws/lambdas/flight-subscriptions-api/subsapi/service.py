"""Subscription operations (contract §4, §5, §8.2). Every method returns (status, body) or
raises ApiError. The clock, sleep and randomness are injectable for tests."""
import random
import time
from datetime import datetime, timedelta, timezone

from . import validation as v
from .errors import ApiError
from .months import current_month, effective_month, is_expired, selectable_months
from .store import ConditionFailed, TxCancelled, WriteConflict

MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 0.025
BACKOFF_CAP_S = 0.2
LEGACY_PLANS = {"tokyo": ("TPE", "TYO"), "seoul": ("TPE", "SEL")}


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class Service:
    def __init__(self, store, cfg, clock=None, sleep=time.sleep, rng=None):
        self.store = store
        self.cfg = cfg
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep
        self.rng = rng or random.Random()

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
        """Normalise a stored row (M1 or M1.4) to the API shape (§3.1, §8.1)."""
        route = row["route"]
        o, _, d = route.partition("-")
        o, d = row.get("origin") or o, row.get("destination") or d
        month = row.get("departure_month")
        paused = bool(row.get("alerts_paused", False))
        status = "expired" if is_expired(month, now) else ("paused" if paused else "active")
        return {
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
            "fare_check": None,  # Phase 2 (contract §3.4)
        }

    # ---- GET /subscriptions ------------------------------------------------------------
    def list(self, email):
        now = self.clock()
        rows = sorted(self.store.list_rows(email), key=lambda r: (r.get("created_at") or "", r["route"]))
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
        route = f"{o}-{d}"
        for attempt in range(MAX_ATTEMPTS):
            self._backoff(attempt)
            quota = self.store.get_quota(email)  # 1. version FIRST
            rows = self.store.list_rows(email)  # 2. then the strongly consistent count
            if any(r["route"] == route for r in rows):
                raise ApiError(409, "duplicate_subscription", {"route": route})
            if len(rows) >= self.cfg.max_subscriptions:
                raise ApiError(409, "limit_reached", {"max": self.cfg.max_subscriptions, "used": len(rows)})
            stamp = self._stamp()
            row = {"route": route, "origin": o, "destination": d, "departure_month": month,
                   "target_price": target, "currency": "TWD", "created_at": stamp, "updated_at": stamp}
            try:
                self.store.create(email, row, quota, len(rows) + 1, stamp)
                return 201, self.view(row, now)
            except TxCancelled as e:
                if e.failed(0):
                    raise ApiError(409, "duplicate_subscription", {"route": route}) from None
                # quota version moved or TransactionConflict: re-read and retry
            except WriteConflict:
                raise ApiError(409, "conflict") from None  # outcome unknown: never retry blindly
        raise ApiError(409, "conflict")

    # ---- DELETE /subscriptions/{route} -------------------------------------------------
    def delete(self, email, route, if_updated_at=None):
        v.route_param(route)
        for attempt in range(MAX_ATTEMPTS):
            self._backoff(attempt)
            quota = self.store.get_quota(email)
            rows = self.store.list_rows(email)
            row = next((r for r in rows if r["route"] == route), None)
            if row is None:
                raise ApiError(404, "not_found")
            if if_updated_at is not None and row.get("updated_at") != if_updated_at:
                raise ApiError(409, "conflict")
            stamp = self._stamp()
            try:
                self.store.delete(email, route, quota, len(rows) - 1, stamp, if_updated_at)
                return 204, None
            except TxCancelled as e:
                if e.failed(0):
                    if self.store.get_row(email, route) is None:
                        raise ApiError(404, "not_found") from None
                    raise ApiError(409, "conflict") from None
            except WriteConflict:
                raise ApiError(409, "conflict") from None  # outcome unknown: never retry blindly
        raise ApiError(409, "conflict")

    # ---- PATCH /subscriptions/{route} (never touches the quota, §5.4) -----------------
    def patch(self, email, route, body):
        now = self.clock()
        v.route_param(route)
        v.patch_body(body)
        row = self.store.get_row(email, route)
        if row is None:
            raise ApiError(404, "not_found")
        stored_at = row.get("updated_at")
        if "if_updated_at" in body and body["if_updated_at"] != stored_at:
            raise ApiError(409, "conflict")
        cur_o, _, cur_d = route.partition("-")
        o, d = row.get("origin") or cur_o, row.get("destination") or cur_d
        if "origin" in body:
            o = v.origin(self.cfg, body["origin"])
            d = v.destination(self.cfg, body["destination"])
            v.different(o, d)
        stored_month = row.get("departure_month")
        month = v.patched_month(self.cfg, body["departure_month"], stored_month, now) if "departure_month" in body else stored_month
        target = v.target_price(self.cfg, body["target_price"]) if "target_price" in body else int(row["target_price"])
        paused = body["alerts_paused"] if "alerts_paused" in body else bool(row.get("alerts_paused", False))
        new_route = f"{o}-{d}"

        if new_route != route:
            return self._change_route(email, row, new_route, o, d, month, target, paused, now)

        if (month, target, paused) == (stored_month, int(row["target_price"]), bool(row.get("alerts_paused", False))):
            return 200, self.view(row, now)  # nothing to change, no write
        stamp = self._stamp(after=stored_at)
        sets = {"target_price": target, "updated_at": stamp}
        removes = []
        if month is not None:
            sets["departure_month"] = month
        if paused:
            sets["alerts_paused"] = True
        else:
            removes.append("alerts_paused")  # stored only when true (§3.1)
        try:
            # The read value of updated_at is the optimistic lock, even without if_updated_at.
            new = self.store.update(email, route, sets, removes, expected_updated_at=stored_at)
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

    def _change_route(self, email, row, new_route, o, d, month, target, paused, now):
        if self.store.get_row(email, new_route) is not None:
            raise ApiError(409, "duplicate_subscription", {"route": new_route})
        stamp = self._stamp(after=row.get("updated_at"))
        new_row = {"route": new_route, "origin": o, "destination": d, "departure_month": month,
                   "target_price": target, "currency": "TWD", "alerts_paused": True if paused else None,
                   "previous_route": row["route"], "created_at": stamp, "updated_at": stamp}
        try:
            self.store.change_route(email, row["route"], row.get("updated_at"), new_row)
        except TxCancelled as e:
            if e.failed(1):
                raise ApiError(409, "duplicate_subscription", {"route": new_route}) from None
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
        route = f"{o}-{d}"
        for attempt in range(MAX_ATTEMPTS):
            self._backoff(attempt)
            quota = self.store.get_quota(email)
            rows = self.store.list_rows(email)
            existing = next((r for r in rows if r["route"] == route), None)
            if existing is not None:
                stamp = self._stamp(after=existing.get("updated_at"))
                try:
                    # Upsert semantics of M1.1: keeps created_at, pause state and departure_month.
                    self.store.update(email, route, {"plan_name": plan, "origin": o, "destination": d,
                                                     "target_price": target, "currency": "TWD",
                                                     "updated_at": stamp}, [])
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
                   "currency": "TWD", "created_at": stamp, "updated_at": stamp}  # rolling: no month
            try:
                self.store.create(email, row, quota, len(rows) + 1, stamp)
                return 200, {"route": route, "plan_name": plan, "target_price": target, "currency": "TWD",
                             "updated_at": stamp}
            except TxCancelled:
                continue  # created meanwhile (→ update next time) or version moved
            except WriteConflict:
                raise ApiError(409, "conflict") from None  # outcome unknown: never retry blindly
        raise ApiError(409, "conflict")
