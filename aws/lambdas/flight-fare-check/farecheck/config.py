"""Catalog (aws/config/airports.json) and runtime settings (environment variables).

The package script copies airports.json into farecheck/config/; in the repository (tests) it is
read from aws/config/ directly.

Mode is fail-closed: FARECHECK_MODE must be set explicitly to `dry_run` or `live`. A missing or
unknown mode, or a DRY_RUN variable that disagrees with it, is a ConfigError raised before any
AWS client exists.
"""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
import json

HERE = Path(__file__).resolve().parent
FALSE_WORDS = {"0", "false", "no", "off"}
DRY_RUN, LIVE = "dry_run", "live"
# B6-3B: the same names and values as the notification's AFFILIATE_LINK_MODE. Only partner_api
# changes the fare check (it creates the partner link before enqueueing); disabled (default) and
# direct enqueue exactly as before.
LINK_MODES = ("disabled", "direct", "partner_api")
# partner_api sends trs and marker as JSON numbers (as documented), so a leading zero couldn't
# survive the round trip: such values are refused rather than silently changed
MARKER_RE = re.compile(r"^[1-9][0-9]{2,11}$")   # searchlinks.MARKER_RE, without a leading zero
TRS_RE = re.compile(r"^[1-9][0-9]{0,11}$")      # partnerlinks.TRS_RE, without a leading zero
SUB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")  # fixed, non-personal (never a user or subscription id)


class ConfigError(ValueError):
    """Unsafe or incomplete configuration; nothing runs."""


def config_dir():
    override = os.environ.get("FARECHECK_CONFIG_DIR")
    if override:
        return Path(override)
    bundled = HERE / "config"
    if (bundled / "airports.json").exists():
        return bundled
    return HERE.parents[2] / "config"  # aws/lambdas/flight-fare-check/farecheck -> aws/config


@dataclass(frozen=True)
class Catalog:
    origins: dict
    destinations: dict

    def known_route(self, origin, destination):
        return origin in self.origins and destination in self.destinations

    def airports(self, code):
        """Airports a code may resolve to: a city's listed airports, or the airport itself."""
        entry = self.origins.get(code) or self.destinations.get(code) or {}
        return frozenset(entry.get("airports") or [code])


def load_catalog(directory=None):
    d = Path(directory) if directory else config_dir()
    data = json.loads((d / "airports.json").read_text(encoding="utf-8"))
    return Catalog(origins={a["code"]: a for a in data["origins"]},
                   destinations={a["code"]: a for a in data["destinations"]})


def flag(env, name, default):
    """Boolean setting. Any value other than an explicit false word counts as true."""
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in FALSE_WORDS


def resolve_mode(env):
    raw = (env.get("FARECHECK_MODE") or "").strip().lower()
    if not raw:
        raise ConfigError("FARECHECK_MODE is not set (expected dry_run or live); refusing to run")
    if raw not in (DRY_RUN, LIVE):
        raise ConfigError(f"FARECHECK_MODE={raw!r} is not dry_run or live; refusing to run")
    dry = env.get("DRY_RUN")
    if dry is not None and dry.strip() != "" and flag(env, "DRY_RUN", False) != (raw == DRY_RUN):
        raise ConfigError("DRY_RUN conflicts with FARECHECK_MODE; refusing to run")
    return raw


def bounded_int(env, name, default, low, high):
    try:
        value = int(env.get(name) or default)
    except ValueError:
        raise ConfigError(f"{name} is not an integer") from None
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return value


@dataclass(frozen=True)
class Settings:
    mode: str = DRY_RUN           # FARECHECK_MODE (required in the environment): dry_run | live
    fare_api: str = "v1"          # FARE_API: v1 (M1 behaviour, default) or v3
    fetch_usd: bool = True        # FETCH_USD: M1's best-effort USD lookup for the 約 US$ line
    v3_limit: int = 30            # V3_LIMIT: offers per v3 request (documented default 30, max 1000)
    max_work_items: int = 500     # MAX_WORK_ITEMS: live work items per run (rotating window)
    dry_run_max_items: int = 50   # DRY_RUN_MAX_ITEMS: in-process dry-run lookups per run (rotating)
    schedule_minutes: int = 30    # SCHEDULE_PERIOD_MINUTES: must match the EventBridge rate
    message_version: int = 1      # NOTIFY_MESSAGE_VERSION: 1 = M1 shape; 2 adds "month" and "as_of" (Phase 2B-1)
    queue_url: str = ""           # FARE_QUEUE_URL (live parser only)
    parser_function: str = "flight-parser"
    tp_param: str = "/flight/travelpayouts"
    affiliate_link_mode: str = "disabled"   # AFFILIATE_LINK_MODE: partner_api creates partner links (B6-3B)
    tp_marker: str = field(default=None, repr=False)   # TP_MARKER (partner_api only; never logged)
    tp_trs: str = field(default=None, repr=False)      # TP_TRS (partner_api only; never logged)
    partner_sub_id: str = "fff-alert-v1"    # PARTNER_SUB_ID: fixed Sub ID, no personal data

    def __post_init__(self):
        if self.affiliate_link_mode not in LINK_MODES:
            raise ConfigError("AFFILIATE_LINK_MODE must be one of " + ", ".join(LINK_MODES))
        if not SUB_ID_RE.match(self.partner_sub_id or ""):
            raise ConfigError("PARTNER_SUB_ID must be 1-32 letters, digits, '-' or '_'")
        if self.affiliate_link_mode == "partner_api":
            # never name the values in the error
            if not MARKER_RE.match(self.tp_marker or ""):
                raise ConfigError("AFFILIATE_LINK_MODE=partner_api requires a valid TP_MARKER")
            if not TRS_RE.match(self.tp_trs or ""):
                raise ConfigError("AFFILIATE_LINK_MODE=partner_api requires a valid TP_TRS")
            if self.message_version != 2:
                # the notification rebuilds the destination from the message's as_of and month;
                # v1 messages have neither, so producer and consumer could disagree
                raise ConfigError("AFFILIATE_LINK_MODE=partner_api requires NOTIFY_MESSAGE_VERSION=2")

    @property
    def dry_run(self):
        return self.mode != LIVE      # anything but an explicit live mode is dry

    @property
    def item_cap(self):
        return self.dry_run_max_items if self.dry_run else self.max_work_items

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        mode = resolve_mode(env)
        api = (env.get("FARE_API") or "v1").strip().lower()
        if api not in ("v1", "v3"):
            raise ConfigError("FARE_API must be v1 or v3")
        return cls(mode=mode, fare_api=api, fetch_usd=flag(env, "FETCH_USD", True),
                   v3_limit=bounded_int(env, "V3_LIMIT", 30, 1, 1000),
                   max_work_items=bounded_int(env, "MAX_WORK_ITEMS", 500, 1, 10000),
                   dry_run_max_items=bounded_int(env, "DRY_RUN_MAX_ITEMS", 50, 1, 1000),
                   schedule_minutes=bounded_int(env, "SCHEDULE_PERIOD_MINUTES", 30, 1, 1440),
                   message_version=bounded_int(env, "NOTIFY_MESSAGE_VERSION", 1, 1, 2),
                   queue_url=env.get("FARE_QUEUE_URL", ""),
                   parser_function=env.get("PARSER_FUNCTION") or "flight-parser",
                   tp_param=env.get("TP_PARAM") or "/flight/travelpayouts",
                   affiliate_link_mode=(env.get("AFFILIATE_LINK_MODE") or "disabled").strip().lower(),
                   tp_marker=(env.get("TP_MARKER") or "").strip() or None,
                   tp_trs=(env.get("TP_TRS") or "").strip() or None,
                   partner_sub_id=(env.get("PARTNER_SUB_ID") or "fff-alert-v1").strip())
