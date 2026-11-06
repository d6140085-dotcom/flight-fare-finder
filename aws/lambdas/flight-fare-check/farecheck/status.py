"""Fare-check status records (API contract §3.4).

One record per work item per run: what the subscription page shows as 最近查到 / 沒有近期票價資料
/ 查詢失敗, plus the itinerary needed later for search links (search-links spec §3.1). Phase 2A
only builds and logs them; storing them needs the proposed `fare_check` table, which is not
created (separate approval).
"""
from datetime import timedelta, timezone

RECORD_TTL = timedelta(days=3)


def utc_iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(item, result, checked_at):
    """Storage shape for the proposed table (key + non-key attributes; never an email)."""
    rec = {
        "key": item.key, "origin": item.origin, "destination": item.destination, "month": item.month,
        "kind": result.kind, "source": result.source, "checked_at": utc_iso(checked_at),
        "expires_at": int((checked_at + RECORD_TTL).timestamp()),
        "offers_seen": result.offers_seen, "rejected": dict(sorted(result.rejected.items())),
    }
    if result.reason:
        rec["reason"] = result.reason
    o = result.offer
    if o:
        rec.update({"price": o.price, "currency": o.currency, "depart_date": o.depart_date,
                    "return_date": o.return_date, "depart_at": o.depart_at, "return_at": o.return_at,
                    "origin_airport": o.origin_airport, "destination_airport": o.destination_airport,
                    "airline": o.airline})
    return rec


def api_view(rec):
    """`fare_check` as the subscriptions API will return it (contract §3.4)."""
    if rec is None:
        return None
    out = {"kind": rec["kind"], "checked_at": rec["checked_at"]}
    if rec["kind"] == "ok":
        out.update({"cheapest_twd": rec["price"], "currency": rec["currency"],
                    "depart_date": rec["depart_date"], "return_date": rec["return_date"],
                    "origin_airport": rec.get("origin_airport"),
                    "destination_airport": rec.get("destination_airport")})
    return out


class NullSink:
    """Phase 2A: no table exists, so records are only returned and logged."""
    def put(self, rec):
        return None
