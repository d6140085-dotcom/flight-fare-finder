"""Subscription identity (R-MM-1, ADR 0001 D1 as amended 2026-11-12).

One subscription = one item in `subscriptions` (PK `email`, SK `route`). The sort key is:

    legacy rolling row (M1, no departure_month):  "KHH-TYO"
    fixed-month row:                              "KHH-TYO#2027-01"

The public, URL-safe subscription ID used in API paths is the sort key with "#" replaced by "_":

    "KHH-TYO"          <->  "KHH-TYO"
    "KHH-TYO_2027-01"  <->  "KHH-TYO#2027-01"

Both forms are pure functions of (origin, destination, departure_month); no extra state, no
lookup. Authorisation never depends on the ID: every read and write uses the session's email
as the partition key, so an ID can only ever address the caller's own rows.

This file is copied verbatim into each Lambda package that needs it (subscriptions API, fare
check, notification v2); a test keeps the copies identical.
"""
import re

_CODE = r"[A-Z]{3}"
_MONTH = r"\d{4}-(?:0[1-9]|1[0-2])"
SORT_KEY_RE = re.compile(rf"^({_CODE})-({_CODE})(?:#({_MONTH}))?$")
PUBLIC_ID_RE = re.compile(rf"^({_CODE})-({_CODE})(?:_({_MONTH}))?$")
PAIR_RE = re.compile(rf"^{_CODE}-{_CODE}$")


def pair(origin, destination):
    return f"{origin}-{destination}"


def sort_key(origin, destination, departure_month):
    """DynamoDB sort key for a subscription; None month = legacy rolling row."""
    base = pair(origin, destination)
    return base if departure_month is None else f"{base}#{departure_month}"


def public_id(key):
    """URL-safe public ID for a sort key (no reserved characters, never percent-encoded)."""
    return key.replace("#", "_")


def key_from_id(value):
    """Sort key for a public ID, or None if the ID is malformed."""
    if not isinstance(value, str) or not PUBLIC_ID_RE.match(value):
        return None
    return value.replace("_", "#")


def parse_key(key):
    """(origin, destination, month or None) from a sort key, or None if malformed."""
    m = SORT_KEY_RE.match(key) if isinstance(key, str) else None
    return (m.group(1), m.group(2), m.group(3)) if m else None


def pair_of_key(key):
    """"KHH-TYO" for "KHH-TYO" and "KHH-TYO#2027-01"; None if malformed."""
    parts = parse_key(key)
    return pair(parts[0], parts[1]) if parts else None
