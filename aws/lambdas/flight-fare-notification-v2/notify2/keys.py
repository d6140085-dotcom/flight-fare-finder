"""Deduplication keys. One alert stream per (subscriber, origin, destination, effective month).

- subscriber_id: HMAC-SHA256 of the normalised address with a secret pepper (SSM). Stable for
  every existing M1 and Phase 1B row (they are all keyed by email), not reversible without the
  pepper, and never the address itself. There is no user-id attribute in any subscription row
  today; see the design doc for why a Supabase user id isn't used.
- stream pk:  "v2#{subscriber_id}#{ORIGIN}-{DEST}#{YYYY-MM}"
- state item: (pk, sent_at="STATE")  - one item per stream, the target of every conditional write
- log items:  (pk, sent_at="<ISO-8601 UTC, ms>") - append-only history of v2 sends
- M1 rows:    (pk="{email}#{route}", sent_at=ISO) - read for compatibility, optionally dual-written
"""
import hashlib
import hmac
import json
import re

from . import months

STATE_SK = "STATE"
ROUTE_RE = re.compile(r"^[A-Z]{3}-[A-Z]{3}$")
PEPPER_MIN_LEN = 32


class InvalidMessage(ValueError):
    """The SQS message can never be processed (dropped, not retried)."""


def normalise_email(email):
    return email.strip().lower()


def subscriber_id(email, pepper):
    if not isinstance(pepper, (bytes, str)) or len(pepper) < PEPPER_MIN_LEN:
        raise ValueError("dedup pepper missing or shorter than 32 characters")
    key = pepper.encode() if isinstance(pepper, str) else pepper
    return "s1_" + hmac.new(key, normalise_email(email).encode(), hashlib.sha256).hexdigest()[:32]


def resolve_month(message):
    """(effective month, source). Prefer the scheduler's resolved month (v2 messages); M1-shape
    messages fall back to the fare's departure month, which Phase 2A validation guarantees equals
    the work item's month."""
    m = message.get("month")
    if m is not None:
        if not months.valid_month(m):
            raise InvalidMessage("invalid month")
        return m, "message"
    dep = (message.get("cheapest") or {}).get("depart_date")
    if isinstance(dep, str) and months.valid_month(dep[:7]):
        return dep[:7], "depart_date"
    raise InvalidMessage("no effective month (no month field and no departure date)")


def stream_pk(sid, route, month):
    if not ROUTE_RE.match(route or "") or not months.valid_month(month):
        raise InvalidMessage("invalid route or month for a stream key")
    return f"v2#{sid}#{route}#{month}"


def legacy_pk(email, route):
    """M1's key, used only to read/write M1-compatible rows (never as the v2 dedup key)."""
    return f"{email}#{route}"


def event_id(pk, message, delivery_id):
    """Identifies one fare event in one stream. With the scheduler's `as_of` (v2 messages) a
    parser re-run of the same schedule maps to the same id; otherwise the SQS message id makes a
    redelivered message idempotent."""
    f = message.get("cheapest") or {}
    basis = {"pk": pk, "price": str(f.get("price")), "depart": f.get("depart_date"), "return": f.get("return_date"),
             "airline": f.get("airline"), "as_of": message.get("as_of") or None,
             "delivery": None if message.get("as_of") else delivery_id}
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()[:40]


def idempotency_key(eid):
    """Resend Idempotency-Key (1-256 characters, remembered for 24 hours)."""
    return f"flight-fare/{eid}"
