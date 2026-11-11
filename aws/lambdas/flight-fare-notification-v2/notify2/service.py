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

from . import history, keys, mailer, months, rules, searchlinks
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
    links_config: dict = None     # validated search-platforms.json, or None: no links section
    catalog: object = None        # searchlinks.Catalog, or None: no links section
    marker: str = None            # Aviasales marker (TP_MARKER, deploy time); None: plain Aviasales link only


def search_links(m, now, deps, t):
    """The fare's search links, or None. Never raises: the email doesn't depend on links (spec §6)."""
    if deps.links_config is None or deps.catalog is None:
        return None
    o, d = m["route"].split("-")
    f = m["cheapest"]
    try:
        links = searchlinks.build(o, d, f.get("depart_date"), f.get("return_date"), months.today(now),
                                  catalog=deps.catalog, config=deps.links_config, marker=deps.marker)
    except Exception as e:              # noqa: BLE001 - spec §6: send without the links section
        deps.log(f"{t}: links: error {type(e).__name__}")
        return None
    if links.reasons:
        deps.log(f"{t}: links: {','.join(links.reasons)}")
    return links


def render(m, now, deps, t):
    if deps.settings.email_template == "m1":
        return mailer.render(m, deps.cities or {}, deps.airlines or {})
    return mailer.render_reference(m, deps.cities or {}, deps.airlines or {}, search_links(m, now, deps, t))


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


def previous_send(deps, state, email, route, month, now, price):
    """Latest earlier alert for this stream: the v2 state and, for compatibility, the newest
    genuine M1 alert for this user and route in the last NOTIFY_FLOOR_HOURS.

    M1 rows record no departure month, so an M1 alert is treated as covering EVERY month of the
    route. During the one-time switch-over this can delay another month's first v2 alert by up to
    the floor (24 h), but it can never resend what M1 already sent. A row without a usable price
    keeps only the time floor (no price-drop re-alert relative to an unknown price)."""
    v2 = {"sent_at": state["last_sent_at"], "price": state["last_price"]} if state and state.get("last_sent_at") else None
    legacy = None
    if deps.settings.legacy_read:
        row = deps.history.legacy_last(email, route, now - timedelta(hours=float(deps.settings.floor_hours)))
        if row:
            legacy = {"sent_at": row["sent_at"], "price": row["price"] if row["price"] is not None else price}
    return rules.latest(v2, legacy)


def message_age_basis(m, enqueued_at):
    """When the fare event was created: the scheduler's as_of, else the SQS SentTimestamp."""
    if m.get("as_of"):
        return as_of_of(m, None)
    return enqueued_at


def process(m, delivery_id, now, deps, enqueued_at=None):
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
    ours = bool(state and state.get("claim_event") == eid)          # an earlier attempt of this event
    basis = message_age_basis(m, enqueued_at)
    if basis and now - basis > timedelta(hours=float(deps.settings.max_message_age_hours)):
        if ours:
            # The earlier attempt may have sent, and Resend's key may be about to expire: don't send
            # again, record it as a (possible) send so the M1 rule suppresses repeats.
            deps.history.commit(pk=pk, eid=eid, now=now, price=price, route=route, month=month, sid=sid,
                                email=email, dual_write=deps.settings.legacy_dual_write)
            deps.log(f"{t}: recorded unconfirmed earlier attempt (message too old to resend)")
            return "recorded:unconfirmed"
        deps.log(f"{t}: skipped (stale fare event)")
        return "skipped:stale"
    if state and state.get("claim_event") and not ours and int(state.get("claim_expires", 0)) >= int(now.timestamp()):
        raise Retry(f"{t}: stream busy (claimed by another worker)")
    if not ours and not rules.should_send(previous_send(deps, state, email, route, month, now, price), price, now,
                                          deps.settings):
        deps.log(f"{t}: skipped (deduped)")
        return "skipped:deduped"
    try:
        deps.history.claim(pk, state, eid, now, deps.settings.claim_seconds)
    except ClaimConflict:
        raise Retry(f"{t}: stream changed or claimed concurrently") from None
    try:
        reason = recheck(deps, m, email, route, month, price, as_of) if deps.settings.recheck_subscription else None
        if reason and not ours:
            deps.history.release(pk, eid)
            deps.log(f"{t}: skipped ({reason})")
            return f"skipped:{reason}"
        if reason and ours:
            # the earlier attempt may already have sent; record conservatively instead of dropping it
            deps.history.commit(pk=pk, eid=eid, now=now, price=price, route=route, month=month, sid=sid,
                                email=email, dual_write=deps.settings.legacy_dual_write)
            deps.log(f"{t}: recorded unconfirmed earlier attempt ({reason})")
            return "recorded:unconfirmed"
        subject, page, text = render(m, now, deps, t)
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
    if outcome == mailer.UNKNOWN:
        # The email may have gone out. Keep the claim: other events wait until it expires, and the
        # redelivery of this message re-enters it and retries with the same Idempotency-Key.
        raise Retry(f"{t}: Resend outcome unknown ({status} {err}); claim kept")
    deps.history.release(pk, eid)
    if outcome == mailer.RETRY:
        raise Retry(f"{t}: Resend {status} {err}")
    deps.log(f"{t}: dropped, Resend {status} {err}")
    return "dropped:resend"
