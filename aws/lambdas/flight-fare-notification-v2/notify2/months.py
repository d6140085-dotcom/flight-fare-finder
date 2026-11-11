"""Departure-month rules, identical to farecheck/months.py and subsapi/months.py (API contract
§3.2, §3.3). Taiwan has had no daylight saving time since 1979, so Asia/Taipei is a fixed UTC+8.
A parity test keeps the three copies in step.
"""
import re
from datetime import timedelta, timezone

TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def valid_month(value):
    return isinstance(value, str) and bool(MONTH_RE.match(value))


def current_month(now):
    d = now.astimezone(TAIPEI)
    return f"{d.year:04d}-{d.month:02d}"


def today(now):
    return now.astimezone(TAIPEI).date()


def add_months(month, n):
    total = int(month[:4]) * 12 + int(month[5:7]) - 1 + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def effective_month(departure_month, now):
    """Fixed rows use their month; legacy rolling rows (no month) use next month."""
    return departure_month or add_months(current_month(now), 1)


def is_expired(departure_month, now):
    return departure_month is not None and departure_month < current_month(now)
