"""Wrapper and parser logic with every dependency injected (handlers in ../wrapper.py and
../parser.py wire in boto3). Offline tests and the dry-run tool call these directly.

Dry run is an isolated, in-process path: the wrapper evaluates every selected work item itself
(Travelpayouts lookup + matching against rows read without email) and returns a report. It
never invokes the parser or any other Lambda, never creates an SQS client and never writes.
Live mode fans out to the parser, passing the resolved month and the scheduler's `as_of`.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import matching, months, status, workitems
from .config import ConfigError
from .effects import DryRunPublisher, DryRunViolation, SqsPublisher
from .travelpayouts import OK, Client

MAX_AS_OF_AGE = timedelta(hours=24)     # async retries happen within hours; older payloads are refused
MAX_AS_OF_SKEW = timedelta(minutes=10)  # clock skew allowance for an as_of slightly in the future


@dataclass
class Evaluation:
    item: workitems.WorkItem
    record: dict
    offer: object = None
    usd_offer: object = None
    matched: list = field(default_factory=list)
    skipped: dict = field(default_factory=dict)


def evaluate(item, *, client, rows, as_of, now, catalog, fetch_usd):
    """One work item: TWD lookup, fare-check record, and (if a fare exists) matching rows.
    `rows` is a zero-argument callable, so subscriptions are read only when there is a fare."""
    twd = client.fetch(item.origin, item.destination, item.month, "twd")
    ev = Evaluation(item, status.record(item, twd, now))
    if twd.kind != OK:
        return ev
    usd = client.fetch(item.origin, item.destination, item.month, "usd") if fetch_usd else None
    ev.offer, ev.usd_offer = twd.offer, (usd.offer if usd and usd.kind == OK else None)
    ev.matched, ev.skipped = matching.match(rows(), item, twd.offer, as_of, catalog)
    return ev


class DryRunEvaluator:
    """In-process evaluation for the wrapper's dry run. Holds no AWS client at all."""
    def __init__(self, *, settings, catalog, client, as_of, log=print):
        if not settings.dry_run:
            raise DryRunViolation("dry-run evaluator used in live mode")
        self.settings, self.catalog, self.client, self.as_of, self.log = settings, catalog, client, as_of, log

    def __call__(self, item, rows):
        ev = evaluate(item, client=self.client, rows=lambda: [r for r in rows if workitems.route_pair(r) == item.route],
                      as_of=self.as_of, now=self.client.now, catalog=self.catalog, fetch_usd=self.settings.fetch_usd)
        out = {"key": item.key, "fare_check": status.api_view(ev.record), "rejected": ev.record["rejected"],
               "would_alert": len(ev.matched), "would_alert_targets": sorted(int(matching.target_of(r)) for r in ev.matched),
               "skipped": ev.skipped}
        self.log(f"DRY_RUN {item.key}: {ev.record['kind']} {ev.record.get('price', '-')} TWD, "
                 f"would alert {len(ev.matched)} subscription(s)")
        return out


def run_wrapper(*, settings, catalog, reader, now, invoker=None, dry_evaluator=None, log=print):
    """`now` is the scheduler's single as_of instant for this run."""
    if settings.dry_run:
        if dry_evaluator is None or invoker is not None:
            raise DryRunViolation("dry run requires the in-process evaluator and no invoker; refusing to run")
    elif invoker is None or dry_evaluator is not None:
        raise ConfigError("live mode requires the parser invoker and no dry-run evaluator")
    rows = list(reader.rows_without_email())
    plan = workitems.build(rows, now, catalog)
    items, window = workitems.select_window(plan.items, settings.item_cap, now, settings.schedule_minutes)
    if window["windows"] > 1:
        log(f"WARNING {window['total']} work items exceed the cap {settings.item_cap}: checking window "
            f"{window['window'] + 1}/{window['windows']} ({window['selected']} items); every item is "
            f"checked once per {window['windows']} scheduled runs")
    out = {"ok": True, "dry_run": settings.dry_run, "as_of": workitems.iso_utc(now), "counts": plan.counts,
           "window": window, "work_items": [i.key for i in items]}
    if settings.dry_run:
        out["dry_run_report"] = [dry_evaluator(item, rows) for item in items]
        log(f"DRY_RUN evaluated {len(items)} work item(s) in-process {plan.counts}; nothing was sent")
        return out
    for item in items:
        invoker.invoke(item.payload(now))
    log(f"fanned out {len(items)} work item(s) {plan.counts}: {', '.join(out['work_items'])}")
    return out


def parse_event(event, catalog, now):
    """(WorkItem, as_of). M1 wrapper payloads have no `month`/`as_of`: next month at `now`."""
    o, d = event.get("origin"), event.get("destination")
    route = event.get("route") or f"{o}-{d}"
    if not isinstance(o, str) or not isinstance(d, str) or route != f"{o}-{d}" or not catalog.known_route(o, d):
        raise ValueError("unknown or inconsistent route")
    as_of = now
    if event.get("as_of") is not None:
        try:
            as_of = datetime.strptime(event["as_of"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            raise ValueError("invalid as_of") from None
        if not now - MAX_AS_OF_AGE <= as_of <= now + MAX_AS_OF_SKEW:
            raise ValueError("as_of outside the accepted window")
    month = event.get("month") or months.effective_month(None, as_of)
    if not months.valid_month(month):
        raise ValueError("invalid month")
    if months.is_expired(month, as_of):
        raise ValueError("month already expired at as_of")
    return workitems.WorkItem(o, d, month), as_of


def run_parser(event, *, settings, catalog, token, reader, now, transport=None, client_factory=None, log=print):
    dry_run = settings.dry_run or event.get("dry_run") is True   # a payload can only turn it on
    try:
        item, as_of = parse_event(event, catalog, now)
    except ValueError as e:  # returned, not raised: an async retry can't fix a bad payload
        log(f"rejected parser event: {e}")
        return {"ok": False, "dry_run": dry_run, "error": str(e), "matched": 0}
    kw = {"transport": transport} if transport else {}
    client = Client(token, catalog, now, api=settings.fare_api, v3_limit=settings.v3_limit, log=log, **kw)
    ev = evaluate(item, client=client, rows=lambda: reader.rows_for_route(item.route), as_of=as_of, now=now,
                  catalog=catalog, fetch_usd=settings.fetch_usd)
    out = {"ok": True, "dry_run": dry_run, "route": item.route, "month": item.month,
           "as_of": workitems.iso_utc(as_of), "source": ev.record["source"], "matched": 0,
           "fare_check": status.api_view(ev.record), "rejected": ev.record["rejected"], "api_requests": client.requests}
    if ev.offer is None:
        log(f"{item.key}: no TWD fare ({ev.record['kind']}: {ev.record.get('reason')}; "
            f"seen {ev.record['offers_seen']}, rejected {ev.record['rejected']}), skipping")
        return out
    o = ev.offer
    log(f"{item.key}: {o.price} TWD ({o.airline}) {o.depart_date}..{o.return_date} "
        f"{o.origin_airport or '-'}->{o.destination_airport or '-'} via {ev.record['source']}"
        + (f", {ev.usd_offer.price} USD" if ev.usd_offer else ", no USD"))
    publisher = DryRunPublisher(log) if dry_run else SqsPublisher(settings.queue_url, client_factory, dry_run)
    v2_as_of = as_of if settings.message_version == 2 else None
    # One message per user and stream: overlapping rolling + fixed rows of one user that both
    # match produce one alert listing both subscriptions (R-MM-1 overlap policy).
    bodies = matching.messages(ev.matched, item, o, ev.usd_offer, v2_as_of)
    for body in bodies:
        publisher.send(body)
    out.update({"cheapest_twd": o.price, "matched": len(bodies), "skipped": ev.skipped})
    log(f"{item.key}: enqueued {len(bodies)}{' (DRY_RUN, nothing sent)' if dry_run else ''}, skipped {ev.skipped}")
    return out
