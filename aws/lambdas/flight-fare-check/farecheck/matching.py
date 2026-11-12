"""Match one work item's fare against the route's subscription rows.

A row matches only if it belongs to the item's route pair and effective month (resolved at the
scheduler's `as_of`), is active (not paused, not expired at `as_of`), has a valid target and
the target is >= the fare (M1 rule). Since R-MM-1 a pair can have several rows per user: the
legacy rolling row ("KHH-TYO") and fixed-month rows ("KHH-TYO#2027-01", ...). Only rows whose
effective month equals the item's month can match, so at most one rolling row and one fixed row
of a user meet here (the rolling/fixed overlap; policy in docs/m1.4-r-mm-1-multi-month.md §4).

`messages` builds ONE message per user for the item (the notification dedup stream is
(user, route, month)), carrying every matched subscription's sort key. The body keeps the M1
message shape key for key; v2 messages add `month`, `as_of` and `subscription_keys`.
"""
from decimal import Decimal, InvalidOperation

from . import workitems


def target_of(row):
    tp = row.get("target_price")
    if tp is None or isinstance(tp, bool):
        return None
    try:
        d = Decimal(str(tp))
    except InvalidOperation:
        return None
    return d if d.is_finite() and d > 0 else None


def match(rows, item, offer, as_of, catalog):
    """(matched rows, skipped counts). Rows may lack `email` (dry run)."""
    matched, skipped = [], {"other_route": 0, "other_month": 0, "paused": 0, "expired": 0,
                            "invalid": 0, "no_target": 0, "above_target": 0}
    price = Decimal(offer.price)
    for row in rows:
        if workitems.route_pair(row) != item.route:
            if workitems.route_codes(row) is None and str(row.get("route", "")).split("#")[0] == item.route:
                skipped["invalid"] += 1  # this pair's key, but key and attributes disagree
            else:
                skipped["other_route"] += 1
            continue
        state, row_item = workitems.classify(row, as_of, catalog)
        if state != workitems.ACTIVE:
            skipped[state] += 1
            continue
        if row_item.month != item.month:
            skipped["other_month"] += 1
            continue
        tp = target_of(row)
        if tp is None:
            skipped["no_target"] += 1
            continue
        if tp < price:
            skipped["above_target"] += 1
            continue
        matched.append(row)
    return matched, skipped


def primary(rows):
    """The subscription an alert is presented for: the strictest (lowest) matched target, then
    the fixed-month row before the rolling one, then the key (deterministic)."""
    return min(rows, key=lambda r: (target_of(r), r.get("departure_month") is None, r["route"]))


def message_body(rows, item, offer, usd_offer, as_of=None):
    """One message for one user's matched rows of this item (same route pair and month).

    M1 shape. `target_price`/`plan_name` come from `primary(rows)`: the fare is at or below every
    matched target, so the lowest one is the most informative true statement. With `as_of`
    (NOTIFY_MESSAGE_VERSION=2) three fields are appended: the scheduler's resolved `month`,
    `as_of`, and `subscription_keys` (sorted sort keys of every matched row, which the v2
    notification Lambda re-reads before sending). The M1 notification Lambda ignores unknown
    fields, so v2 messages are safe for it too."""
    rows = rows if isinstance(rows, (list, tuple)) else [rows]
    emails = {r["email"] for r in rows}
    if len(emails) != 1:
        raise ValueError("one message per user")
    p = primary(rows)
    body = {"email": p["email"], "route": item.route, "plan_name": p.get("plan_name"),
            "target_price": int(target_of(p)), "cheapest": offer.message_dict()}
    if usd_offer:
        body["cheapest_usd"] = usd_offer.message_dict()
    if as_of is not None:
        body["month"] = item.month
        body["as_of"] = workitems.iso_utc(as_of)
        body["subscription_keys"] = sorted(r["route"] for r in rows)
    return body


def messages(matched, item, offer, usd_offer, as_of=None):
    """One message per user (dedup stream = user, route, month), users in a stable order."""
    by_user = {}
    for row in matched:
        by_user.setdefault(row["email"], []).append(row)
    return [message_body(by_user[e], item, offer, usd_offer, as_of) for e in sorted(by_user)]
