"""flight-parser: fetch one route's cheapest fare (TWD gates, USD best-effort) and
enqueue subscribers whose target_price >= it. Never logs the token or emails."""
import datetime, json, os, urllib.error, urllib.parse, urllib.request
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr

UA = "Mozilla/5.0 (compatible; flight-notifier/1.0)"
API = "https://api.travelpayouts.com/v1/prices/cheap"
_ssm = boto3.client("ssm")
_sqs = boto3.client("sqs")
_table = boto3.resource("dynamodb").Table("subscriptions")
_token = None


def get_token():
    # SSM value: bare token or {"token": ...}; cached per container.
    global _token
    if _token is None:
        name = os.environ.get("TP_PARAM", "/flight/travelpayouts")
        raw = _ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"].strip()
        try:
            v = json.loads(raw)
        except ValueError:
            v = raw
        _token = v.get("token") if isinstance(v, dict) else str(v)
        if not _token:
            raise RuntimeError("Travelpayouts parameter has no token")
    return _token


def next_month(today=None):
    d = today or datetime.datetime.now(datetime.timezone.utc).date()
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return f"{y:04d}-{m:02d}"


def fetch_cheapest(origin, dest, month, token, cur):
    q = urllib.parse.urlencode({"origin": origin, "destination": dest, "depart_date": month, "currency": cur})
    # Token in a header (not the URL) so it can't leak into errors or logs.
    req = urllib.request.Request(f"{API}?{q}", headers={"User-Agent": UA, "Accept": "application/json", "X-Access-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"travelpayouts {cur} HTTP {e.code} for {origin}-{dest}")
        return None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        print(f"travelpayouts {cur} unavailable for {origin}-{dest}: {type(e).__name__}")
        return None
    offers = (body.get("data") or {}).get(dest) if body.get("success") else None
    if not offers:
        return None
    b = min(offers.values(), key=lambda o: o["price"])
    return {"price": b["price"], "currency": cur.upper(), "airline": b.get("airline"),
            "depart_date": b.get("departure_at"), "return_date": b.get("return_at")}


def subscribers_for(route):
    kw = {"FilterExpression": Attr("route").eq(route)}
    while True:
        page = _table.scan(**kw)
        yield from page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            return
        kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def handler(event, context):
    o, d = event["origin"], event["destination"]
    route = event.get("route") or f"{o}-{d}"
    month = next_month()
    token = get_token()
    twd = fetch_cheapest(o, d, month, token, "twd")
    if not twd:
        print(f"{route} {month}: no TWD fare (empty/429), skipping")
        return {"ok": True, "route": route, "month": month, "matched": 0}
    usd = fetch_cheapest(o, d, month, token, "usd")  # may be None; never blocks
    print(f"{route} {month}: {twd['price']} TWD ({twd['airline']})" + (f", {usd['price']} USD" if usd else ", no USD"))
    cheapest = Decimal(str(twd["price"]))
    scanned = matched = 0
    for it in subscribers_for(route):
        scanned += 1
        tp = it.get("target_price")
        if tp is None or Decimal(str(tp)) < cheapest:
            continue
        body = {"email": it["email"], "route": route, "plan_name": it.get("plan_name"),
                "target_price": int(tp), "cheapest": twd}
        if usd:
            body["cheapest_usd"] = usd
        _sqs.send_message(QueueUrl=os.environ["FARE_QUEUE_URL"], MessageBody=json.dumps(body))
        matched += 1
    print(f"{route}: scanned {scanned}, enqueued {matched}")
    return {"ok": True, "route": route, "month": month, "cheapest_twd": twd["price"], "matched": matched}
