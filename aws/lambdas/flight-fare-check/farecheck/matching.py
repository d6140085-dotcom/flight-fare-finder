"""Match one work item's fare against the route's subscription rows.

A row matches only if it belongs to the item's route and effective month (resolved at the
scheduler's `as_of`), is active (not paused, not expired at `as_of`), has a valid target and
the target is >= the fare (M1 rule). `message_body` builds the M1 message shape key for key, so
the notification Lambda needs no change; it is used only on the live path.
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
        if row.get("route") != item.route:
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


def message_body(row, item, offer, usd_offer):
    body = {"email": row["email"], "route": item.route, "plan_name": row.get("plan_name"),
            "target_price": int(target_of(row)), "cheapest": offer.message_dict()}
    if usd_offer:
        body["cheapest_usd"] = usd_offer.message_dict()
    return body
