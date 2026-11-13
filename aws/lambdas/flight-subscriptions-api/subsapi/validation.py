"""Request validation (contract §4). Each function raises ApiError with the first failure, in the
contract's order, or returns clean values."""
import re

from . import subkeys
from .errors import ApiError
from .months import MONTH_RE, selectable_months

ROUTE_RE = re.compile(r"^[A-Z]{3}-[A-Z]{3}$")
PATCH_FIELDS = {"target_price", "departure_month", "alerts_paused", "origin", "destination"}
IGNORED_FIELDS = {"email"}  # identity only ever comes from the Supabase session


def _code(value):
    return value.strip().upper() if isinstance(value, str) else value


def origin(cfg, value):
    if value in (None, ""):
        raise ApiError(400, "origin_required")
    v = _code(value)
    if v not in cfg.origins:
        raise ApiError(400, "unsupported_origin")
    return v


def destination(cfg, value):
    if value in (None, ""):
        raise ApiError(400, "destination_required")
    v = _code(value)
    if v not in cfg.destinations:
        raise ApiError(400, "unsupported_destination")
    return v


def different(o, d):
    if o == d:
        raise ApiError(400, "same_origin_destination")


def target_price(cfg, value):
    """Strict integer: not a bool, string or float (contract §4.2)."""
    if isinstance(value, bool) or not isinstance(value, int) or not cfg.min_target <= value <= cfg.max_target:
        raise ApiError(400, "invalid_target_price")
    return value


def legacy_target_price(cfg, value):
    """M1.1 accepted whole-number floats (9000.0); the legacy endpoint keeps that behaviour."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        raise ApiError(400, "invalid_target_price")
    value = int(value)
    if not cfg.min_target <= value <= cfg.max_target:
        raise ApiError(400, "invalid_target_price")
    return value


def new_month(cfg, value, now):
    if not isinstance(value, str) or not MONTH_RE.match(value):
        raise ApiError(400, "invalid_departure_month")
    if value not in selectable_months(now, cfg.first_offset_months, cfg.window_months):
        raise ApiError(400, "invalid_departure_month")
    return value


def patched_month(cfg, value, stored, now):
    """A month in the window, or the stored value unchanged (an expired month, or null for a
    legacy rolling row). A fixed row can never return to rolling."""
    if value == stored:
        return value
    if value is None:
        raise ApiError(400, "invalid_departure_month")
    return new_month(cfg, value, now)


def route_param(value):
    if not isinstance(value, str) or not ROUTE_RE.match(value):
        raise ApiError(400, "invalid_route")
    return value


def id_param(value):
    """Public subscription ID from the URL path -> sort key (R-MM-1). "KHH-TYO" (legacy rolling)
    or "KHH-TYO_2027-01" (fixed month); anything else is 400 invalid_route."""
    key = subkeys.key_from_id(value)
    if key is None:
        raise ApiError(400, "invalid_route")
    return key


def create_body(cfg, body, now):
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_json")
    o = origin(cfg, body.get("origin"))
    d = destination(cfg, body.get("destination"))
    different(o, d)
    m = new_month(cfg, body.get("departure_month"), now)
    t = target_price(cfg, body.get("target_price"))
    return o, d, m, t


def patch_body(body):
    """Shape checks that don't need the stored row."""
    if not isinstance(body, dict):
        raise ApiError(400, "invalid_json")
    unknown = sorted(set(body) - PATCH_FIELDS - IGNORED_FIELDS - {"if_version"})
    if unknown:
        raise ApiError(400, "unknown_field", {"fields": unknown})
    if not PATCH_FIELDS & set(body):
        raise ApiError(400, "empty_patch")
    if ("origin" in body) != ("destination" in body):
        raise ApiError(400, "route_change_incomplete")
    if "alerts_paused" in body and not isinstance(body["alerts_paused"], bool):
        raise ApiError(400, "invalid_alerts_paused")
    if_version(body.get("if_version"))
    return body


VERSION_RE = re.compile(r"^(?:[A-Za-z0-9_-]{22}|legacy-[0-9a-f]{32})$")


def if_version(value):
    """The client's expected row version (contract §4.5): required for PATCH and DELETE."""
    if value is None:
        raise ApiError(428, "version_required")
    if not isinstance(value, str) or not VERSION_RE.match(value):
        raise ApiError(400, "invalid_version")
    return value
