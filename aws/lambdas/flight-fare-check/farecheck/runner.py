"""Wrapper and parser logic with every dependency injected (handlers in ../wrapper.py and
../parser.py wire in boto3). Offline tests and the dry-run tool call these directly."""
from . import matching, months, status, workitems
from .effects import DryRunPublisher, SqsPublisher, subscriber_tag
from .travelpayouts import OK, Client


def run_wrapper(*, settings, catalog, reader, invoker, now, log=print):
    plan = workitems.build(reader.rows_without_email(), now, catalog)
    items = plan.items
    if len(items) > settings.max_work_items:
        log(f"WARNING {len(items)} work items exceed MAX_WORK_ITEMS={settings.max_work_items}; "
            f"checking the first {settings.max_work_items} (sorted) this run")
        items = items[:settings.max_work_items]
    for item in items:
        invoker.invoke(item.payload(dry_run=settings.dry_run))
    keys = [i.key for i in items]
    log(f"{'DRY_RUN ' if settings.dry_run else ''}fanned out {len(keys)} work item(s) {plan.counts}: {', '.join(keys)}")
    return {"ok": True, "dry_run": settings.dry_run, "work_items": keys, "counts": plan.counts}


def parse_event(event, catalog, now):
    """WorkItem from a parser event. M1 wrapper payloads have no `month`: next month applies."""
    o, d = event.get("origin"), event.get("destination")
    route = event.get("route") or f"{o}-{d}"
    if not isinstance(o, str) or not isinstance(d, str) or route != f"{o}-{d}" or not catalog.known_route(o, d):
        raise ValueError("unknown or inconsistent route")
    month = event.get("month") or months.effective_month(None, now)
    if not months.valid_month(month):
        raise ValueError("invalid month")
    return workitems.WorkItem(o, d, month)


def run_parser(event, *, settings, catalog, token, reader, now, transport=None, client_factory=None,
               sink=None, log=print):
    dry_run = settings.dry_run or event.get("dry_run") is True   # a payload can only turn it on
    try:
        item = parse_event(event, catalog, now)
    except ValueError as e:  # returned, not raised: an async retry can't fix a bad payload
        log(f"rejected parser event: {e}")
        return {"ok": False, "dry_run": dry_run, "error": str(e), "matched": 0}
    kw = {"transport": transport} if transport else {}
    client = Client(token, catalog, now, api=settings.fare_api, v3_limit=settings.v3_limit, log=log, **kw)
    twd = client.fetch(item.origin, item.destination, item.month, "twd")
    rec = status.record(item, twd, now)
    (sink or status.NullSink()).put(rec)
    out = {"ok": True, "dry_run": dry_run, "route": item.route, "month": item.month, "source": twd.source,
           "matched": 0, "fare_check": status.api_view(rec), "rejected": rec["rejected"]}
    if twd.kind != OK:
        log(f"{item.key}: no TWD fare ({twd.kind}: {twd.reason}; seen {twd.offers_seen}, rejected {rec['rejected']}), skipping")
        return out
    usd = client.fetch(item.origin, item.destination, item.month, "usd") if settings.fetch_usd else None
    usd_offer = usd.offer if usd and usd.kind == OK else None
    o = twd.offer
    log(f"{item.key}: {o.price} TWD ({o.airline}) {o.depart_date}..{o.return_date} "
        f"{o.origin_airport or '-'}->{o.destination_airport or '-'} via {twd.source}"
        + (f", {usd_offer.price} USD" if usd_offer else ", no USD"))
    bodies, skipped = matching.match(reader.rows_for_route(item.route), item, o, usd_offer, now, catalog)
    publisher = DryRunPublisher(log) if dry_run else SqsPublisher(settings.queue_url, client_factory, dry_run)
    for body in bodies:
        publisher.send(body)
    out.update({"cheapest_twd": o.price, "matched": len(bodies), "skipped": skipped,
                "api_requests": client.requests})
    if dry_run:
        out["would_enqueue"] = [{"route": b["route"], "subscriber": subscriber_tag(b["email"]),
                                 "target_price": b["target_price"]} for b in bodies]
    log(f"{item.key}: enqueued {len(bodies)}{' (DRY_RUN, nothing sent)' if dry_run else ''}, skipped {skipped}")
    return out
