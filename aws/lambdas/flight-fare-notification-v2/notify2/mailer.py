"""Email content and Resend delivery.

render() and booking_url() are the M1 functions unchanged (a parity test compares them with the
M1 Lambda); they remain available as EMAIL_TEMPLATE=m1 for rollback. render_reference() is the
Phase 2B-2 email of the search-links spec (§2.1, §5): reference-fare wording with Aviasales named
as the source, 「約 NT$」, multi-platform search links, disclaimer and affiliate disclosure, and no
「立即訂購」. It refuses (AffiliateLinkUnavailable) unless a labelled Aviasales affiliate link is
present, because Travelpayouts' permission to show Data API fares requires one. send() adds Resend's Idempotency-Key header, so a message that
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


DISCLAIMER = ("參考票價來自 Aviasales（經 Travelpayouts 取得），已換算為新台幣，金額為約略值，可能已變動。"
              "各平台顯示的價格與可訂位情況可能不同，實際價格以訂購網站為準。")
FALLBACK_NOTICE = "未帶入行程，請自行輸入並確認航線與日期"   # searchlinks.FALLBACK_NOTICE (a test keeps them equal)
FALLBACK_LINE = ("標示「一般搜尋頁」的連結只會開啟該網站的搜尋頁，不會帶入此行程：請自行輸入並確認出發地、目的地與來回日期。"
                 "各平台的價格可能與 Aviasales 參考票價不同。")
AFFILIATE_DISCLOSURE = "本服務可能因你透過 Aviasales 連結訂購而獲得佣金。"
BUTTON = {"primary": "display:inline-block;padding:10px 16px;margin:4px 0;background:#0b57d0;color:#ffffff;"
                     "border-radius:6px;text-decoration:none;font-weight:bold",
          "secondary": "display:inline-block;padding:10px 16px;margin:4px 0;background:#ffffff;color:#0b57d0;"
                       "border:1px solid #0b57d0;border-radius:6px;text-decoration:none"}


class AffiliateLinkUnavailable(Exception):
    """The reference-fare email can't carry a labelled Aviasales affiliate link: don't send it.
    `reason` is a data-free code, safe to log."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _nt(value):
    return f"約 NT${value:,}"


def render_reference(m, cities, airlines, links):
    """Phase 2B-2 email. `links` is a searchlinks.LinkSet. Raises AffiliateLinkUnavailable unless
    it holds a labelled Aviasales affiliate link (Travelpayouts condition 3): Data API reference
    fares are never shown without one, and never next to a plain Aviasales link."""
    shown = list(links.links) if links is not None else []
    av = [link for link in shown if link.platform == "aviasales"]
    if not (len(av) == 1 and av[0].affiliate and "marker=" in av[0].url):
        raise AffiliateLinkUnavailable(",".join(getattr(links, "reasons", ()) or ()) or "no_affiliate_link")
    o, d = (cities.get(c, c) for c in m["route"].split("-"))
    f, usd, target = m["cheapest"], m.get("cheapest_usd"), m["target_price"]
    reached = "已低於你的目標價" if f["price"] < target else "已達你的目標價"
    subject = f"✈️ {o} → {d} 參考票價 {_nt(f['price'])}（{reached}）"
    day = lambda k: (f.get(k) or "")[:10] or "未提供"
    al = f.get("airline")
    lines = [f"參考票價（來源：Aviasales）：{_nt(f['price'])}" + (f"（約 US${usd['price']:,}）" if usd else ""),
             f"你的目標價：NT${target:,}", f"去程 {day('depart_date')}  回程 {day('return_date')}（來回）"]
    if al:
        lines.append("航空公司：" + (f"{airlines[al]}（{al}）" if al in airlines else al))
    if any(link.kind != "itinerary" for link in shown):
        lines.append(FALLBACK_LINE)
    notes = [DISCLAIMER, AFFILIATE_DISCLOSURE]
    esc = html.escape
    parts = [f"<h2>{esc(subject)}</h2>"] + [f"<p>{esc(x)}</p>" for x in lines]
    for link in shown:
        style = BUTTON.get(link.role) if link.kind == "itinerary" else None
        attrs = f' href="{esc(link.url)}" target="_blank" rel="noopener noreferrer"'
        parts.append(f'<p><a{attrs} style="{style}">{esc(link.label)}</a></p>' if style
                     else f"<p><a{attrs}>{esc(link.label)}</a></p>")
    parts += [f'<p style="color:#555555;font-size:12px">{esc(x)}</p>' for x in notes]
    text = lines + [""] + [f"{link.label}：{link.url}" for link in shown] + [""] + notes
    return subject, "".join(parts), "\n".join(text)


SENT, RETRY, UNKNOWN, DROP, DUPLICATE = "sent", "retry", "unknown", "drop", "duplicate"


def classify(status, body):
    """Resend outcome:
    sent       2xx
    duplicate  409 invalid_idempotent_request: this key was already used by an earlier request
    retry      429: rejected before processing, nothing was sent; retry later
    unknown    5xx, 409 concurrent_idempotent_requests, network/timeout: the email may have gone
               out; retry later with the same key (Resend replays it within 24 h)
    drop       other 4xx (e.g. 403/422), as M1"""
    if status is None:
        return UNKNOWN
    if 200 <= status < 300:
        return SENT
    if status == 409 and "invalid_idempotent_request" in body:
        return DUPLICATE
    if status == 429:
        return RETRY
    if status == 409 or status >= 500:
        return UNKNOWN
    return DROP


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
    except OSError as e:          # includes timeouts: the request may have reached Resend
        return None, UNKNOWN, type(e).__name__
    return status, classify(status, body), body
