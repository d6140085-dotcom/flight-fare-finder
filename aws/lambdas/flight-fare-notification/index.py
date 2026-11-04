"""SQS -> dedup -> Resend. Logs no secrets/emails."""
import datetime as dt, hashlib, html, json, os, re, urllib.error, urllib.request
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

_ssm = boto3.client("ssm")
_hist = boto3.resource("dynamodb").Table("notification_history")
CITY, AIRLINES = (json.loads(os.environ.get(k) or "{}") for k in ("CITIES", "AIRLINES"))
_cfg = None
def resend_cfg():
    global _cfg
    if _cfg is None:
        raw = _ssm.get_parameter(Name=os.environ["RESEND_PARAM"], WithDecryption=True)["Parameter"]["Value"].strip()
        v = json.loads(raw) if raw.startswith("{") else {"api_key": raw}
        _cfg = {"key": v["api_key"], "from": v.get("from") or "onboarding@resend.dev"}
    return _cfg
def should_send(last, price, now):
    env = lambda k, v: Decimal(os.environ.get(k, v))
    prev = Decimal(str(last["price"])) if last else 0
    return not last or (now - dt.datetime.fromisoformat(last["sent_at"]) >= dt.timedelta(hours=float(env("NOTIFY_FLOOR_HOURS", "24")))
            or price <= prev * (1 - env("REALERT_PCT", "20") / 100) or prev - price >= env("REALERT_ABS_TWD", "2000"))
def booking_url(m):
    # + adults (required)
    (o, d), f = m["route"].split("-"), m["cheapest"]
    dm = lambda s: f"{s[8:10]}{s[5:7]}" if s else ""
    return f"https://www.aviasales.com/search/{o}{dm(f.get('depart_date'))}{d}{dm(f.get('return_date'))}1"
def render(m):
    o, d = (CITY.get(c, c) for c in m["route"].split("-"))
    f, usd = m["cheapest"], m.get("cheapest_usd")
    nt = f"NT${f['price']:,}"
    subject = f"✈️ {o} → {d} 降價通知！{nt} 已達標"
    day = lambda k: (f.get(k) or "")[:10]
    al = f.get("airline")
    lines = [f"{o} → {d} 最低票價 {nt}" + (f"（約 US${usd['price']:,}）" if usd else ""),
             f"你的目標價：NT${m['target_price']:,}", f"去程 {day('depart_date')}  回程 {day('return_date')}"]
    if al:
        lines.append("航空公司：" + (f"{AIRLINES[al]}（{al}）" if al in AIRLINES else al))
    url = booking_url(m)
    body = "".join(f"<p>{html.escape(x)}</p>" for x in lines)
    page = f'<h2>{html.escape(subject)}</h2>{body}<p><a href="{html.escape(url)}">立即訂購</a></p>'
    return subject, page, "\n".join(lines + ["立即訂購：" + url])
def send(cfg, to, subject, page, text):
    req = urllib.request.Request("https://api.resend.com/emails", method="POST",
        data=json.dumps({"from": cfg["from"], "to": [to], "subject": subject, "html": page, "text": text}).encode(),
        headers={"Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json", "User-Agent": "flight-notifier/1.0"})
    try:
        return urllib.request.urlopen(req, timeout=10).status, ""
    except urllib.error.HTTPError as e:
        return e.code, re.sub(r"\S+@\S+", "<email>", e.read().decode(errors="replace"))[:300]
def process(m, now):
    email, route = m["email"], m["route"]
    pk, tag = f"{email}#{route}", f"{route} sub:{hashlib.sha256(email.encode()).hexdigest()[:8]}"
    price = Decimal(str(m["cheapest"]["price"]))
    last = (_hist.query(KeyConditionExpression=Key("pk").eq(pk), ScanIndexForward=False, Limit=1).get("Items") or [None])[0]
    if not should_send(last, price, now):
        return print(f"{tag}: skipped (deduped)")
    status, err = send(resend_cfg(), email, *render(m))
    if 200 <= status < 300:
        _hist.put_item(Item={"pk": pk, "sent_at": now.isoformat(timespec="milliseconds"), "email": email, "route": route, "price": price})
        print(f"{tag}: sent {price}")
    elif status == 429 or status >= 500:
        raise RuntimeError(f"Resend {status} {err}")
    else:
        print(f"{tag}: dropped, Resend {status} {err}")
def handler(event, context):
    failures = []
    for rec in event.get("Records", []):
        try:
            process(json.loads(rec["body"]), dt.datetime.now(dt.timezone.utc))
        except Exception as e:
            print(f"retry {rec['messageId']}: {e!r}")
            failures.append({"itemIdentifier": rec["messageId"]})
    return {"batchItemFailures": failures}
