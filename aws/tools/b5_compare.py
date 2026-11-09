"""B5: local, read-only Travelpayouts v1/v3 comparison (docs/m1.4-b5-live-validation-plan.md).

Standard library only (Python 3.10+), no AWS, no boto3, no Docker. It reuses the Phase 2A
adapters (aws/lambdas/flight-fare-check/farecheck), so requests and validation are exactly
the production ones.

Commands (Windows PowerShell: use `python` or `py -3.12`, and backslashes in paths):

  preview                  Show the plan, every request URL, counts and duration. ZERO network.
  live --round N --confirm-live
                           The approved live test. Also needs B5_LIVE_APPROVED=yes and the
                           token in TP_TOKEN. Sequential, rate-limited, capped.
  check-output DIR         Scan a results folder for the token, emails and secret-like strings.
  summarize DIR [DIR ...]  Combine rounds: A. technical compatibility (PASS/FAIL) and
                           B. business suitability (PASS/CONDITIONAL/FAIL).

Importing this module or running the unit tests never makes a network request: the only code
that opens a connection is LiveTransport, which is constructed only by `live` after all gates.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
AWS = TOOLS.parent
sys.path.insert(0, str(AWS / "lambdas" / "flight-fare-check"))

from farecheck import compat, config, months  # noqa: E402
from farecheck import travelpayouts as tp     # noqa: E402

TOOL_VERSION = "1"
TOKEN_ENV = "TP_TOKEN"
APPROVAL_ENV = "B5_LIVE_APPROVED"
TOKEN_RE = re.compile(r"^[A-Za-z0-9]{16,64}$")
RATE_HEADER_RE = re.compile(r"ratelimit|rate-limit|retry-after", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
AWS_KEY_RE = re.compile(r"AKIA[0-9A-Z]{16}")
FALSE_POSITIVE_REASONS = ("one_way", "currency_mismatch", "wrong_month", "departed", "expired",
                          "return_before_departure", "return_too_far", "origin_mismatch",
                          "destination_mismatch", "bad_price", "bad_departure", "bad_return", "not_object")
AIRPORT_REASONS = ("origin_airport_unexpected", "destination_airport_unexpected")
MAX_CONSECUTIVE_FAILURES = 3


class Refused(SystemExit):
    """A safety gate refused to continue. Exit code 2; nothing was sent."""
    def __init__(self, message):
        super().__init__(2)
        self.message = message


class Abort(Exception):
    """Stop the round now (auth failure, rate limit, repeated errors)."""


# ---- plan -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Request:
    seq: int
    api: str          # v1 | v3
    variant: str      # twd | market | usd
    origin: str
    destination: str
    month: str

    @property
    def item_key(self):
        return f"{self.origin}-{self.destination}#{self.month}"


def load_plan(path=None):
    return json.loads(Path(path or TOOLS / "b5_routes.json").read_text(encoding="utf-8"))


def build_requests(plan, now, catalog, usd=False):
    """Deterministic request list for one round. USD checks (optional) are appended per item."""
    base = months.current_month(now)
    items = []
    for g in plan["groups"]:
        for k in g["month_offsets"]:
            for d in g["destinations"]:
                if not catalog.known_route(g["origin"], d):
                    raise ValueError(f"{g['origin']}-{d} is not in airports.json")
                items.append((g["origin"], d, months.add_months(base, k)))
    if len(set(items)) != len(items):
        raise ValueError("duplicate work items in the plan")
    reqs = []
    for o, d, m in items:
        for api in ("v1", "v3"):
            reqs.append((api, "twd", o, d, m))
    mc = plan.get("market_check")
    if mc:
        m = months.add_months(base, mc["month_offset"])
        for route in mc["routes"]:
            o, d = route.split("-")
            reqs.append(("v3", "market", o, d, m))
    if usd:
        reqs += [("v3", "usd", o, d, m) for o, d, m in items]
    return [Request(i + 1, *r) for i, r in enumerate(reqs)]


class B5Client(tp.Client):
    """Production client plus optional extra query parameters (market check only)."""
    def __init__(self, *args, extra=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.extra = extra or {}

    def request_url(self, origin, destination, month, currency):
        url = super().request_url(origin, destination, month, currency)
        return url + "".join(f"&{k}={v}" for k, v in self.extra.items())


def request_url(req, v3_limit=30, market="tw"):
    """The URL a request will use (no token: the token is only ever a header)."""
    extra = {"market": market} if req.variant == "market" else None
    client = B5Client("", config.load_catalog(), datetime.now(timezone.utc), api=req.api, v3_limit=v3_limit, extra=extra)
    return client.request_url(req.origin, req.destination, req.month, "usd" if req.variant == "usd" else "twd")


# ---- transport ------------------------------------------------------------------------------

class LiveTransport:
    """The only code path that opens a network connection. Records response metadata; never
    records request headers. Enforces the request cap and the minimum interval."""
    def __init__(self, max_requests, interval, opener=urllib.request.urlopen, clock=time.monotonic, sleep=time.sleep):
        self.max_requests, self.interval = max_requests, interval
        self.opener, self.clock, self.sleep = opener, clock, sleep
        self.count, self.last_start, self.last = 0, None, None

    def __call__(self, url, headers, timeout):
        if self.count >= self.max_requests:
            raise Abort(f"request cap {self.max_requests} reached")
        if self.last_start is not None:
            wait = self.interval - (self.clock() - self.last_start)
            if wait > 0:
                self.sleep(wait)
        self.last_start = self.clock()
        self.count += 1
        self.last = {"status": None, "rate_limit": {}, "body": None, "elapsed_ms": None}
        started = self.clock()
        req = urllib.request.Request(url, headers=headers)
        try:
            with self.opener(req, timeout=timeout) as r:
                self.last["status"] = getattr(r, "status", 200)
                self.last["rate_limit"] = rate_headers(getattr(r, "headers", {}))
                raw = r.read()
        except urllib.error.HTTPError as e:
            self.last.update(status=e.code, rate_limit=rate_headers(e.headers or {}))
            raise tp.HttpError(e.code) from None
        finally:
            self.last["elapsed_ms"] = round((self.clock() - started) * 1000)
        body = json.loads(raw)       # ValueError -> adapter reports "malformed"
        self.last["body"] = body
        return body


def rate_headers(headers):
    items = headers.items() if hasattr(headers, "items") else []
    return {k: v for k, v in items if RATE_HEADER_RE.search(k)}


# ---- token and output hygiene ---------------------------------------------------------------

def read_token(env):
    token = (env.get(TOKEN_ENV) or "").strip()
    if not token:
        raise Refused(f"{TOKEN_ENV} is not set. See the PowerShell guide for setting it securely.")
    if not TOKEN_RE.match(token):
        raise Refused(f"{TOKEN_ENV} does not look like a Travelpayouts API token (16-64 letters/digits); "
                      "refusing to send it anywhere.")
    return token


def token_fingerprint(token):
    """Lets the owner confirm which token was used without revealing it."""
    return hashlib.sha256(token.encode()).hexdigest()[:8]


def sanitize(obj, token):
    """Replace the token anywhere in strings (keys and values) before anything is written."""
    if isinstance(obj, dict):
        return {sanitize(k, token): sanitize(v, token) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v, token) for v in obj]
    if isinstance(obj, str) and token and token in obj:
        return obj.replace(token, "[REDACTED]")
    return obj


def scan_text(text, token=None):
    findings = []
    if token and token in text:
        findings.append("api token")
    if "x-access-token" in text.lower():
        findings.append("X-Access-Token header name")
    if EMAIL_RE.search(text):
        findings.append("email address")
    if JWT_RE.search(text):
        findings.append("JWT-like string")
    if AWS_KEY_RE.search(text):
        findings.append("AWS access key")
    if re.search(r"[?&]marker=\w+", text):
        findings.append("affiliate marker parameter")
    return findings


def check_output(directory, token=None):
    """{relative file: [findings]} for every file in the folder (empty dict = clean)."""
    out = {}
    for p in sorted(Path(directory).rglob("*")):
        if p.is_file():
            f = scan_text(p.read_text(encoding="utf-8", errors="replace"), token)
            if f:
                out[str(p.relative_to(directory))] = f
    return out


# ---- live round -----------------------------------------------------------------------------

def offer_view(raw, verdict, reason):
    keys = ("price", "currency", "origin", "destination", "origin_airport", "destination_airport", "airline",
            "departure_at", "return_at", "transfers", "return_transfers", "expires_at", "link")
    v = {k: raw.get(k) for k in keys if isinstance(raw, dict) and k in raw}
    v.update(verdict=verdict, reason=reason)
    return v


def raw_offers(api, body, destination):
    if not isinstance(body, dict):
        return []
    data = body.get("data")
    if api == "v1":
        per = data.get(destination) if isinstance(data, dict) else None
        return list(per.values()) if isinstance(per, dict) else []
    return data if isinstance(data, list) else []


def analyse(req, result, body, catalog, now):
    """Per-offer verdicts and the risk signals for one response."""
    currency = "usd" if req.variant == "usd" else "twd"
    offers = []
    for raw in raw_offers(req.api, body, req.destination):
        o, why = tp.validate_offer(raw, origin=req.origin, destination=req.destination, month=req.month,
                                   currency=currency, catalog=catalog, now=now, source=req.api)
        offers.append(offer_view(raw, "accepted" if o else "rejected", why))
    accepted = result.offer.price if result.offer else None
    naive = [tp.to_price(x.get("price")) for x in offers]
    naive = min([p for p in naive if p], default=None)
    risky = [x for x in offers if x["verdict"] == "rejected" and tp.to_price(x.get("price"))
             and (accepted is None or tp.to_price(x["price"]) < accepted)]
    return offers, {
        "naive_min_price": naive, "validated_price": accepted,
        # an M1-style "cheapest offer, no checks" would have used a rejected offer:
        "false_positive_prevented": bool(naive and (accepted is None or naive < accepted)
                                         and any(x["reason"] in FALSE_POSITIVE_REASONS for x in risky)),
        "cheaper_rejected": Counter(x["reason"] for x in risky),
        "airport_rejected_cheaper": [{"price": tp.to_price(x["price"]), "origin_airport": x.get("origin_airport"),
                                      "destination_airport": x.get("destination_airport"),
                                      "destination": x.get("destination"), "reason": x["reason"]}
                                     for x in risky if x["reason"] in AIRPORT_REASONS],
    }


def run_round(requests, *, token, transport, catalog, now, out_dir, v3_limit=30, market="tw", log=print):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows, by_item, aborted, failures = [], {}, None, 0
    for req in requests:
        extra = {"market": market} if req.variant == "market" else None
        client = B5Client(token, catalog, now, api=req.api, v3_limit=v3_limit, transport=transport,
                          log=lambda m: None, extra=extra)
        transport.last = None
        try:
            result = client.fetch(req.origin, req.destination, req.month, "usd" if req.variant == "usd" else "twd")
        except Abort as e:
            aborted = str(e)
            break
        meta = transport.last or {}
        offers, signals = analyse(req, result, meta.get("body"), catalog, now)
        row = {"seq": req.seq, "api": req.api, "variant": req.variant, "item": req.item_key,
               "month": req.month, "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "url": client.request_url(req.origin, req.destination, req.month,
                                         "usd" if req.variant == "usd" else "twd"),
               "status": meta.get("status"), "elapsed_ms": meta.get("elapsed_ms"), "rate_limit": meta.get("rate_limit", {}),
               "response_currency": (meta.get("body") or {}).get("currency") if isinstance(meta.get("body"), dict) else None,
               "kind": result.kind, "reason": result.reason, "offers_seen": result.offers_seen,
               "rejected": dict(result.rejected), "signals": signals, "offers": offers,
               "chosen": None if not result.offer else {
                   "price": result.offer.price, "currency": result.offer.currency,
                   "depart_at": result.offer.depart_at, "return_at": result.offer.return_at,
                   "origin_airport": result.offer.origin_airport,
                   "destination_airport": result.offer.destination_airport}}
        rows.append(row)
        by_item.setdefault(req.item_key, {})[f"{req.api}:{req.variant}"] = result
        log(f"[{req.seq:3d}/{len(requests)}] {req.api} {req.variant:6s} {req.item_key:18s} "
            f"HTTP {row['status']} {result.kind}{' ' + result.reason if result.reason else ''}")
        status = meta.get("status")
        if status in (401, 403):
            aborted = f"HTTP {status}: token rejected; stopping (fix the token, then rerun)"
            break
        if status == 429:
            aborted = "HTTP 429: rate limited; stopping without retry"
            break
        failures = failures + 1 if result.kind == tp.ERROR else 0
        if failures >= MAX_CONSECUTIVE_FAILURES:
            aborted = f"{failures} consecutive errors; stopping"
            break
    comparisons = {}
    for key, res in by_item.items():
        if "v1:twd" in res and "v3:twd" in res:
            comparisons[key] = compat.compare(res["v1:twd"], res["v3:twd"])
    summary = summarize_rows(rows, comparisons)
    summary.update({"aborted": aborted, "requests_planned": len(requests), "requests_sent": transport.count})
    summary["preliminary"] = evaluate(rows, [{"dir": str(out_dir), "aborted": aborted}], token=None,
                                      scan_dirs=False)
    write_json(out_dir / "requests.jsonl", rows, token, jsonl=True)
    write_json(out_dir / "comparisons.json", comparisons, token)
    write_json(out_dir / "summary.json", summary, token)
    (out_dir / "summary.md").write_text(sanitize(summary_markdown(summary), token), encoding="utf-8")
    return summary


def write_json(path, data, token, jsonl=False):
    data = sanitize(data, token)
    text = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in data) + "\n" if jsonl \
        else json.dumps(data, ensure_ascii=False, indent=2, default=str)
    Path(path).write_text(text, encoding="utf-8")


# ---- summaries and acceptance criteria ------------------------------------------------------
#
# B5 reports two separate conclusions (plan §5.7):
#   A. Technical compatibility (PASS / FAIL): is the integration correct and safe?
#   B. Business suitability (PASS / CONDITIONAL / FAIL): is v3's fare coverage good enough, and
#      should v1 stay available as a fallback?
# Relative prices between v1 and v3 are reported for review only. They never fail B5 and never
# select the production API on their own.

FORMAT_REASONS = ("bad_price", "bad_departure", "bad_return", "not_object")
KNOWN_ERROR_REASONS = ("malformed", "unavailable", "api_unsuccessful", "currency_mismatch")
PRICE_GAP_PCT = 10.0
USD_RATIO_RANGE = (10.0, 60.0)      # plausible TWD per USD; outside means currency=twd was ignored
V3_SHARE_FLOOR = 0.5                # v3 below half of all covered route/months: not suitable alone


def twd_rows(rows, api):
    return {(r.get("round", ""), r["item"]): r for r in rows if r["api"] == api and r["variant"] == "twd"}


def label(key):
    rnd, item = key
    return f"{rnd}:{item}" if rnd else item


def coverage(rows):
    """Valid-fare coverage per endpoint, computed per (round, route/month)."""
    v1, v3 = twd_rows(rows, "v1"), twd_rows(rows, "v3")
    keys = sorted(set(v1) & set(v3))
    ok = lambda d, k: d[k]["kind"] == tp.OK
    groups = {"both": [k for k in keys if ok(v1, k) and ok(v3, k)],
              "v1_only": [k for k in keys if ok(v1, k) and not ok(v3, k)],
              "v3_only": [k for k in keys if ok(v3, k) and not ok(v1, k)],
              "neither": [k for k in keys if not ok(v1, k) and not ok(v3, k)]}
    n = len(keys)
    rate = lambda count: round(count / n * 100, 1) if n else None
    kinds = lambda d: dict(Counter(d[k]["kind"] for k in keys))
    return {"compared": n,
            "v1_valid": len(groups["both"]) + len(groups["v1_only"]),
            "v3_valid": len(groups["both"]) + len(groups["v3_only"]),
            "v1_valid_pct": rate(len(groups["both"]) + len(groups["v1_only"])),
            "v3_valid_pct": rate(len(groups["both"]) + len(groups["v3_only"])),
            "union_valid": n - len(groups["neither"]), "neither_pct": rate(len(groups["neither"])),
            "v1_kinds": kinds(v1), "v3_kinds": kinds(v3),
            **{name: [label(k) for k in ks] for name, ks in groups.items()}}


def parse_when(value):
    w = tp.parse_when(value)
    return w and (w[1] or datetime(w[0].year, w[0].month, w[0].day, tzinfo=timezone.utc))


def freshness(row):
    """v1 offers carry expires_at; v3 offers carry no freshness field."""
    if row["api"] != "v1" or not row["chosen"]:
        return {"source": row["api"], "expires_at": None, "hours_left": None,
                "note": "v3 provides no freshness field" if row["api"] == "v3" else "no fare"}
    match = next((o for o in row["offers"] if o["verdict"] == "accepted"
                  and o.get("departure_at") == row["chosen"]["depart_at"]
                  and tp.to_price(o.get("price")) == row["chosen"]["price"]), None)
    exp, checked = parse_when(match and match.get("expires_at")), parse_when(row.get("checked_at"))
    hours = round((exp - checked).total_seconds() / 3600, 1) if exp and checked else None
    return {"source": "v1", "expires_at": match and match.get("expires_at"), "hours_left": hours}


def price_differences(rows, threshold=PRICE_GAP_PCT):
    """Every overlapping route/month whose v1 and v3 cheapest fares differ by more than the
    threshold, with the facts needed to explain the gap. Never a failure by itself."""
    v1, v3 = twd_rows(rows, "v1"), twd_rows(rows, "v3")
    out, overlapping = [], 0
    for k in sorted(set(v1) & set(v3)):
        a, b = v1[k]["chosen"], v3[k]["chosen"]
        if not (a and b):
            continue
        overlapping += 1
        diff = abs(b["price"] - a["price"]) / a["price"] * 100
        if diff <= threshold:
            continue
        rt = lambda c: bool(c["return_at"]) and c["return_at"][:10] >= c["depart_at"][:10]
        out.append({"item": label(k), "v1_price": a["price"], "v3_price": b["price"], "diff_pct": round(diff, 1),
                    "lower": "v3" if b["price"] < a["price"] else "v1",
                    "v1_dates": [a["depart_at"][:10], a["return_at"][:10]],
                    "v3_dates": [b["depart_at"][:10], b["return_at"][:10]],
                    "same_departure_date": a["depart_at"][:10] == b["depart_at"][:10],
                    "same_return_date": a["return_at"][:10] == b["return_at"][:10],
                    "v3_airports": [b["origin_airport"], b["destination_airport"]],
                    "both_round_trip": rt(a) and rt(b), "both_twd": a["currency"] == b["currency"] == "TWD",
                    "v1_freshness": freshness(v1[k]), "v3_freshness": freshness(v3[k])})
    return {"overlapping": overlapping, "over_threshold": len(out), "threshold_pct": threshold, "cases": out}


def departed_at_check(departure, checked_at):
    """Same rule as production validation: aware times compare as instants, dates as Taipei dates."""
    dep, chk = tp.parse_when(departure), parse_when(checked_at)
    if not dep or not chk:
        return False
    return dep[1] < chk if dep[1] else dep[0] < months.today(chk)


def check(ok, detail, not_verified=False):
    return {"verdict": "not_verified" if not_verified else ("pass" if ok else "fail"), "detail": detail}


def technical(rows, rounds, token=None, scan_dirs=True):
    accepted = [(r, o) for r in rows for o in r["offers"] if o["verdict"] == "accepted"]
    twd = [r for r in rows if r["variant"] != "usd"]
    fmt = Counter(o["reason"] for r in rows for o in r["offers"] if o["reason"] in FORMAT_REASONS)
    malformed_200 = [r["item"] for r in rows if r["status"] == 200 and r["reason"] == "malformed"]
    no_return = [r["item"] for r, o in accepted if not o.get("return_at")]
    bad_order = [r["item"] for r, o in accepted
                 if o.get("return_at") and o.get("departure_at") and o["return_at"][:10] < o["departure_at"][:10]]
    v3_one_way_param = [r["item"] for r in rows if r["api"] == "v3" and "one_way=false" not in r["url"]]
    off_month = [r["item"] for r, o in accepted if str(o.get("departure_at", ""))[:7] != r.get("month", r["item"][-7:])]
    departed = [r["item"] for r, o in accepted if departed_at_check(o.get("departure_at"), r.get("checked_at"))]
    wrong_cur = sorted({str(r["response_currency"]) for r in twd if r["response_currency"] not in (None, "twd", "TWD")})
    chosen_cur = sorted({r["chosen"]["currency"] for r in twd if r["chosen"]} - {"TWD"})
    ratios = []
    usd = twd_rows([dict(r, variant="twd") for r in rows if r["variant"] == "usd"], "v3")
    for k, r in twd_rows(rows, "v3").items():
        if k in usd and r["chosen"] and usd[k]["chosen"]:
            ratios.append(r["chosen"]["price"] / usd[k]["chosen"]["price"])
    bad_ratio = [round(x, 2) for x in ratios if not USD_RATIO_RANGE[0] <= x <= USD_RATIO_RANGE[1]]
    currency_evidence = any(r["response_currency"] for r in twd) or bool(ratios)
    unknown_errors = [r["item"] for r in rows if r["kind"] == tp.ERROR and not
                      (str(r["reason"]).startswith("http_") or r["reason"] in KNOWN_ERROR_REASONS)]
    unmapped_status = [r["item"] for r in rows if r["status"] not in (None, 200) and r["reason"] != f"http_{r['status']}"]
    aborted = [f"{x['dir']}: {x['aborted']}" for x in rounds if x.get("aborted")]
    leaks = {}
    if scan_dirs:
        for x in rounds:
            leaks.update({f"{x['dir']}/{k}": v for k, v in check_output(x["dir"], token).items()})
    rejected = Counter(reason for r in rows for reason, n in r["rejected"].items() for _ in range(n))
    checks = {
        "api_parsing": check(not malformed_200 and not fmt,
                             f"HTTP 200 responses not parseable: {malformed_200}; offers with unexpected field formats: {dict(fmt)}"),
        "round_trip": check(not no_return and not bad_order and not v3_one_way_param,
                            f"accepted offers without return: {len(no_return)}; return before departure: {len(bad_order)}; "
                            f"v3 requests without one_way=false: {len(v3_one_way_param)}; one-way offers rejected: {rejected.get('one_way', 0)}"),
        "departure_and_return_dates": check(not departed and not bad_order,
                                            f"accepted offers already departed: {len(departed)}; rejected as departed: "
                                            f"{rejected.get('departed', 0)}, expired: {rejected.get('expired', 0)}"),
        "currency": check(not wrong_cur and not chosen_cur and not bad_ratio,
                          f"non-TWD response currency: {wrong_cur}; non-TWD fares: {chosen_cur}; TWD/USD ratios outside "
                          f"{USD_RATIO_RANGE}: {bad_ratio}; ratios measured: {len(ratios)}",
                          not_verified=not currency_evidence and not wrong_cur and not chosen_cur),
        "wrong_month_rejection": check(not off_month, f"accepted offers outside the requested month: {len(off_month)}; "
                                                      f"wrong-month offers rejected: {rejected.get('wrong_month', 0)}"),
        "malformed_response_handling": check(not unknown_errors,
                                             f"errors with unexpected reasons: {unknown_errors}; malformed responses handled: "
                                             f"{sum(r['reason'] == 'malformed' for r in rows)}"),
        "api_error_handling": check(not unmapped_status and not aborted,
                                    f"non-200 statuses not mapped to an error: {unmapped_status}; rounds stopped early "
                                    f"(repeat them): {aborted}; statuses seen: {dict(Counter(str(r['status']) for r in rows))}"),
        "token_safety": check(not leaks, f"secret-scan findings: {leaks}" if leaks else
                              ("all result files clean" if scan_dirs else "scan runs after the round (see live output)")),
        "no_production_side_effects": check(True, "by construction: the tool has no AWS client, no database, Lambda, "
                                                  "SQS or email code (offline tests verify); only Travelpayouts was called"),
    }
    verdict = "PASS" if all(c["verdict"] == "pass" for c in checks.values()) else "FAIL"
    return {"verdict": verdict, "checks": checks}


def business(rows, cov):
    airport = [dict(c, item=r["item"]) for r in rows if r["api"] == "v3" and r["variant"] == "twd"
               for c in r["signals"]["airport_rejected_cheaper"]]
    all_rejected = [label((r.get("round", ""), r["item"])) for r in rows
                    if r["variant"] == "twd" and r["reason"] == "all_rejected"]
    union, v3_valid = cov["union_valid"], cov["v3_valid"]
    v3_share = v3_valid / union if union else 0
    v3_alone = union > 0 and not cov["v1_only"] and not airport
    fallback = bool(cov["v1_only"])
    conditions = []
    if fallback:
        conditions.append(f"keep v1 available as a fallback: {len(cov['v1_only'])} route/month(s) had a valid fare only from v1")
    if airport:
        conditions.append(f"review airport mapping (plan §6a): {len(airport)} cheaper v3 offer(s) rejected for airport")
    if cov["neither_pct"] and cov["neither_pct"] > 25:
        conditions.append(f"{cov['neither_pct']} % of route/months had no valid fare from either API (expected on thin routes; review)")
    if union == 0:
        verdict, why = "FAIL", "no valid fare from either API; nothing to judge"
    elif v3_share < V3_SHARE_FLOOR:
        verdict, why = "FAIL", f"v3 covers {round(v3_share * 100, 1)} % of the route/months that have any valid fare (< {int(V3_SHARE_FLOOR * 100)} %)"
    elif v3_alone and not conditions:
        verdict, why = "PASS", "v3 covers every route/month v1 covers, with no airport-mapping losses"
    else:
        verdict, why = "CONDITIONAL", "; ".join(conditions)
    return {"verdict": verdict, "reason": why,
            "metrics": {"v1_valid_pct": cov["v1_valid_pct"], "v3_valid_pct": cov["v3_valid_pct"],
                        "v3_share_of_covered_pct": round(v3_share * 100, 1) if union else None,
                        "both": len(cov["both"]), "v1_only": len(cov["v1_only"]), "v3_only": len(cov["v3_only"]),
                        "neither": len(cov["neither"]), "neither_pct": cov["neither_pct"],
                        "v1_missing_data_pct": pct_not_ok(cov["v1_kinds"]), "v3_missing_data_pct": pct_not_ok(cov["v3_kinds"])},
            "false_negative_risks": {"airport_rejected_cheaper": airport, "all_offers_rejected": all_rejected},
            "v3_alone_sufficient": v3_alone, "v1_fallback_advisable": fallback, "conditions": conditions,
            "note": "Relative v1/v3 prices are reported for review and never select the production API on their own. "
                    "The owner decides FARE_API."}


def pct_not_ok(kinds):
    n = sum(kinds.values())
    return round((n - kinds.get(tp.OK, 0)) / n * 100, 1) if n else None


def evaluate(rows, rounds, token=None, scan_dirs=True):
    cov = coverage(rows)
    return {"technical_compatibility": technical(rows, rounds, token, scan_dirs),
            "business_suitability": business(rows, cov), "coverage": cov, "price_differences": price_differences(rows)}


def summarize_rows(rows, comparisons):
    by = lambda api, variant: [r for r in rows if r["api"] == api and r["variant"] == variant]
    risks = Counter()
    airport_cases, rate_low, currencies = [], None, Counter()
    for r in rows:
        currencies[f"{r['api']}:{r['variant']}:{r['response_currency']}"] += 1
        for reason, n in r["rejected"].items():
            risks[f"rejected_{reason}"] += n
        if r["signals"]["false_positive_prevented"]:
            risks["false_positives_prevented"] += 1
        airport_cases += [dict(c, item=r["item"], api=r["api"]) for c in r["signals"]["airport_rejected_cheaper"]]
        for k, v in r["rate_limit"].items():
            if "remaining" in k.lower() and str(v).isdigit():
                rate_low = int(v) if rate_low is None else min(rate_low, int(v))
    kinds = {f"{api}:{variant}": dict(Counter(r["kind"] for r in by(api, variant)))
             for api in ("v1", "v3") for variant in ("twd", "market", "usd") if by(api, variant)}
    market = []
    for r in by("v3", "market"):
        base = next((b for b in by("v3", "twd") if b["item"] == r["item"]), None)
        market.append({"item": r["item"], "default_kind": base and base["kind"], "market_kind": r["kind"],
                       "default_price": base and base["chosen"] and base["chosen"]["price"],
                       "market_price": r["chosen"] and r["chosen"]["price"]})
    usd_ratio = []
    for r in by("v3", "usd"):
        base = next((b for b in by("v3", "twd") if b["item"] == r["item"]), None)
        if base and base["chosen"] and r["chosen"]:
            usd_ratio.append({"item": r["item"], "twd_per_usd": round(base["chosen"]["price"] / r["chosen"]["price"], 2)})
    cov = coverage(rows)
    return {"tool_version": TOOL_VERSION, "kinds": kinds, "risks": dict(sorted(risks.items())),
            "airport_rejected_cheaper": airport_cases,
            "coverage": cov, "availability": {"v1_only": cov["v1_only"], "v3_only": cov["v3_only"]},
            "price_gaps_over_10pct": [c["item"] for c in price_differences(rows)["cases"]],
            "response_currency": dict(currencies), "market_check": market, "usd_ratio": usd_ratio,
            "rate_limit_lowest_remaining": rate_low,
            "http_status": dict(Counter(str(r["status"]) for r in rows))}


def summary_markdown(s):
    cov = s["coverage"]
    lines = ["# B5 round summary", "", f"- Aborted: {s.get('aborted') or 'no'}",
             f"- Requests: {s.get('requests_sent')} sent of {s.get('requests_planned')} planned",
             f"- HTTP status: {s['http_status']}", f"- Result kinds: {s['kinds']}",
             f"- Coverage: v1 {cov['v1_valid']}/{cov['compared']}, v3 {cov['v3_valid']}/{cov['compared']}, "
             f"both {len(cov['both'])}, only v1 {cov['v1_only']}, only v3 {cov['v3_only']}, neither {cov['neither']}",
             f"- Price gaps over 10 % (review only): {s['price_gaps_over_10pct']}",
             f"- Response currency: {s['response_currency']}",
             f"- Market check: {s['market_check']}", f"- TWD per USD (sanity): {s['usd_ratio']}",
             f"- Lowest rate-limit remaining seen: {s['rate_limit_lowest_remaining']}"]
    if s.get("preliminary"):
        p = s["preliminary"]
        lines += [f"- Preliminary (this round only): technical {p['technical_compatibility']['verdict']}, "
                  f"business {p['business_suitability']['verdict']}"]
    lines += ["", "## Risk signals", ""]
    lines += [f"- {k}: {v}" for k, v in s["risks"].items()] or ["- none"]
    lines += ["", "## Cheaper offers rejected for airport mapping", ""]
    lines += [f"- {c}" for c in s["airport_rejected_cheaper"]] or ["- none"]
    return "\n".join(lines) + "\n"


def report_markdown(r):
    t, b, cov, pd = r["technical_compatibility"], r["business_suitability"], r["coverage"], r["price_differences"]
    lines = ["# B5 result", "", f"Rounds: {', '.join(x['dir'] for x in r['rounds'])}; requests: {r['requests']}", "",
             f"## A. Technical compatibility: {t['verdict']}", "", "| Check | Verdict | Detail |", "|---|---|---|"]
    lines += [f"| {k} | {v['verdict']} | {v['detail']} |" for k, v in t["checks"].items()]
    lines += ["", f"## B. Business suitability: {b['verdict']}", "", f"{b['reason']}", ""]
    lines += [f"- {k}: {v}" for k, v in b["metrics"].items()]
    lines += [f"- v3 alone sufficient: {b['v3_alone_sufficient']}", f"- v1 fallback advisable: {b['v1_fallback_advisable']}"]
    lines += [f"- condition: {c}" for c in b["conditions"]]
    lines += ["", f"_{b['note']}_", "", "## Coverage", "",
              f"- Only v1: {cov['v1_only']}", f"- Only v3: {cov['v3_only']}", f"- Neither: {cov['neither']}", "",
              f"## Price differences over {pd['threshold_pct']} % (review only, never a failure)", "",
              f"{pd['over_threshold']} of {pd['overlapping']} overlapping route/months.", ""]
    lines += [f"- {c['item']}: v1 {c['v1_price']} vs v3 {c['v3_price']} ({c['diff_pct']} %, lower: {c['lower']}); "
              f"dates v1 {c['v1_dates']} v3 {c['v3_dates']}; v3 airports {c['v3_airports']}; "
              f"round trip {c['both_round_trip']}; TWD {c['both_twd']}; v1 expires in {c['v1_freshness']['hours_left']} h"
              for c in pd["cases"]] or ["- none"]
    lines += ["", "## False-negative risks", "", f"- Airport mapping: {b['false_negative_risks']['airport_rejected_cheaper'] or 'none'}",
              f"- All offers rejected: {b['false_negative_risks']['all_offers_rejected'] or 'none'}"]
    return "\n".join(lines) + "\n"


# ---- commands -------------------------------------------------------------------------------

def cmd_preview(a, env, out):
    plan, catalog, now = load_plan(a.plan), config.load_catalog(), datetime.now(timezone.utc)
    reqs = build_requests(plan, now, catalog, usd=a.usd)
    lim = plan["limits"]
    out(f"B5 preview (no network). Plan v{plan['plan_version']}, current month {months.current_month(now)} (Asia/Taipei)")
    out(f"Work items: {len({r.item_key for r in reqs if r.variant == 'twd'})}; requests per round: {len(reqs)} "
        f"(v1 {sum(r.api == 'v1' for r in reqs)}, v3 {sum(r.api == 'v3' for r in reqs)}); "
        f"cap {lim['default_max_requests']} (absolute {lim['absolute_max_requests']})")
    out(f"Estimated duration at {lim['default_interval_seconds']} s spacing: about "
        f"{round(len(reqs) * (lim['default_interval_seconds'] + 0.5) / 60, 1)} minutes")
    out(f"Token in {TOKEN_ENV}: {'set (never shown)' if env.get(TOKEN_ENV) else 'not set'}; "
        f"{APPROVAL_ENV}: {'yes' if env.get(APPROVAL_ENV) == 'yes' else 'not set'}")
    for r in reqs:
        out(f"  {r.seq:3d} {r.api} {r.variant:6s} {r.item_key:18s} {request_url(r, market=plan['market_check']['market'])}")
    return 0


def cmd_live(a, env, out, transport_factory=None):
    if not a.confirm_live:
        raise Refused("Live requests need --confirm-live. Run `preview` first; nothing was sent.")
    if env.get(APPROVAL_ENV) != "yes":
        raise Refused(f"Live requests need {APPROVAL_ENV}=yes in this session (owner approval). Nothing was sent.")
    token = read_token(env)
    plan, catalog, now = load_plan(a.plan), config.load_catalog(), datetime.now(timezone.utc)
    lim = plan["limits"]
    if not 1 <= a.max_requests <= lim["absolute_max_requests"]:
        raise Refused(f"--max-requests must be 1..{lim['absolute_max_requests']}")
    if a.interval < lim["min_interval_seconds"]:
        raise Refused(f"--interval must be at least {lim['min_interval_seconds']} s")
    reqs = build_requests(plan, now, catalog, usd=a.usd and a.round == 1)
    if len(reqs) > a.max_requests:
        raise Refused(f"The round needs {len(reqs)} requests, above --max-requests {a.max_requests}. Nothing was sent.")
    out_dir = Path(a.out or f"b5-results/round{a.round}-{now.strftime('%Y%m%dT%H%M%SZ')}")
    if out_dir.exists() and any(out_dir.iterdir()):
        raise Refused(f"{out_dir} is not empty; choose a new --out folder.")
    transport = (transport_factory or LiveTransport)(a.max_requests, a.interval)
    out(f"B5 live round {a.round}: {len(reqs)} requests, {a.interval} s apart, token fingerprint "
        f"{token_fingerprint(token)}, output {out_dir}")
    summary = run_round(reqs, token=token, transport=transport, catalog=catalog, now=now, out_dir=out_dir,
                        market=plan["market_check"]["market"], log=out)
    write_json(out_dir / "run.json", {"tool_version": TOOL_VERSION, "round": a.round, "started_at": now.isoformat(),
                                      "plan": plan, "token_fingerprint": token_fingerprint(token),
                                      "interval_seconds": a.interval, "max_requests": a.max_requests}, token)
    findings = check_output(out_dir, token)
    out(f"Done: {summary['requests_sent']} requests sent; aborted: {summary['aborted'] or 'no'}")
    if findings:
        out(f"SECRET CHECK FAILED: {findings}. Delete {out_dir} and report this.")
        return 3
    out(f"Secret check: clean. Files: {', '.join(sorted(p.name for p in out_dir.iterdir()))}")
    return 1 if summary["aborted"] else 0


def cmd_check(a, env, out):
    findings = check_output(a.dir, (env.get(TOKEN_ENV) or "").strip() or None)
    if findings:
        for f, what in findings.items():
            out(f"FOUND in {f}: {', '.join(what)}")
        return 3
    out(f"Clean: no token{'' if env.get(TOKEN_ENV) else ' (TP_TOKEN not set; token check skipped)'}, "
        "email, JWT, AWS key, token header or marker found.")
    return 0


def cmd_summarize(a, env, out):
    rows, rounds = [], []
    for d in a.dirs:
        d = Path(d)
        summary = json.loads((d / "summary.json").read_text(encoding="utf-8"))
        rounds.append({"dir": str(d), "aborted": summary.get("aborted")})
        rows += [dict(json.loads(line), round=d.name)
                 for line in (d / "requests.jsonl").read_text(encoding="utf-8").splitlines() if line]
    token = (env.get(TOKEN_ENV) or "").strip() or None
    report = dict(evaluate(rows, rounds, token), rounds=rounds, requests=len(rows))
    if a.out:
        out_path = Path(a.out)
        write_json(out_path, report, token)
        out_path.with_suffix(".md").write_text(sanitize(report_markdown(report), token), encoding="utf-8")
    t, b = report["technical_compatibility"], report["business_suitability"]
    out(f"A. Technical compatibility: {t['verdict']}")
    for k, v in t["checks"].items():
        out(f"   {v['verdict']:13s} {k}")
    out(f"B. Business suitability: {b['verdict']} - {b['reason']}")
    out(f"   v3 alone sufficient: {b['v3_alone_sufficient']}; v1 fallback advisable: {b['v1_fallback_advisable']}")
    out(f"   Price gaps over 10 % (review only): {report['price_differences']['over_threshold']} of "
        f"{report['price_differences']['overlapping']}")
    if a.out:
        out(f"Written: {a.out} and {Path(a.out).with_suffix('.md')}")
    return 0


def parser():
    p = argparse.ArgumentParser(prog="b5_compare", description="B5 Travelpayouts v1/v3 comparison (local, read-only).")
    sub = p.add_subparsers(dest="cmd", required=True)
    pv = sub.add_parser("preview", help="show the plan and every request; no network")
    pv.add_argument("--plan")
    pv.add_argument("--usd", action="store_true")
    lv = sub.add_parser("live", help="the approved live test (needs --confirm-live and B5_LIVE_APPROVED=yes)")
    lv.add_argument("--round", type=int, choices=(1, 2, 3), required=True)
    lv.add_argument("--confirm-live", action="store_true")
    lv.add_argument("--plan")
    lv.add_argument("--out")
    lv.add_argument("--max-requests", type=int, default=200)
    lv.add_argument("--interval", type=float, default=2.0)
    lv.add_argument("--usd", action="store_true", help="round 1 only: add v3 USD requests (currency sanity check)")
    ck = sub.add_parser("check-output", help="scan a results folder for secrets and personal data")
    ck.add_argument("dir")
    sm = sub.add_parser("summarize", help="combine rounds and evaluate the proposed acceptance criteria")
    sm.add_argument("dirs", nargs="+")
    sm.add_argument("--out")
    return p


def main(argv=None, env=None, out=print, transport_factory=None):
    a = parser().parse_args(argv)
    env = os.environ if env is None else env
    try:
        if a.cmd == "preview":
            return cmd_preview(a, env, out)
        if a.cmd == "live":
            return cmd_live(a, env, out, transport_factory)
        if a.cmd == "check-output":
            return cmd_check(a, env, out)
        return cmd_summarize(a, env, out)
    except Refused as r:
        out(f"REFUSED: {r.message}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
