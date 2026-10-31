"""SQS -> dedup -> Resend -> notification_history. Logs no secrets/emails."""
import datetime as dt, hashlib, html, json, os, re, urllib.error, urllib.request
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key

UA = "flight-notifier/1.0"  # Cloudflare blocks urllib's UA
CITY = {"TPE": "台北", "TYO": "東京", "SEL": "首爾"}
_ssm = boto3.client("ssm")
_hist = boto3.resource("dynamodb").Table("notification_history")
_cfg = None

def resend_cfg():
    global _cfg
    if _cfg is None:
        raw = _ssm.get_parameter(Name=os.environ["RESEND_PARAM"], WithDecryption=True)["Parameter"]["Value"].strip()
        try:
            v = json.loads(raw)
        except ValueError:
            v = {"api_key": raw}
        _cfg = {"key": v["api_key"], "from": v.get("from") or "onboarding@resend.dev"}
    return _cfg

def should_send(last, price, now):
    if not last:
        return True
    env = lambda k, v: Decimal(os.environ.get(k, v))
    if now - dt.datetime.fromisoformat(last["sent_at"]) >= dt.timedelta(hours=float(env("NOTIFY_FLOOR_HOURS", "24"))):
        return True
    prev = Decimal(str(last["price"]))
    return price <= prev * (1 - env("REALERT_PCT", "20") / 100) or prev - price >= env("REALERT_ABS_TWD", "2000")

def booking_url(m):
    (o, d), f = m["route"].split("-"), m["cheapest"]
    dm = lambda s: f"{s[8:10]}{s[5:7]}" if s else ""
    return f"https://www.aviasales.com/search/{o}{dm(f.get('depart_date'))}{d}{dm(f.get('return_date'))}"

def render(m):
    o, d = (CITY.get(c, c) for c in m["route"].split("-"))
    f, usd = m["cheapest"], m.get("cheapest_usd")
    nt = f"NT${f['price']:,}"
    subject = f"✈️ {o} → {d} 降價通知！{nt} 已達標"
    day = lambda k: (f.get(k) or "")[:10]
    lines = [f"{o} → {d} 最低票價 {nt}" + (f"（約 US${usd['price']:,}）" if usd else ""),
             f"你的目標價：NT${m['target_price']:,}", f"去程 {day('depart_date')} 回程 {day('return_date')} {f.get('airline') or ''}"]
    url = booking_url(m)
    body = "".join(f"<p>{html.escape(x)}</p>" for x in lines)
    page = f'<h2>{html.escape(subject)}</h2>{body}<p><a href="{html.escape(url)}">立即訂購</a></p>'
    return subject, page, "\n".join(lines + [f"立即訂購：{url}"])

def send(cfg, to, subject, page, text):
    req = urllib.request.Request("https://api.resend.com/emails", method="POST",
        data=json.dumps({"from": cfg["from"], "to": [to], "subject": subject, "html": page, "text": text}).encode(),
        headers={"Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json", "User-Agent": UA})
    try:
        return urllib.request.urlopen(req, timeout=10).status, ""
    except urllib.error.HTTPError as e:  # mask addresses
        return e.code, re.sub(r"[^\s\"'<>]+@[^\s\"'<>]+", "<email>", e.read().decode(errors="replace"))[:300]

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
