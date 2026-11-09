"""Travelpayouts Data API adapters: v1 /prices/cheap (M1 behaviour) and v3 /prices_for_dates.

Both return a FareResult with the same validated Offer shape, so the parser never cares which
API answered. Every offer must be a round trip that departs in the requested month (not in the
past), in the requested currency, between the requested codes; anything else is rejected and
counted, never alerted on. The token travels only in the X-Access-Token header and is never
logged or returned.
"""
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from . import months

V1_URL = "https://api.travelpayouts.com/v1/prices/cheap"
V3_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
UA = "Mozilla/5.0 (compatible; flight-notifier/1.0)"
MAX_PRICE = 2_000_000          # far above any economy round trip in TWD; rejects garbage
MAX_TRIP_DAYS = 366            # return later than this after departure is treated as malformed

OK, NO_DATA, ERROR = "ok", "no_data", "error"


class HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


def urllib_get_json(url, headers, timeout=10):
    """Default transport. Raises HttpError, OSError (network/timeout) or ValueError (bad JSON)."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise HttpError(e.code) from None


@dataclass(frozen=True)
class Offer:
    price: int
    currency: str
    airline: object
    depart_at: str
    return_at: str
    source: str
    flight_number: object = None
    origin_airport: object = None
    destination_airport: object = None
    transfers: object = None
    return_transfers: object = None
    link: object = None

    @property
    def depart_date(self):
        return self.depart_at[:10]

    @property
    def return_date(self):
        return self.return_at[:10]

    def message_dict(self):
        """The M1 SQS `cheapest` shape, key for key (the notification Lambda is unchanged)."""
        return {"price": self.price, "currency": self.currency, "airline": self.airline,
                "depart_date": self.depart_at, "return_date": self.return_at}


@dataclass
class FareResult:
    kind: str
    source: str
    offer: Offer = None
    reason: str = None
    offers_seen: int = 0
    rejected: Counter = field(default_factory=Counter)


# ---- validation -----------------------------------------------------------------------------

def parse_when(value):
    """ISO 8601 date or date-time (with Z or an offset) -> (local date, aware datetime or None)."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        if len(value) == 10:
            return date.fromisoformat(value), None
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.date(), (dt if dt.tzinfo else None)


def to_price(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    if not d.is_finite() or (isinstance(value, float) and not math.isfinite(value)):
        return None
    p = int(d.to_integral_value(ROUND_HALF_UP))
    return p if 0 < p <= MAX_PRICE else None


def _code(value):
    return value.upper() if isinstance(value, str) else None


def validate_offer(raw, *, origin, destination, month, currency, catalog, now, source):
    """(Offer, None) or (None, reason)."""
    if not isinstance(raw, dict):
        return None, "not_object"
    price = to_price(raw.get("price"))
    if price is None:
        return None, "bad_price"
    item_cur = raw.get("currency")
    if item_cur is not None and str(item_cur).lower() != currency:
        return None, "currency_mismatch"
    if raw.get("return_at") in (None, ""):
        return None, "one_way"
    dep, ret = parse_when(raw.get("departure_at")), parse_when(raw.get("return_at"))
    if dep is None:
        return None, "bad_departure"
    if ret is None:
        return None, "bad_return"
    (dep_day, dep_dt), (ret_day, ret_dt) = dep, ret
    if f"{dep_day.year:04d}-{dep_day.month:02d}" != month:
        return None, "wrong_month"
    if ret_day < dep_day or (dep_dt and ret_dt and ret_dt < dep_dt):
        return None, "return_before_departure"
    if ret_day > dep_day + timedelta(days=MAX_TRIP_DAYS):
        return None, "return_too_far"
    if (dep_dt and dep_dt < now) or (not dep_dt and dep_day < months.today(now)):
        return None, "departed"
    expires = parse_when(raw.get("expires_at")) if raw.get("expires_at") is not None else None
    if raw.get("expires_at") is not None and (expires is None or (expires[1] and expires[1] < now)):
        return None, "expired"
    allowed_o, allowed_d = catalog.airports(origin) | {origin}, catalog.airports(destination) | {destination}
    if raw.get("origin") is not None and _code(raw["origin"]) not in allowed_o:
        return None, "origin_mismatch"
    if raw.get("destination") is not None and _code(raw["destination"]) not in allowed_d:
        return None, "destination_mismatch"
    o_ap, d_ap = _code(raw.get("origin_airport")), _code(raw.get("destination_airport"))
    if o_ap is not None and o_ap not in catalog.airports(origin):
        return None, "origin_airport_unexpected"
    if d_ap is not None and d_ap not in catalog.airports(destination):
        return None, "destination_airport_unexpected"
    return Offer(price=price, currency=currency.upper(), airline=raw.get("airline"),
                 depart_at=raw["departure_at"], return_at=raw["return_at"], source=source,
                 flight_number=raw.get("flight_number"), origin_airport=o_ap, destination_airport=d_ap,
                 transfers=raw.get("transfers"), return_transfers=raw.get("return_transfers"),
                 link=raw.get("link") if isinstance(raw.get("link"), str) else None), None


def cheapest(offers):
    """Lowest price; ties broken by earliest departure, then airline, so the result is stable."""
    return min(offers, key=lambda o: (o.price, o.depart_at, str(o.airline or ""))) if offers else None


# ---- client ---------------------------------------------------------------------------------

class Client:
    def __init__(self, token, catalog, now, api="v1", v3_limit=30, transport=urllib_get_json, log=print):
        self.token, self.catalog, self.now, self.api = token, catalog, now, api
        self.v3_limit, self.transport, self.log = v3_limit, transport, log
        self.requests = 0

    def request_url(self, origin, destination, month, currency):
        if self.api == "v1":  # exactly M1's query
            q = {"origin": origin, "destination": destination, "depart_date": month, "currency": currency}
            return f"{V1_URL}?{urllib.parse.urlencode(q)}"
        q = {"origin": origin, "destination": destination, "departure_at": month, "one_way": "false",
             "currency": currency, "sorting": "price", "limit": str(self.v3_limit)}
        return f"{V3_URL}?{urllib.parse.urlencode(q)}"

    def fetch(self, origin, destination, month, currency="twd"):
        currency = currency.lower()
        url = self.request_url(origin, destination, month, currency)
        headers = {"User-Agent": UA, "Accept": "application/json", "X-Access-Token": self.token}
        route = f"{origin}-{destination}"
        self.requests += 1
        try:
            body = self.transport(url, headers, 10)
        except HttpError as e:
            self.log(f"travelpayouts {self.api} {currency} HTTP {e.status} for {route} {month}")
            return FareResult(ERROR, self.api, reason=f"http_{e.status}")
        except ValueError:
            self.log(f"travelpayouts {self.api} {currency} malformed JSON for {route} {month}")
            return FareResult(ERROR, self.api, reason="malformed")
        except (OSError, TimeoutError) as e:
            self.log(f"travelpayouts {self.api} {currency} unavailable for {route} {month}: {type(e).__name__}")
            return FareResult(ERROR, self.api, reason="unavailable")
        return self.parse(body, origin, destination, month, currency)

    def parse(self, body, origin, destination, month, currency):
        if not isinstance(body, dict):
            return FareResult(ERROR, self.api, reason="malformed")
        if body.get("success") is not True:
            return FareResult(ERROR, self.api, reason="api_unsuccessful")
        top = body.get("currency")
        if top is not None and str(top).lower() != currency:
            return FareResult(ERROR, self.api, reason="currency_mismatch")
        data = body.get("data")
        if self.api == "v1":
            if data in (None, {}, []):
                return FareResult(NO_DATA, self.api, reason="empty")
            if not isinstance(data, dict):
                return FareResult(ERROR, self.api, reason="malformed")
            per_dest = data.get(destination)
            if per_dest in (None, {}):
                return FareResult(NO_DATA, self.api, reason="empty")
            if not isinstance(per_dest, dict):
                return FareResult(ERROR, self.api, reason="malformed")
            raws = list(per_dest.values())
        else:
            if data is None or data == []:
                return FareResult(NO_DATA, self.api, reason="empty")
            if not isinstance(data, list):
                return FareResult(ERROR, self.api, reason="malformed")
            raws = data
        valid, rejected = [], Counter()
        for raw in raws:
            offer, why = validate_offer(raw, origin=origin, destination=destination, month=month,
                                        currency=currency, catalog=self.catalog, now=self.now, source=self.api)
            if offer:
                valid.append(offer)
            else:
                rejected[why] += 1
        best = cheapest(valid)
        if not best:
            return FareResult(NO_DATA, self.api, reason="all_rejected", offers_seen=len(raws), rejected=rejected)
        return FareResult(OK, self.api, offer=best, offers_seen=len(raws), rejected=rejected)
