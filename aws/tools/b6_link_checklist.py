"""B6-1: local-only generator of the manual provider-link verification checklist
(docs/m1.4-multi-platform-search-links.md §7.3; docs/m1.4-b6-status.md).

Prints a Markdown checklist of the links a person should open by hand to verify each platform's
route, dates, round trip, language, currency and airport/city naming. It builds the URLs with the
same code the app uses (notify2/searchlinks.py) and prints them; it never opens them.

Safety, by construction:
- No network: imports only the standard library and the pure link builder; nothing is fetched.
- No affiliate clicks: Aviasales URLs are printed WITHOUT any `marker` (the builder runs with the
  obvious test value 000000, which is then removed). Attribution is never checked or claimed
  here; it stays "NOT VERIFIED" until the B6-3 dashboard test.
- Never reads TP_MARKER, TP_TOKEN or any other environment variable, credential or user data.
- Writes only to stdout or the --out file you name.

Usage:
  python3 aws/tools/b6_link_checklist.py [--today 2026-11-23] [--months 1,6,12]
      [--origins TPE,KHH,RMQ] [--destinations TYO,OSA,...] [--out checklist.md]

--today defaults to today's date in Asia/Taipei; pass it for reproducible output.
Each month offset is counted from --today's month (1 = next month); the sample fare departs on the
20th of that month and returns 7 days later.
"""
import argparse
import sys
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

AWS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AWS / "lambdas" / "flight-fare-notification-v2"))

from notify2 import months, searchlinks as sl  # noqa: E402

TEST_MARKER = "000000"   # obvious test value; removed from every printed URL
# Representative destinations (the B5 sample plus the verified ones) until subscription counts
# exist to pick "the 10 most-subscribed" (spec §7.3).
DEFAULT_DESTINATIONS = ["TYO", "OSA", "SEL", "OKA", "HKG", "BKK", "SIN", "LON", "NYC", "SYD"]
DEFAULT_MONTHS = [1, 6, 12]
CHECKS = "[ ] 航線（出發地與目的地名稱）  [ ] 去程日期  [ ] 回程日期（來回）  [ ] 繁體中文  [ ] TWD  [ ] 機場／城市正確"
# Aviasales: English UI and no TWD parameter by design (spec §4.2), so those two aren't checked.
AVIASALES_CHECKS = "[ ] 航線（出發地與目的地）  [ ] 去程日期  [ ] 回程日期（來回）  [ ] 1 位成人・經濟艙  [ ] 機場／城市正確"


def strip_marker(url):
    """The URL without any `marker` query parameter (and without an empty `?`)."""
    u = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True) if k != "marker"]
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(q), ""))


def verified_on(cfg, pid):
    p = next(p for p in cfg["platforms"] if p["id"] == pid)
    vc = p.get("verified_codes") or {}
    return vc.get("codes"), vc.get("verified_on"), p.get("enabled")


def status_for(cfg, pid, origin, destination, kind):
    codes, when, enabled = verified_on(cfg, pid)
    if not enabled:
        return "DISABLED in search-platforms.json"
    if pid == "google":
        return "GENERAL PAGE BY DESIGN (no documented deep link)"
    if pid == "aviasales":
        base = "codes: Aviasales catalog; format browser-verified for TPE-TYO only (2026-11-06)"
        return base if kind == sl.ITINERARY else base + "; general page for this date (no year in the link)"
    if codes != "catalog" and origin in codes and destination in codes:
        return f"VERIFIED codes ({when}, browser)" if kind == sl.ITINERARY else "VERIFIED codes; general page for this date"
    return "UNVERIFIED — the app shows the general page; candidate itinerary URL below is for checking only"


def candidate(pid, origin, destination, dep, ret):
    """The itinerary URL a verifier would check for an unverified code (never used by the app)."""
    if pid == "skyscanner":
        return sl.skyscanner_url(origin, destination, dep, ret)
    if pid == "tripcom":
        return sl.tripcom_url(origin, destination, dep, ret)
    return None


def sample_dates(today, offset):
    first = sl.add_months(date(today.year, today.month, 1), offset)
    dep = date(first.year, first.month, 20)
    return dep, dep + timedelta(days=7)


def build_checklist(today, *, origins, destinations, month_offsets, cfg=None, catalog=None):
    cfg = cfg or sl.load_config()
    catalog = catalog or sl.load_catalog()
    out = [
        "# B6 manual provider-link verification checklist",
        "",
        f"Generated offline for today = {today.isoformat()} (Asia/Taipei). Nothing was opened or fetched.",
        "",
        "- **Affiliate attribution: NOT VERIFIED.** Aviasales URLs below carry **no marker**; opening them",
        "  credits nobody. Attribution is a separate B6-3 dashboard check.",
        "- **VERIFIED codes** = both codes are in that platform's `verified_codes` (browser check, date shown).",
        "  **UNVERIFIED** = the app currently shows that platform's general search page; the candidate",
        "  itinerary URL is printed only so a person can check it. Record results in `search-platforms.json`.",
        "- Check each link by hand in a normal browser. Don't automate it (bot checks, provider terms).",
        "",
    ]
    for offset in month_offsets:
        dep, ret = sample_dates(today, offset)
        month = f"{dep:%Y-%m}"
        out += [f"## Departure month {month} (offset +{offset}): sample fare {dep.isoformat()} → {ret.isoformat()}", ""]
        for o in origins:
            for d in destinations:
                ls = sl.build(o, d, dep.isoformat(), ret.isoformat(), today, catalog=catalog, config=cfg,
                              marker=TEST_MARKER, month=month)
                out.append(f"### {o} → {d} ({month})")
                if not ls.links:
                    out += [f"- No links: {', '.join(ls.reasons) or 'none'}", ""]
                    continue
                for link in ls.links:
                    st = status_for(cfg, link.platform, o, d, link.kind)
                    out.append(f"- **{link.platform}** — {link.kind} — {st}")
                    if link.platform == "aviasales":
                        out.append(f"  - App link (marker removed): {strip_marker(link.url)}")
                    else:
                        out.append(f"  - App link: {link.url}")
                    if link.kind == sl.FALLBACK and st.startswith("UNVERIFIED") and ls.itinerary_valid:
                        cand = candidate(link.platform, o, d, dep, ret)
                        if cand:
                            out.append(f"  - Candidate itinerary URL (UNVERIFIED, not used by the app): {cand}")
                    if link.platform == "aviasales":
                        out.append(f"  - {AVIASALES_CHECKS}")
                    elif link.platform != "google":
                        out.append(f"  - {CHECKS}")
                out.append("")
    return "\n".join(out) + "\n"


def parse_list(text):
    return [x.strip().upper() for x in text.split(",") if x.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--today", help="YYYY-MM-DD (default: today in Asia/Taipei)")
    ap.add_argument("--months", default=",".join(map(str, DEFAULT_MONTHS)))
    ap.add_argument("--origins", default="TPE,KHH,RMQ")
    ap.add_argument("--destinations", default=",".join(DEFAULT_DESTINATIONS))
    ap.add_argument("--out", help="write here instead of stdout")
    a = ap.parse_args(argv)
    today = date.fromisoformat(a.today) if a.today else months.today(datetime.now(months.TAIPEI))
    offsets = [int(x) for x in a.months.split(",") if x.strip()]
    if not offsets or any(not 1 <= x <= 12 for x in offsets):
        ap.error("--months must be offsets between 1 and 12")
    text = build_checklist(today, origins=parse_list(a.origins), destinations=parse_list(a.destinations),
                           month_offsets=offsets)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
