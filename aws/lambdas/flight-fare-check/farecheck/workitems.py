"""Turn subscription rows into deduplicated fare lookups: one per (origin, destination,
effective month) with at least one active row. Rows are read without `email`."""
from collections import Counter
from dataclasses import dataclass

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

    def payload(self, dry_run=False):
        p = {"origin": self.origin, "destination": self.destination, "route": self.route, "month": self.month}
        if dry_run:
            p["dry_run"] = True
        return p


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


def build(rows, now, catalog):
    counts, items = Counter(), set()
    for row in rows:
        state, item = classify(row, now, catalog)
        counts[state] += 1
        if item:
            items.add(item)
    counts["work_items"] = len(items)
    return Plan(tuple(sorted(items)), dict(counts))
