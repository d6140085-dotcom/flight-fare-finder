"""Travelpayouts Partner Links API producer (B6-3B, offline; nothing deployed or enabled).

Travelpayouts Support (ticket #257588, 2026-11-27) recommends the Link Generator or the Partner
Links API rather than appending the marker to Aviasales URLs. When AFFILIATE_LINK_MODE=partner_api
the parser converts ONE destination per fare offer (shared by every matched recipient) before it
enqueues anything:

  1. destination: the Aviasales link the notification would build itself (mirrored
     searchlinks.py: same route, dates, catalog, `as_of` and month check), marker removed;
  2. POST /links/v1/create (documented contract): {trs, marker, shorten: false,
     links: [{url, sub_id}]};
  3. Layer A (documented response contract) and Layers B/C (URL safety, approved format with
     the exact decoded destination) from the mirrored partnerlinks.py;
  4. only then is `aviasales_partner_url` added to every recipient's message.

Any failure -> PartnerLinkUnavailable(reason) -> the parser enqueues NOTHING for that offer (no
partial queue, no direct-marker fallback) and the next scheduled fare check tries again.
APPROVED_FORMATS is empty until B6-3C, so step 3 always fails today: no message is enqueued in
partner_api mode.

PENDING OFFICIAL CONFIRMATION, deliberately not guessed:
  - the authentication header: the documentation says an API token is required but doesn't name
    the header. CONFIRMED_AUTH_HEADER is None, so the client refuses to send (reason
    `auth_method_unconfirmed`) unless a caller injects a header name (tests only). B6-3C sets it
    from a written confirmation or a controlled dry run.
  - trs/marker types: the documentation's request and response examples show unquoted JSON
    numbers, so we send integers (config refuses values with a leading zero). Whether the API
    also accepts strings is not documented.
  - the rate-limit (429) response body, link lifetime, reuse/caching and determinism.

Never logged: the token, marker, trs, destination or partner link (reason codes only).
"""
import json
import time
from datetime import datetime, timezone
import urllib.error
import urllib.request

from . import months, partnerlinks, searchlinks

API_URL = "https://api.travelpayouts.com/links/v1/create"
CONFIRMED_AUTH_HEADER = None          # pending official confirmation (see module docstring)
REQUEST_TIMEOUT_SECONDS = 5           # per attempt
RETRY_DELAY_SECONDS = 1               # one retry, fixed delay, no backoff
MAX_ATTEMPTS = 2
MIN_REMAINING_MS_FOR_RETRY = 15_000   # don't start the retry unless this much Lambda time is left
UA = "Mozilla/5.0 (compatible; flight-notifier/1.0)"


class HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


class PartnerLinkUnavailable(Exception):
    """No approved partner link for this offer: enqueue nothing. `reason` is data-free."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def urllib_post_json(url, headers, body, timeout):
    """Default transport. Raises HttpError, OSError (network/timeout) or ValueError (bad JSON)."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise HttpError(e.code) from None


def transient(status):
    return status == 429 or 500 <= status <= 599


def request_body(destination, marker, trs, sub_id):
    return {"trs": int(trs), "marker": int(marker), "shorten": False,
            "links": [{"url": destination, "sub_id": sub_id}]}


def create_link(destination, *, token, marker, trs, sub_id, transport=urllib_post_json,
                auth_header=CONFIRMED_AUTH_HEADER, sleep=time.sleep, remaining_ms=None):
    """The partner_url for `destination` (Layer A checked, NOT yet format-approved), or
    PartnerLinkUnavailable. At most one retry, only for a timeout/network error, 429 or 5xx."""
    if not auth_header:
        raise PartnerLinkUnavailable("auth_method_unconfirmed")
    if not token:
        raise PartnerLinkUnavailable("no_token")
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": UA,
               auth_header: token}
    body = request_body(destination, marker, trs, sub_id)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = transport(API_URL, headers, body, REQUEST_TIMEOUT_SECONDS)
        except HttpError as e:
            if not transient(e.status):
                raise PartnerLinkUnavailable(f"http_{e.status}") from None     # 400, 401, 403, ...
            reason = f"http_{e.status}"
        except ValueError:
            raise PartnerLinkUnavailable("malformed_json") from None
        except (OSError, TimeoutError):
            reason = "unavailable"
        else:
            try:
                return partnerlinks.check_api_response(response, [destination], marker=marker, trs=trs)[destination]
            except partnerlinks.PartnerLinkError as e:
                raise PartnerLinkUnavailable(e.reason) from None              # never retried
        if attempt == MAX_ATTEMPTS:
            break
        if remaining_ms is not None and remaining_ms() < MIN_REMAINING_MS_FOR_RETRY:
            raise PartnerLinkUnavailable(reason + "_no_time_to_retry")
        sleep(RETRY_DELAY_SECONDS)
    raise PartnerLinkUnavailable(reason)


_links = {}


def link_inputs():
    """The bundled search-link configuration and catalog (loaded once per container)."""
    if not _links:
        _links.update(config=searchlinks.load_config(), catalog=searchlinks.load_catalog())
    return _links["config"], _links["catalog"]


def expected_destination(body, *, marker, links_config=None, catalog=None):
    """The destination the notification will require (notify2.service.with_partner_link),
    computed from the message exactly as the notification computes it: route, the fare's dates,
    today at the message's `as_of` (Asia/Taipei) and the message's month. A fare outside the
    message month (or any other link problem) gets no partner link at all."""
    if links_config is None or catalog is None:
        links_config, catalog = link_inputs()
    if not body.get("as_of") or not body.get("month"):
        raise PartnerLinkUnavailable("needs_v2_message")           # as_of fixes "today" for both sides
    as_of = datetime.strptime(body["as_of"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    o, d = body["route"].split("-")
    f = body["cheapest"]
    links = searchlinks.build(o, d, f.get("depart_date"), f.get("return_date"), months.today(as_of),
                              catalog=catalog, config=links_config, marker=marker, month=body.get("month"))
    if links.reasons and ("fare_outside_subscribed_month" in links.reasons
                          or "invalid_subscribed_month" in links.reasons):
        raise PartnerLinkUnavailable(links.reasons[-1])
    if not links.compliant or links.aviasales is None:
        raise PartnerLinkUnavailable(",".join(links.reasons) or "no_affiliate_link")
    try:
        return partnerlinks.expected_destination(links.aviasales.url, marker)
    except partnerlinks.PartnerLinkError as e:
        raise PartnerLinkUnavailable(e.reason) from None


def approved_partner_link(body, *, token, marker, trs, sub_id, formats=None, transport=urllib_post_json,
                          auth_header=CONFIRMED_AUTH_HEADER, sleep=time.sleep, remaining_ms=None,
                          links_config=None, catalog=None):
    """One approved partner link for the offer in `body` (every recipient of the offer shares
    it), or PartnerLinkUnavailable. The API's link must pass the same checks the notification
    applies (approved format, exact destination, marker, trs); with no approved format (until
    B6-3C) this always fails closed."""
    formats = partnerlinks.APPROVED_FORMATS if formats is None else formats
    destination = expected_destination(body, marker=marker, links_config=links_config, catalog=catalog)
    url = create_link(destination, token=token, marker=marker, trs=trs, sub_id=sub_id, transport=transport,
                      auth_header=auth_header, sleep=sleep, remaining_ms=remaining_ms)
    try:
        partnerlinks.check_format(url, expected_destination=destination, marker=marker, trs=trs, formats=formats)
    except partnerlinks.PartnerLinkError as e:
        raise PartnerLinkUnavailable(f"partner_link_invalid:{e.reason}") from None
    return url
