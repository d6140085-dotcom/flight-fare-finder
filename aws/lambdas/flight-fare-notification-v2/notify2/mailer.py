"""Email content and Resend delivery.

render() and booking_url() are the M1 functions unchanged (a parity test compares them with the
M1 Lambda): Phase 2B-1 changes deduplication only. The wording and search-link changes of the
search-links spec are Phase 2B-2. send() adds Resend's Idempotency-Key header, so a message that
is redelivered after a crash between "sent" and "recorded" doesn't send a second email within
Resend's 24-hour key window.
"""
import html
import json
import re
import urllib.error
import urllib.request


def booking_url(m):
    (o, d), f = m["route"].split("-"), m["cheapest"]
    dm = lambda s: f"{s[8:10]}{s[5:7]}" if s else ""
    return f"https://www.aviasales.com/search/{o}{dm(f.get('depart_date'))}{d}{dm(f.get('return_date'))}1"


def render(m, cities, airlines):
    o, d = (cities.get(c, c) for c in m["route"].split("-"))
    f, usd = m["cheapest"], m.get("cheapest_usd")
    nt = f"NT${f['price']:,}"
    subject = f"✈️ {o} → {d} 降價通知！{nt} 已達標"
    day = lambda k: (f.get(k) or "")[:10]
    al = f.get("airline")
    lines = [f"{o} → {d} 最低票價 {nt}" + (f"（約 US${usd['price']:,}）" if usd else ""),
             f"你的目標價：NT${m['target_price']:,}", f"去程 {day('depart_date')}  回程 {day('return_date')}"]
    if al:
        lines.append("航空公司：" + (f"{airlines[al]}（{al}）" if al in airlines else al))
    url = booking_url(m)
    body = "".join(f"<p>{html.escape(x)}</p>" for x in lines)
    page = f'<h2>{html.escape(subject)}</h2>{body}<p><a href="{html.escape(url)}">立即訂購</a></p>'
    return subject, page, "\n".join(lines + ["立即訂購：" + url])


SENT, RETRY, DROP, DUPLICATE = "sent", "retry", "drop", "duplicate"


def classify(status, body):
    """Resend outcome -> sent / duplicate (already sent under this key) / retry / drop."""
    if 200 <= status < 300:
        return SENT
    if status == 409 and "invalid_idempotent_request" in body:
        return DUPLICATE        # key already used: that earlier request was the send
    if status == 409 or status == 429 or status >= 500:
        return RETRY            # includes concurrent_idempotent_requests
    return DROP                 # e.g. 403/422, as M1


def send(cfg, to, subject, page, text, idempotency_key, opener=urllib.request.urlopen):
    """(status, outcome, error text with addresses masked)."""
    req = urllib.request.Request("https://api.resend.com/emails", method="POST",
        data=json.dumps({"from": cfg["from"], "to": [to], "subject": subject, "html": page, "text": text}).encode(),
        headers={"Authorization": f"Bearer {cfg['key']}", "Content-Type": "application/json",
                 "User-Agent": "flight-notifier/1.0", "Idempotency-Key": idempotency_key})
    try:
        status, body = opener(req, timeout=10).status, ""
    except urllib.error.HTTPError as e:
        status, body = e.code, re.sub(r"\S+@\S+", "<email>", e.read().decode(errors="replace"))[:300]
    except OSError as e:
        return None, RETRY, type(e).__name__
    return status, classify(status, body), body
