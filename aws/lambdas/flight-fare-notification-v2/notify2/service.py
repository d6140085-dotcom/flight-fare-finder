"""One SQS message -> at most one email per fare event and stream, M1 rules within a stream.

Outcomes (logged with a subscriber hash, never an address):
  skipped:<reason>  nothing sent, message done (deduped, duplicate, paused, expired, ...)
  sent / sent:duplicate_key / sent:claim_lost
  dropped:<reason>  permanent problem (invalid message, Resend 4xx), message done
  Retry exception   message returned to SQS (claim held by another worker, Resend 429/5xx/409
                    concurrent, network), as M1's partial-batch retry
"""
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from . import history, keys, mailer, months, rules
from .history import ClaimConflict


class Retry(Exception):
    """Return the message to SQS; it is safe to process again."""


@dataclass
class Deps:
    settings: object
    history: history.History
    pepper: str
    resend_cfg: object            # callable -> {"key", "from"} (lazy, like M1)
    subscriptions: object = None  # boto3 Table for the re-check (GetItem only), or None
    opener: object = None         # urllib opener for Resend (tests inject a fake)
    cities: dict = None
    airlines: dict = None
    log: object = print


def tag(email, route):
    return f"{route} sub:{hashlib.sha256(email.encode()).hexdigest()[:8]}"


def validate(m):
    if not isinstance(m, dict):
        raise keys.InvalidMessage("not an object")
    email, route, f = m.get("email"), m.get("route"), m.get("cheapest")
    if not isinstance(email, str) or "@" not in email:
        raise keys.InvalidMessage("no recipient")
    if not isinstance(route, str) or not keys.ROUTE_RE.match(route):
        raise keys.InvalidMessage("invalid route")
    if not isinstance(f, dict):
        raise keys.InvalidMessage("no fare")
    try:
        price = Decimal(str(f["price"]))
    except (KeyError, InvalidOperation):
        raise keys.InvalidMessage("invalid price") from None
    if not price.is_finite() or price <= 0:
        raise keys.InvalidMessage("invalid price")
    return email, route, price


def as_of_of(m, now):
    raw = m.get("as_of")
    if raw is None:
        return now
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise keys.InvalidMessage("invalid as_of") from None


def recheck(deps, m, email, route, month, price, as_of):
    """Re-read the subscription just before sending (it may have been paused, edited, expired or
    deleted since the fare check). Returns None if still eligible, else the skip reason."""
    if deps.subscriptions is None:
        return None
    row = deps.subscriptions.get_item(Key={"email": email, "route": route}, ConsistentRead=True).get("Item")
    if row is None:
        return "subscription_gone"
    dep = row.get("departure_month")
    if months.is_expired(dep, as_of):
        return "expired"
    if row.get("alerts_paused") is True:
        return "paused"
    if months.effective_month(dep, as_of) != month:
        return "month_changed"
    try:
        target = Decimal(str(row.get("target_price")))
    except InvalidOperation:
        return "no_target"
    if not target.is_finite() or target < price:
        return "target_changed"
    return None


def previous_send(deps, state, email, route, month, now):
    """Latest earlier alert for this stream: the v2 state, and (compatibility) the newest M1 row
    if that M1 alert was about this month."""
    v2 = {"sent_at": state["last_sent_at"], "price": state["last_price"]} if state and state.get("last_sent_at") else None
    legacy = None
    if deps.settings.legacy_read:
        row = deps.history.legacy_last(email, route, now - timedelta(hours=float(deps.settings.floor_hours)))
        if row and month in history.legacy_months(row["sent_at"]):
            legacy = {"sent_at": row["sent_at"], "price": row["price"]}
    return rules.latest(v2, legacy)


def process(m, delivery_id, now, deps):
    email, route, price = validate(m)
    month, month_source = keys.resolve_month(m)
    as_of = as_of_of(m, now)
    sid = keys.subscriber_id(email, deps.pepper)
    pk = keys.stream_pk(sid, route, month)
    eid = keys.event_id(pk, m, delivery_id)
    t = f"{tag(email, route)} {month}"

    state = deps.history.state(pk)
    if state and state.get("last_event_id") == eid:
        deps.log(f"{t}: skipped (duplicate event)")
        return "skipped:duplicate_event"
    if state and state.get("claim_event") and int(state.get("claim_expires", 0)) >= int(now.timestamp()):
        raise Retry(f"{t}: stream busy (claimed by another worker)")
    if not rules.should_send(previous_send(deps, state, email, route, month, now), price, now, deps.settings):
        deps.log(f"{t}: skipped (deduped)")
        return "skipped:deduped"
    try:
        deps.history.claim(pk, state, eid, now, deps.settings.claim_seconds)
    except ClaimConflict:
        raise Retry(f"{t}: stream changed or claimed concurrently") from None
    try:
        reason = recheck(deps, m, email, route, month, price, as_of) if deps.settings.recheck_subscription else None
        if reason:
            deps.history.release(pk, eid)
            deps.log(f"{t}: skipped ({reason})")
            return f"skipped:{reason}"
        subject, page, text = mailer.render(m, deps.cities or {}, deps.airlines or {})
        kw = {"opener": deps.opener} if deps.opener else {}
        status, outcome, err = mailer.send(deps.resend_cfg(), email, subject, page, text, keys.idempotency_key(eid), **kw)
    except BaseException:
        deps.history.release(pk, eid)
        raise
    if outcome in (mailer.SENT, mailer.DUPLICATE):
        result = deps.history.commit(pk=pk, eid=eid, now=now, price=price, route=route, month=month, sid=sid,
                                     email=email, dual_write=deps.settings.legacy_dual_write)
        label = "sent" + (":duplicate_key" if outcome == mailer.DUPLICATE else "") + (":claim_lost" if result == "claim_lost" else "")
        deps.log(f"{t}: {label} {price} (month from {month_source})")
        return label
    deps.history.release(pk, eid)
    if outcome == mailer.RETRY:
        raise Retry(f"{t}: Resend {status} {err}")
    deps.log(f"{t}: dropped, Resend {status} {err}")
    return "dropped:resend"
