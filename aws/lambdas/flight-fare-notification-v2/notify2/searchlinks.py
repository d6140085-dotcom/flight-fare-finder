"""Multi-platform flight search links (docs/m1.4-multi-platform-search-links.md §4-§6, §8.5).

Pure functions: no network, no AWS, no user data. Inputs are catalog codes, the fare's dates,
today's date (Asia/Taipei) and the deploy-time Aviasales marker. Every output is an https link on
the platform's own host with an allow-listed set of query keys.

- An itinerary link is built only when the platform's format has been verified for both codes
  (verified_codes) and both dates are valid; otherwise that platform gets its general search page,
  labelled 「一般搜尋頁（未帶入行程，請自行輸入並確認航線與日期）」. A link is never one-way.
- URL formats and query keys are fixed in code (spec §4.2); search-platforms.json only switches
  platforms on/off, orders them and lists verified codes. It never holds a marker or ID.
- Travelpayouts' written conditions (2026-11-07): a clearly labelled Aviasales affiliate link must
  stay available wherever Data API reference fares are shown. Without a valid deploy-time marker
  NO links are built (LinkSet.compliant is False) and the caller must not send the email
  (fail closed); a plain, non-affiliate Aviasales link is never produced.
"""
import calendar
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE_RE = re.compile(r"^[A-Z]{3}$")
# Travelpayouts Partner ID ("marker"): numeric in the docs' example (1234567). SubIDs (".subid")
# are documented but not used, so a dot is rejected; anything unexpected disables the affiliate link.
MARKER_RE = re.compile(r"^[0-9]{3,12}$")
WINDOW_MONTHS = 13                              # spec §4.1
ITINERARY, FALLBACK = "itinerary", "fallback"
FALLBACK_NOTICE = "未帶入行程，請自行輸入並確認航線與日期"   # every general-search-page link

HOSTS = {"skyscanner": "www.skyscanner.com.tw", "tripcom": "tw.trip.com",
         "aviasales": "www.aviasales.com", "google": "www.google.com"}
ALLOWED_QUERY = {
    "skyscanner": {"adultsv2", "cabinclass", "market", "locale", "currency"},
    "tripcom": {"dcity", "acity", "ddate", "rdate", "triptype", "class", "quantity", "locale", "curr"},
    "aviasales": {"marker"},
    "google": {"hl"},
}
FALLBACK_URLS = {"skyscanner": "https://www.skyscanner.com.tw/", "tripcom": "https://tw.trip.com/flights/",
                 "aviasales": "https://www.aviasales.com/",
                 "google": "https://www.google.com/travel/flights?hl=zh-TW"}
AFFILIATE_KEYS = {"marker", "mediapartnerid", "irclickid", "associateid", "campaign_id", "allianceid", "sid",
                  "subid", "aff", "affiliate", "ref"}


class ConfigError(ValueError):
    """search-platforms.json or airports.json is unsafe; no links are built."""


@dataclass(frozen=True)
class Catalog:
    origins: frozenset
    destinations: frozenset


@dataclass(frozen=True)
class Link:
    platform: str
    label: str                # Traditional Chinese, ready to show
    url: str
    kind: str                 # itinerary | fallback
    role: str                 # primary | secondary | source | fallback_only
    affiliate: bool = False
    notice: str = None        # set on general-search-page (fallback) links


@dataclass(frozen=True)
class LinkSet:
    links: tuple = ()
    itinerary_valid: bool = False
    third_party_suppressed: bool = False
    reasons: tuple = field(default_factory=tuple)

    @property
    def has_affiliate(self):
        return any(link.affiliate for link in self.links)

    @property
    def compliant(self):
        """Travelpayouts condition 3: a labelled Aviasales affiliate link is present."""
        av = self.aviasales
        return bool(av and av.affiliate)

    @property
    def aviasales(self):
        return next((link for link in self.links if link.platform == "aviasales"), None)


# ---- configuration ----------------------------------------------------------------------------

def _config_file(name):
    bundled = HERE / "data" / name                     # inside the Lambda ZIP
    return bundled if bundled.exists() else HERE.parents[2] / "config" / name   # repo: aws/config


def load_catalog(path=None):
    raw = json.loads(Path(path or _config_file("airports.json")).read_text(encoding="utf-8"))
    origins = frozenset(o["code"] for o in raw["origins"])
    dests = frozenset(d["code"] for d in raw["destinations"])
    if not origins or not dests or not all(CODE_RE.match(c) for c in origins | dests):
        raise ConfigError("airports.json: invalid catalog")
    return Catalog(origins, dests)


def load_config(path=None):
    cfg = json.loads(Path(path or _config_file("search-platforms.json")).read_text(encoding="utf-8"))
    validate_config(cfg)
    return cfg


def _scalars(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _scalars(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _scalars(v)
    else:
        yield obj


def validate_config(cfg):
    """Spec §7.1 L12. Raises ConfigError; the packager and the tests run it, and so does the Lambda
    on start-up (a bad file disables the links section, never the email)."""
    plats = cfg.get("platforms") if isinstance(cfg, dict) else None
    if not isinstance(plats, list) or not plats:
        raise ConfigError("no platforms")
    ids = [p.get("id") for p in plats]
    if len(set(ids)) != len(ids) or set(ids) - set(HOSTS):
        raise ConfigError(f"unknown or duplicate platform ids: {ids}")
    orders = [p.get("order") for p in plats]
    if len(set(orders)) != len(orders) or not all(isinstance(o, int) for o in orders):
        raise ConfigError("order must be unique integers")
    for p in plats:
        pid = p["id"]
        if not isinstance(p.get("enabled"), bool) or not isinstance(p.get("label"), str) or not p["label"]:
            raise ConfigError(f"{pid}: enabled must be true/false and label must be set")
        for key in ("url", "template", "host", "params", "query"):
            if key in p:
                raise ConfigError(f"{pid}: `{key}` is not configurable; URL formats are fixed in code (spec §4.2)")
        aff = p.get("affiliate") or {}
        if pid != "aviasales" and aff.get("enabled"):
            raise ConfigError(f"{pid}: affiliate.enabled needs an issued, documented partner ID; none exists")
        if pid == "aviasales" and aff.get("enabled") and (aff.get("param") != "marker" or aff.get("env") != "TP_MARKER"):
            raise ConfigError("aviasales affiliate: parameter must be the documented `marker`, read from TP_MARKER")
        for code, override in (p.get("code_overrides") or {}).items():
            if not CODE_RE.match(code) or not re.fullmatch(r"[a-z]{3,5}", str(override)):
                raise ConfigError(f"{pid}: invalid code override {code}->{override}")
        vc = (p.get("verified_codes") or {}).get("codes")
        if pid == "aviasales" and vc == "catalog":
            continue
        if not (isinstance(vc, list) and all(isinstance(c, str) and CODE_RE.match(c) for c in vc)):
            raise ConfigError(f"{pid}: verified_codes.codes must be a list of upper-case IATA codes")
    for value in _scalars({k: v for k, v in cfg.items() if k != "_comment"}):
        text = str(value)
        if ((isinstance(value, int) and not isinstance(value, bool) and value >= 1000)
                or re.search(r"\d{5,}", text) or re.search(r"marker=|token|secret|api[_-]?key|eyJ", text, re.I)):
            raise ConfigError("search-platforms.json must not contain a marker, ID, token or secret; "
                              "the marker is injected at deploy time (TP_MARKER)")
    av = next((p for p in plats if p["id"] == "aviasales"), None)
    if not (av and av["enabled"] and (av.get("affiliate") or {}).get("enabled") and av["affiliate"].get("required")):
        raise ConfigError("the labelled Aviasales affiliate link must be enabled and required: reference fares "
                          "come from the Aviasales Data API (Travelpayouts condition 3, spec §8.5)")
    return cfg


def valid_marker(marker):
    return isinstance(marker, str) and bool(MARKER_RE.fullmatch(marker))


# ---- dates ------------------------------------------------------------------------------------

def parse_day(value):
    """'YYYY-MM-DD' or an ISO date-time starting with it -> date; anything else -> None."""
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}($|T)", value):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def add_months(day, n):
    total = day.year * 12 + day.month - 1 + n
    y, m = divmod(total, 12)
    return date(y, m + 1, min(day.day, calendar.monthrange(y, m + 1)[1]))


def window_end(today):
    return add_months(today, WINDOW_MONTHS)


def date_problem(depart, ret, today):
    """None when the round trip can be linked, else the reason (spec §4.1)."""
    if depart is None or ret is None:
        return "missing_or_malformed_date"
    if depart < today:
        return "departure_in_the_past"
    if ret < depart:
        return "return_before_departure"
    if ret > window_end(today):
        return "outside_window"
    return None


def aviasales_year_safe(ret, today):
    """Aviasales' path has DDMM without a year. Spec §4.2 assumed the 13-month window keeps that
    unambiguous; it doesn't (12+ months wraps), so the itinerary link is built only when the
    return date is earlier than the same day next year."""
    return ret < add_months(today, 12)


# ---- per-platform itinerary URLs (spec §4.2; formats verified 2026-11-06) ----------------------

def _q(params):
    return urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def skyscanner_url(o, d, dep, ret):
    path = "/".join(urllib.parse.quote(x) for x in (o.lower(), d.lower(), f"{dep:%y%m%d}", f"{ret:%y%m%d}"))
    return f"https://www.skyscanner.com.tw/transport/flights/{path}/?" + _q(
        {"adultsv2": "1", "cabinclass": "economy", "market": "TW", "locale": "zh-TW", "currency": "TWD"})


def tripcom_url(o, d, dep, ret):
    return "https://tw.trip.com/flights/showfarefirst?" + _q({
        "dcity": o.lower(), "acity": d.lower(), "ddate": dep.isoformat(), "rdate": ret.isoformat(),
        "triptype": "rt", "class": "y", "quantity": "1", "locale": "zh-TW", "curr": "TWD"})


def aviasales_url(o, d, dep, ret, marker=None):
    path = urllib.parse.quote(f"{o}{dep:%d%m}{d}{ret:%d%m}1")
    return f"https://www.aviasales.com/search/{path}" + (f"?{_q({'marker': marker})}" if marker else "")


# ---- builder ----------------------------------------------------------------------------------

AVIASALES_TAGS = "票價來源・聯盟連結・英文介面"


def _label(pid, label, kind):
    if pid == "aviasales":
        return (f"在 Aviasales 搜尋此行程（{AVIASALES_TAGS}）" if kind == ITINERARY
                else f"前往 Aviasales 一般搜尋頁（{AVIASALES_TAGS}；{FALLBACK_NOTICE}）")
    if kind == ITINERARY:
        return f"在 {label} 搜尋此行程"
    return f"前往 {label} 一般搜尋頁（{FALLBACK_NOTICE}）"


def build(origin, destination, depart_date, return_date, today, *, catalog, config, marker=None):
    """LinkSet for one round-trip reference fare. Bad input never raises: unsupported codes and a
    missing/invalid marker give an empty, non-compliant LinkSet; bad dates give general search
    pages (spec §6)."""
    if not (isinstance(origin, str) and isinstance(destination, str) and CODE_RE.match(origin)
            and CODE_RE.match(destination) and origin in catalog.origins and destination in catalog.destinations):
        return LinkSet(reasons=("unsupported_code",))
    dep, ret = parse_day(depart_date), parse_day(return_date)
    problem = date_problem(dep, ret, today)
    if not valid_marker(marker):
        # fail closed: no affiliate link -> no links at all (never a plain Aviasales link)
        return LinkSet((), problem is None, True, tuple(r for r in (problem, "no_valid_aviasales_marker") if r))
    links = []
    for p in sorted(config["platforms"], key=lambda x: x["order"]):
        pid, label, role = p["id"], p["label"], p.get("role", "")
        if not p["enabled"]:
            continue
        vc = p["verified_codes"]["codes"]
        verified = vc == "catalog" or (origin in vc and destination in vc)
        ov = p.get("code_overrides") or {}
        o, d = ov.get(origin, origin), ov.get(destination, destination)
        itinerary = problem is None and verified
        if pid == "aviasales":
            if not (p.get("affiliate") or {}).get("enabled"):
                continue                               # validate_config forbids this; never plain
            if itinerary and aviasales_year_safe(ret, today):
                links.append(Link(pid, _label(pid, label, ITINERARY), aviasales_url(o, d, dep, ret, marker),
                                  ITINERARY, role, True))
            else:
                links.append(Link(pid, _label(pid, label, FALLBACK), FALLBACK_URLS[pid] + "?" + _q({"marker": marker}),
                                  FALLBACK, role, True, FALLBACK_NOTICE))
        elif itinerary and pid in ("skyscanner", "tripcom"):
            url = (skyscanner_url if pid == "skyscanner" else tripcom_url)(o, d, dep, ret)
            links.append(Link(pid, _label(pid, label, ITINERARY), url, ITINERARY, role))
        else:                                          # Google is always a general search page
            links.append(Link(pid, _label(pid, label, FALLBACK), FALLBACK_URLS[pid], FALLBACK, role, False,
                              FALLBACK_NOTICE))
    if not any(link.platform == "aviasales" and link.affiliate for link in links):
        return LinkSet((), problem is None, True, tuple(r for r in (problem, "aviasales_affiliate_disabled") if r))
    for link in links:
        check_url(link, marker)
    return LinkSet(tuple(links), problem is None, False, tuple(r for r in (problem,) if r))


def check_url(link, marker=None):
    """Defence in depth (spec §8.1, L08-L10): https on the platform's host, allow-listed query keys
    only, at most one marker and only on Aviasales, nothing that looks like personal data."""
    u = urllib.parse.urlsplit(link.url)
    if u.scheme != "https" or u.netloc != HOSTS[link.platform] or u.fragment or u.username or u.password or u.port:
        raise ValueError(f"unsafe URL for {link.platform}")
    pairs = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
    keys = [k for k, _ in pairs]
    if set(keys) - ALLOWED_QUERY[link.platform] or len(keys) != len(set(keys)):
        raise ValueError(f"unexpected query keys for {link.platform}")
    markers = [v for k, v in pairs if k == "marker"]
    if link.platform != "aviasales" and {k.lower() for k in keys} & AFFILIATE_KEYS:
        raise ValueError(f"affiliate parameter on {link.platform}")
    if link.platform == "aviasales" and (markers != ([marker] if link.affiliate else [])):
        raise ValueError("aviasales marker must appear exactly once, and only on the affiliate link")
    if "@" in link.url or "eyJ" in link.url or "%40" in link.url:
        raise ValueError("a link must never carry an address or token")
