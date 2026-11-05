"""Departure months (contract §3.2). Taiwan has had no daylight saving time since 1979, so the
Asia/Taipei calendar is UTC+8; a fixed offset avoids depending on tzdata in the runtime."""
import re
from datetime import timedelta, timezone

TAIPEI = timezone(timedelta(hours=8), "Asia/Taipei")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def current_month(now):
    d = now.astimezone(TAIPEI)
    return f"{d.year:04d}-{d.month:02d}"


def add_months(month, n):
    total = int(month[:4]) * 12 + int(month[5:7]) - 1 + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def selectable_months(now, first_offset=1, window=12):
    start = add_months(current_month(now), first_offset)
    return [add_months(start, i) for i in range(window)]


def effective_month(departure_month, now):
    """Fixed rows use their month; legacy rolling rows (no month) use next month."""
    return departure_month or add_months(current_month(now), 1)


def is_expired(departure_month, now):
    return departure_month is not None and departure_month < current_month(now)


def label(month):
    return f"{int(month[:4])} 年 {int(month[5:7])} 月"
