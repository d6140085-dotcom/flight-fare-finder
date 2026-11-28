"""One SQS message -> at most one email per fare event and stream, M1 rules within a stream.

Outcomes (logged with a subscriber hash, never an address):
  skipped:<reason>  nothing sent, message done (deduped, duplicate, paused, expired, ...)
  sent / sent:duplicate_key / sent:claim_lost
  blocked:<reason>  reference-fare email without a valid Aviasales affiliate link (missing or
                    invalid TP_MARKER, bad link config), or the legacy M1 template selected
                    (EMAIL_TEMPLATE=m1), or AFFILIATE_LINK_MODE=disabled, or in partner_api
                    mode a missing/invalid aviasales_partner_url: nothing sent, nothing
                    recorded, message done
  dropped:<reason>  permanent problem (invalid message, Resend 4xx), message done
  Retry exception   message returned to SQS (claim held by another worker, Resend 429/5xx/409
                    concurrent, network), as M1's partial-batch retry
"""
import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from . import history, keys, mailer, months, partnerlinks, rules, searchlinks, subkeys
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
    partner_trs: str = None       # TP_TRS: Travelpayouts project id, for formats with a `trs` rule (never logged)
    partner_formats: tuple = partnerlinks.APPROVED_FORMATS   # approved partner-link formats (empty: fail closed)


def search_links(m, now, deps, partner_url=None):
    """The fare's search links. Any problem becomes AffiliateLinkUnavailable with a data-free
    reason: the reference-fare email is then not sent (Travelpayouts condition 3).

    AFFILIATE_LINK_MODE (B6-3A): `disabled` blocks; `direct` is unchanged; `partner_api` replaces
    the Aviasales link with the message's aviasales_partner_url (or `partner_url`, the link pinned
    to this fare event) after strict validation, and never falls back to the direct link."""
    mode = deps.settings.affiliate_link_mode
    if mode == "disabled":
        raise mailer.AffiliateLinkUnavailable("affiliate_links_disabled")
    if deps.links_config is None or deps.catalog is None:
        raise mailer.AffiliateLinkUnavailable("links_config_unavailable")
    o, d = m["route"].split("-")
    f = m["cheapest"]
    try:
        # Links are a function of the message: the scheduler's as_of fixes "today", so a retry on
        # another day renders the same links (same payload under the same Idempotency-Key).
        # B6-1: the scheduler's resolved effective month (the same field keys.resolve_month reads
        # first: the fixed month, or next month for a legacy rolling row) guards against an
        # itinerary link for a fare departing in another month. M1-shape messages carry no month;
        # resolve_month would derive it from the departure date itself, so there is nothing
        # independent to compare and None (no check) is passed. The builder validates the value.
        # process() already drops such a message earlier (keys.resolve_month: fare outside the
        # message month -> InvalidMessage); this second check covers direct render() calls.
        links = searchlinks.build(o, d, f.get("depart_date"), f.get("return_date"), months.today(as_of_of(m, now)),
                                  catalog=deps.catalog, config=deps.links_config, marker=deps.marker,
                                  month=m.get("month"))
    except Exception as e:              # noqa: BLE001 - never send the reference fare without the link
        raise mailer.AffiliateLinkUnavailable(f"link_builder_error:{type(e).__name__}") from None
    if mode == "partner_api":
        return with_partner_link(links, m.get("aviasales_partner_url") if partner_url is None else partner_url, deps)
    return links


def with_partner_link(links, url, deps):
    """`links` with its Aviasales link replaced by the validated partner link. The expected
    destination is the direct link searchlinks built (same route, dates and month checks) without
    the marker, so the destination never comes from the message. A non-compliant LinkSet (no valid
    TP_MARKER, ...) is returned as is: render_reference blocks it with its own reasons."""
    av = links.aviasales
    if not links.compliant or av is None:
        return links
    if url is None:
        raise mailer.AffiliateLinkUnavailable("partner_link_missing")
    try:
        expected = partnerlinks.expected_destination(av.url, deps.marker)
        partnerlinks.check_format(url, expected_destination=expected, marker=deps.marker,
                                  trs=getattr(deps, "partner_trs", None),
                                  formats=getattr(deps, "partner_formats", partnerlinks.APPROVED_FORMATS))
    except partnerlinks.PartnerLinkError as e:
        raise mailer.AffiliateLinkUnavailable(f"partner_link_invalid:{e.reason}") from None
    swapped = replace(av, url=url, via="partner_api")
    return replace(links, links=tuple(swapped if link is av else link for link in links.links))


def render(m, now, deps, t, partner_url=None):
    """(subject, html, text). Raises mailer.AffiliateLinkUnavailable for a blocked email.

    EMAIL_TEMPLATE=m1 is blocked (B6 MVP safety): the legacy M1 email shows Aviasales Data API fares
    with an unmarked 「立即訂購」 link, no source label and no disclosure, which the Travelpayouts
    permission (spec §8.5, conditions 3 and 4) doesn't allow. mailer.render() is kept, unused, for
    reference. EMAIL_TEMPLATE=m1 is never a rollback; neither is a previous Lambda version unless
    verified compliant. Otherwise suspend delivery (docs/m1.4-b3-suspension-rollback.md)."""
    if deps.settings.email_template == "m1":
        raise mailer.AffiliateLinkUnavailable("legacy_m1_template_disabled")
    links = search_links(m, now, deps, partner_url)
    if links.reasons:
        deps.log(f"{t}: links: {','.join(links.reasons)}")
    return mailer.render_reference(m, deps.cities or {}, deps.airlines or {}, links)


def tag(email, route):
    return f"{route} sub:{hashlib.sha256(email.encode()).hexdigest()[:8]}"


MAX_KEYS = 10  # a user holds at most 10 subscriptions


def subscription_keys(m, route):
    """Sort keys of the matched subscriptions (R-MM-1 messages), or None for older messages.

    Every key must be a well-formed key of the message's route pair; a fixed-month key must be for
    the message's month. Anything else can never be processed (dropped, not retried)."""
    raw = m.get("subscription_keys")
    if raw is None:
        return None
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_KEYS or len(set(map(str, raw))) != len(raw):
        raise keys.InvalidMessage("invalid subscription_keys")
    for k in raw:
        parts = subkeys.parse_key(k)
        if not parts or subkeys.pair(parts[0], parts[1]) != route:
            raise keys.InvalidMessage("subscription key of another route")
        if parts[2] is not None and m.get("month") is not None and parts[2] != m.get("month"):
            raise keys.InvalidMessage("subscription key of another month")
    return sorted(raw)


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


def eligibility(row, month, price, as_of):
    """None if this subscription row still wants this alert, else why not."""
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


def recheck(deps, m, email, route, month, price, as_of):
    """Re-read the subscriptions just before sending (they may have been paused, edited, expired
    or deleted since the fare check).

    Returns (None, eligible rows) if at least one subscription still wants the alert, else
    (reason, []). With `subscription_keys` (R-MM-1) every listed key is read in the caller's own
    partition (the message's email); an alert is sent if ANY of them is still eligible, so a
    paused or deleted overlapping subscription never suppresses a valid alert of another one.
    Older messages (no keys) re-read the legacy key "ORIG-DEST" as before."""
    if deps.subscriptions is None:
        return None, []
    sub_keys = subscription_keys(m, route) or [route]
    rows, reasons = [], []
    for k in sub_keys:
        row = deps.subscriptions.get_item(Key={"email": email, "route": k}, ConsistentRead=True).get("Item")
        reason = eligibility(row, month, price, as_of)
        if reason is None:
            rows.append(row)
        else:
            reasons.append(reason)
    if rows:
        return None, rows
    return "+".join(sorted(set(reasons))), []


def presented_target(m, eligible):
    """Target shown in the email: the lowest target among the still-eligible subscriptions (the
    fare is at or below all of them). Messages without `subscription_keys` (older fare checks)
    always show the message's own value, exactly as before R-MM-1. The caller pins the result to
    the claim, so retries of one fare event never change the payload."""
    if m.get("subscription_keys") is None:
        return m.get("target_price")
    targets = []
    for row in eligible:
        try:
            targets.append(int(Decimal(str(row.get("target_price")))))
        except (InvalidOperation, ValueError, TypeError):
            pass
    return min(targets) if targets else m.get("target_price")


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
    subscription_keys(m, route)  # malformed keys: dropped here, before any history write
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
        # Rendered before any history write: a blocked email leaves no claim and no "sent" record.
        subject, page, text = render(m, now, deps, t)
    except mailer.AffiliateLinkUnavailable as e:
        deps.log(f"{t}: BLOCKED {e.reason} (reference-fare email needs the labelled Aviasales affiliate "
                 "link; nothing sent or recorded; check TP_MARKER, AFFILIATE_LINK_MODE and search-platforms.json)")
        return f"blocked:{e.reason}"
    try:
        deps.history.claim(pk, state, eid, now, deps.settings.claim_seconds)
    except ClaimConflict:
        raise Retry(f"{t}: stream changed or claimed concurrently") from None
    try:
        reason, eligible = (recheck(deps, m, email, route, month, price, as_of)
                            if deps.settings.recheck_subscription else (None, []))
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
        partner = None
        if deps.settings.affiliate_link_mode == "partner_api":
            # B6-3A: the first claimed attempt of this fare event fixes the partner link; any
            # other message or retry of the same event id (a parser re-run, a redelivery after
            # an unknown Resend outcome) reuses it, so one event id -> one payload under one
            # Idempotency-Key. The message's own link was validated before the claim.
            try:
                partner = deps.history.pin_link(pk, eid, m.get("aviasales_partner_url"))
            except ClaimConflict:
                raise Retry(f"{t}: claim lost before sending") from None
        target = presented_target(m, eligible)
        if m.get("subscription_keys") is not None and deps.settings.recheck_subscription:
            # first attempt pins its choice; a redelivery (e.g. after an unknown Resend outcome)
            # reuses it even if the subscriptions changed meanwhile
            try:
                target = deps.history.pin_target(pk, eid, target)
            except ClaimConflict:
                raise Retry(f"{t}: claim lost before sending") from None
        if target != m.get("target_price") or partner is not None:
            # e.g. the strictest overlapping subscription was paused since the fare check: show
            # the target of one that still wants the alert (pure re-render, same links); in
            # partner_api mode the pinned link is validated again here and used
            try:
                subject, page, text = render({**m, "target_price": target}, now, deps, t, partner)
            except mailer.AffiliateLinkUnavailable as e:
                if ours:
                    # an earlier attempt may have sent with this link: never send another one
                    deps.history.commit(pk=pk, eid=eid, now=now, price=price, route=route, month=month, sid=sid,
                                        email=email, dual_write=deps.settings.legacy_dual_write)
                    deps.log(f"{t}: recorded unconfirmed earlier attempt (pinned link rejected: {e.reason})")
                    return "recorded:unconfirmed"
                deps.history.release(pk, eid)
                deps.log(f"{t}: BLOCKED {e.reason} (pinned partner link; nothing sent)")
                return f"blocked:{e.reason}"
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
