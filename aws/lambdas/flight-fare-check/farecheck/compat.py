"""v1 vs v3 side-by-side comparison for release blocker B5 (plan: "Travelpayouts v3 migration").

compare() is pure: the same function scores the offline fixture pairs in the tests and, after a
separately approved live DRY_RUN, the logged results of both APIs for the same work item.
v3 may replace v1 only if, over several schedules, it returns a valid round-trip fare at least
as often as v1 and the cheapest prices agree within the agreed tolerance.
"""
from .travelpayouts import OK

DEFAULT_TOLERANCE_PCT = 10.0


def compare(v1, v3, tolerance_pct=DEFAULT_TOLERANCE_PCT):
    out = {"v1_kind": v1.kind, "v3_kind": v3.kind, "both_ok": v1.kind == OK and v3.kind == OK,
           "v3_at_least_as_available": v3.kind == OK or v1.kind != OK}
    if out["both_ok"]:
        a, b = v1.offer, v3.offer
        diff = abs(a.price - b.price) / a.price * 100
        out.update({"v1_price": a.price, "v3_price": b.price, "price_diff_pct": round(diff, 2),
                    "within_tolerance": diff <= tolerance_pct,
                    "same_currency": a.currency == b.currency == "TWD",
                    "both_round_trip": bool(a.return_at and b.return_at),
                    "same_month": a.depart_date[:7] == b.depart_date[:7],
                    "v3_origin_airport": b.origin_airport, "v3_destination_airport": b.destination_airport})
    return out


def summarize(comparisons, tolerance_pct=DEFAULT_TOLERANCE_PCT):
    """Aggregate over many work items/schedules -> the B5 switch decision inputs."""
    n = len(comparisons)
    v1_ok = sum(c["v1_kind"] == OK for c in comparisons)
    v3_ok = sum(c["v3_kind"] == OK for c in comparisons)
    both = [c for c in comparisons if c["both_ok"]]
    within = sum(c["within_tolerance"] for c in both)
    airports = {}
    for c in both:
        key = c["v3_origin_airport"] or "unknown"
        airports[key] = airports.get(key, 0) + 1
    return {"items": n, "v1_ok": v1_ok, "v3_ok": v3_ok, "both_ok": len(both), "within_tolerance": within,
            "tolerance_pct": tolerance_pct, "v3_origin_airports": airports,
            "switch_ok": n > 0 and v3_ok >= v1_ok and len(both) > 0 and within == len(both)}
