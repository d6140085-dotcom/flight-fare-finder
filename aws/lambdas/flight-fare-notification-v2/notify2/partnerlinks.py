"""Travelpayouts Partner Links API: validation of partner links (B6-3A, offline foundation).

Travelpayouts Support (ticket #257588, 2026-11-27) recommends creating Aviasales affiliate links
with the Link Generator or the Partner Links API instead of appending the marker to Aviasales
search URLs, and says attribution can't be guaranteed for hand-built links. This module only
VALIDATES partner links. There is no HTTP client here: nothing calls the API, holds the API token
or creates links.

Three layers, all fail closed (any problem -> PartnerLinkError with a data-free reason):

  A  API response contract (`check_api_response`): only what the official documentation states
     (POST /links/v1/create): top-level `code`, `result.{marker, trs, shorten, links}`, and per
     link `url` (our input, echoed), `code` and `partner_url`. For the future producer; the
     notification Lambda never sees an API response.
  B  General URL safety (`check_url_safety`): https, no user info, port or fragment, printable
     ASCII only, bounded length, nothing that looks like an address or a token.
  C  Approved link format (`check_format`): the link must match one APPROVED format exactly:
     host, path, the exact set of query keys (each once) and a rule for every value, including
     a destination parameter that must decode to exactly the Aviasales link we built ourselves
     (so the destination is never taken from the API or from the message).

APPROVED_FORMATS is EMPTY. The dashboard-generated link the owner observed
(https://tp.media/r?campaign_id=…&marker=…&p=…&sub_id=…&trs=…&u=<destination>) is evidence of
what the Link Generator produces, not the API's documented response format: the documentation
shows no long-link example. A format is added only by a later reviewed change that cites a
controlled API dry run; until then every partner link is rejected and, in partner_api mode,
every reference-fare email is blocked.

Short links (opaque destination) are never accepted: their destination can't be checked without
following the redirect. A format must therefore carry exactly one destination parameter.

What this proves, and what it doesn't: a link that passes asks the redirect host for exactly our
destination. Whether the host really redirects there, and whether attribution works, can only be
established by Travelpayouts or by a controlled manual check; this module can't verify either.
"""
import re
import urllib.parse
from dataclasses import dataclass

MAX_URL_LENGTH = 2048
HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
TRS_RE = re.compile(r"^[0-9]{1,12}$")
FIXED_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
PRINTABLE = re.compile(r"^[\x21-\x7e]+$")        # no spaces, no control characters, ASCII only
# Personal data and secrets never belong in a link: an address (raw or encoded), a JWT-like
# token, or a backslash (authority confusion in some parsers).
FORBIDDEN = ("@", "%40", "eyJ", "\\", "%5c", "%5C")

RULES = ("marker", "trs", "destination", "fixed")


class PartnerLinkError(ValueError):
    """The partner link (or API response) can't be trusted. `reason` is data-free, safe to log."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class LinkFormat:
    """One approved partner-link format. `params` maps every query key to a rule:
    "marker" (equals TP_MARKER), "trs" (equals the configured project id), "destination" (decodes
    to exactly the expected Aviasales link) or ("fixed", value) (a recorded constant). Keys not
    listed are rejected."""
    name: str
    host: str
    path: str
    params: tuple      # ((key, rule), ...)
    evidence: str      # where this format was observed (a reviewed dry run), never a credential


# Empty by design (see the module docstring): partner_api mode fails closed until a reviewed
# change adds a format observed in a controlled API dry run.
APPROVED_FORMATS = ()


def validate_formats(formats):
    """Reject a format list that couldn't be enforced strictly. Raises PartnerLinkError."""
    if not isinstance(formats, tuple):
        raise PartnerLinkError("formats_not_a_tuple")
    names = set()
    for f in formats:
        if not isinstance(f, LinkFormat) or not f.name or f.name in names:
            raise PartnerLinkError("format_invalid")
        names.add(f.name)
        if not isinstance(f.host, str) or not HOST_RE.match(f.host):
            raise PartnerLinkError("format_host_invalid")
        if not isinstance(f.path, str) or not f.path.startswith("/") or "//" in f.path or not PRINTABLE.match(f.path):
            raise PartnerLinkError("format_path_invalid")
        if not isinstance(f.evidence, str) or not f.evidence.strip():
            raise PartnerLinkError("format_without_evidence")
        keys = [k for k, _ in f.params]
        if len(keys) != len(set(keys)) or not all(isinstance(k, str) and KEY_RE.match(k) for k in keys):
            raise PartnerLinkError("format_keys_invalid")
        kinds = [r if isinstance(r, str) else (r[0] if isinstance(r, tuple) and len(r) == 2 else None)
                 for _, r in f.params]
        if any(k not in RULES for k in kinds):
            raise PartnerLinkError("format_rule_invalid")
        for _, r in f.params:
            if isinstance(r, tuple) and (r[0] != "fixed" or not isinstance(r[1], str) or not FIXED_RE.match(r[1])):
                raise PartnerLinkError("format_rule_invalid")
            if r == "fixed":
                raise PartnerLinkError("format_rule_invalid")
        # an inspectable destination is mandatory (no short / opaque links) and the marker must
        # be checked; each appears exactly once
        if kinds.count("destination") != 1 or kinds.count("marker") != 1:
            raise PartnerLinkError("format_needs_one_destination_and_one_marker")
    return formats


def check_url_safety(url):
    """Layer B. Returns the parsed URL. Raises PartnerLinkError."""
    if not isinstance(url, str) or not url:
        raise PartnerLinkError("missing")
    if len(url) > MAX_URL_LENGTH:
        raise PartnerLinkError("too_long")
    if not PRINTABLE.match(url):
        raise PartnerLinkError("non_printable")
    if any(s in url for s in FORBIDDEN):
        raise PartnerLinkError("personal_data_or_token")
    try:
        u = urllib.parse.urlsplit(url)
        port = u.port
    except ValueError:
        raise PartnerLinkError("unparseable") from None
    if u.scheme != "https":
        raise PartnerLinkError("not_https")
    if u.username is not None or u.password is not None or "@" in u.netloc:
        raise PartnerLinkError("userinfo")
    if port is not None or ":" in u.netloc:
        raise PartnerLinkError("port")
    if u.fragment or "#" in url:
        raise PartnerLinkError("fragment")
    if not u.hostname or u.netloc != u.hostname:          # rejects upper case and odd authorities
        raise PartnerLinkError("host")
    return u


def check_format(url, *, expected_destination, marker, trs=None, formats=APPROVED_FORMATS):
    """Layers B and C. Returns the name of the matching approved format. Raises PartnerLinkError.

    `expected_destination` is the Aviasales link built by searchlinks (marker removed): the
    destination parameter must decode (once) to exactly this string."""
    u = check_url_safety(url)
    validate_formats(formats)
    if not formats:
        raise PartnerLinkError("no_approved_format")
    if not isinstance(expected_destination, str) or not expected_destination.startswith("https://www.aviasales.com/"):
        raise PartnerLinkError("no_expected_destination")
    if not isinstance(marker, str) or not marker:
        raise PartnerLinkError("no_marker")
    try:
        pairs = urllib.parse.parse_qsl(u.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise PartnerLinkError("query_malformed") from None
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise PartnerLinkError("duplicate_query_key")
    candidates = [f for f in formats if f.host == u.hostname and f.path == u.path]
    if not candidates:
        raise PartnerLinkError("unapproved_host_or_path")
    values = dict(pairs)
    last = "unapproved_format"
    for f in candidates:
        if set(keys) != {k for k, _ in f.params}:
            last = "unexpected_query_keys"
            continue
        try:
            for key, rule in f.params:
                v = values[key]
                if rule == "marker":
                    if v != marker:
                        raise PartnerLinkError("marker_mismatch")
                elif rule == "trs":
                    if not (isinstance(trs, str) and TRS_RE.match(trs)) or v != trs:
                        raise PartnerLinkError("trs_mismatch")
                elif rule == "destination":
                    # parse_qsl decoded the value exactly once: a double-encoded or altered
                    # destination doesn't match and is rejected
                    if v != expected_destination:
                        raise PartnerLinkError("destination_mismatch")
                elif v != rule[1]:
                    raise PartnerLinkError("fixed_value_mismatch")
        except PartnerLinkError as e:
            last = e.reason
            continue
        return f.name
    raise PartnerLinkError(last)


def expected_destination(direct_url, marker):
    """The destination a partner link must encode: the Aviasales link searchlinks built (route,
    dates, month checks and all), without its marker. Raises PartnerLinkError unless the marker
    appears exactly once with the expected value and nothing else is in the query."""
    u = urllib.parse.urlsplit(direct_url)
    if u.scheme != "https" or u.netloc != "www.aviasales.com" or u.fragment:
        raise PartnerLinkError("no_expected_destination")
    pairs = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
    if pairs != [("marker", marker)]:
        raise PartnerLinkError("no_expected_destination")
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, "", ""))


def check_api_response(body, requested, *, marker, trs):
    """Layer A, for the future producer (the fare check): the documented response of
    POST /links/v1/create for a request of `requested` URLs with shorten=false. Returns
    {requested url: partner_url}. Every partner_url must still pass check_format before use.

    Required (documented): top-level code "success"; result.marker and result.trs equal to ours;
    result.shorten false; result.links with exactly one entry per requested URL, matched by the
    echoed `url` (the documentation doesn't promise an order), each with code "success" and a
    non-empty partner_url string. A per-link "failed" entry, a missing or extra entry, or any
    other shape is rejected as a whole: links from a response we don't fully understand are not
    used."""
    if not isinstance(requested, (list, tuple)) or not requested or len(set(requested)) != len(requested):
        raise PartnerLinkError("bad_request_list")
    if len(requested) > 10:                       # documented: at most 10 links per request
        raise PartnerLinkError("too_many_links")
    if not isinstance(body, dict) or body.get("code") != "success":
        raise PartnerLinkError("api_not_success")
    result = body.get("result")
    if not isinstance(result, dict):
        raise PartnerLinkError("api_no_result")
    if str(result.get("marker")) != str(marker) or not marker:
        raise PartnerLinkError("api_marker_mismatch")
    if str(result.get("trs")) != str(trs) or not trs:
        raise PartnerLinkError("api_trs_mismatch")
    if result.get("shorten") is not False:
        raise PartnerLinkError("api_short_links")
    links = result.get("links")
    if not isinstance(links, list) or len(links) != len(requested):
        raise PartnerLinkError("api_link_count")
    out = {}
    for item in links:
        if not isinstance(item, dict):
            raise PartnerLinkError("api_link_shape")
        src, code, partner = item.get("url"), item.get("code"), item.get("partner_url")
        if src not in requested or src in out:
            raise PartnerLinkError("api_unexpected_url")
        if code != "success":
            raise PartnerLinkError("api_link_failed")
        if not isinstance(partner, str) or not partner:
            raise PartnerLinkError("api_no_partner_url")
        out[src] = partner
    return out
