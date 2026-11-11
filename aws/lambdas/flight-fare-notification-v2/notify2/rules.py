"""The M1 re-alert rule, unchanged: within one stream, send if there was no previous alert, the
last alert is at least NOTIFY_FLOOR_HOURS old, or the fare dropped at least REALERT_PCT % or
REALERT_ABS_TWD since the last alert. (M1: should_send in flight-fare-notification/index.py;
a parity test compares both on many cases.)
"""
from datetime import datetime, timedelta
from decimal import Decimal


def should_send(last, price, now, settings):
    """`last` is None or {"sent_at": ISO string, "price": number}."""
    if not last:
        return True
    prev = Decimal(str(last["price"]))
    return (now - datetime.fromisoformat(last["sent_at"]) >= timedelta(hours=float(settings.floor_hours))
            or price <= prev * (1 - settings.realert_pct / 100)
            or prev - price >= settings.realert_abs_twd)


def latest(*candidates):
    """Most recent of several `last` records (None ignored)."""
    present = [c for c in candidates if c]
    return max(present, key=lambda c: datetime.fromisoformat(c["sent_at"])) if present else None
