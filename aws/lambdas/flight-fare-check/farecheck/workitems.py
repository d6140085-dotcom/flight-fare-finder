"""Turn subscription rows into deduplicated fare lookups: one per (origin, destination,
effective month) with at least one active row. Rows are read without `email`.

All month rules use one `as_of` instant per scheduler run (Asia/Taipei calendar). The wrapper
resolves it once and passes it downstream, so a run that crosses midnight at the end of a month
never mixes two months (no skipped or duplicated month).
"""
import math
from collections import Counter
from dataclasses import dataclass
from datetime import timezone

from . import months

ACTIVE, PAUSED, EXPIRED, INVALID = "active", "paused", "expired", "invalid"


@dataclass(frozen=True, order=True)
class WorkItem:
    origin: str
    destination: str
    month: str

    @property
    def route(self):
        return f"{self.origin}-{self.destination}"

    @property
    def key(self):
        """Fare-check key (API contract §3.4)."""
        return f"{self.route}#{self.month}"

    def payload(self, as_of):
        """Live parser payload: the resolved month plus the scheduler's `as_of` instant."""
        return {"origin": self.origin, "destination": self.destination, "route": self.route,
                "month": self.month, "as_of": iso_utc(as_of)}


def iso_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def route_codes(row):
    """(origin, destination) from the row, falling back to splitting `route` (M1 rows)."""
    route = row.get("route")
    if not isinstance(route, str) or route.count("-") != 1:
        return None
    o, d = route.split("-")
    o, d = row.get("origin") or o, row.get("destination") or d
    return (o, d) if f"{o}-{d}" == route else None


def classify(row, now, catalog):
    """(state, WorkItem or None). Expiry is checked before pause, as in contract §3.3."""
    codes = route_codes(row)
    if not codes or not catalog.known_route(*codes):
        return INVALID, None
    dep = row.get("departure_month")
    if dep is not None and not months.valid_month(dep):
        return INVALID, None
    if months.is_expired(dep, now):
        return EXPIRED, None
    if row.get("alerts_paused") is True:
        return PAUSED, None
    return ACTIVE, WorkItem(codes[0], codes[1], months.effective_month(dep, now))


@dataclass(frozen=True)
class Plan:
    items: tuple
    counts: dict


def select_window(items, cap, as_of, period_minutes):
    """Bounded, deterministic rotation: never the same items dropped on every run.

    With n sorted items and a cap c < n, the items form w = ceil(n / c) consecutive windows. The
    run number (as_of divided into schedule periods) picks window run % w, so w consecutive
    scheduled runs check every item exactly once while the set is unchanged. Stateless: no
    table, no new AWS resource. Assumes SCHEDULE_PERIOD_MINUTES matches the schedule.
    """
    n = len(items)
    if n <= cap:
        return items, {"windows": 1, "window": 0, "selected": n, "total": n}
    windows = math.ceil(n / cap)
    run = int(as_of.timestamp() // (period_minutes * 60))
    w = run % windows
    chosen = items[w * cap:(w + 1) * cap]
    return chosen, {"windows": windows, "window": w, "selected": len(chosen), "total": n}


def build(rows, now, catalog):
    counts, items = Counter(), set()
    for row in rows:
        state, item = classify(row, now, catalog)
        counts[state] += 1
        if item:
            items.add(item)
    counts["work_items"] = len(items)
    return Plan(tuple(sorted(items)), dict(counts))
