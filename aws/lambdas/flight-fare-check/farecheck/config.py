"""Catalog (aws/config/airports.json) and runtime settings (environment variables).

The package script copies airports.json into farecheck/config/; in the repository (tests) it is
read from aws/config/ directly.
"""
import json
import os
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
FALSE_WORDS = {"0", "false", "no", "off"}


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


@dataclass(frozen=True)
class Settings:
    fare_api: str = "v1"          # FARE_API: v1 (M1 behaviour, default) or v3
    dry_run: bool = False         # DRY_RUN: compute and log only; never SQS
    fetch_usd: bool = True        # FETCH_USD: M1's best-effort USD lookup for the 約 US$ line
    v3_limit: int = 30            # V3_LIMIT: offers per v3 request (documented default 30, max 1000)
    max_work_items: int = 500     # MAX_WORK_ITEMS: safety cap on parser invocations per run
    queue_url: str = ""           # FARE_QUEUE_URL (live mode only)
    parser_function: str = "flight-parser"
    tp_param: str = "/flight/travelpayouts"

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        api = (env.get("FARE_API") or "v1").strip().lower()
        if api not in ("v1", "v3"):
            raise ValueError("FARE_API must be v1 or v3")
        limit = int(env.get("V3_LIMIT") or 30)
        cap = int(env.get("MAX_WORK_ITEMS") or 500)
        if not 1 <= limit <= 1000 or not 1 <= cap <= 10000:
            raise ValueError("V3_LIMIT or MAX_WORK_ITEMS out of range")
        return cls(fare_api=api, dry_run=flag(env, "DRY_RUN", False), fetch_usd=flag(env, "FETCH_USD", True),
                   v3_limit=limit, max_work_items=cap, queue_url=env.get("FARE_QUEUE_URL", ""),
                   parser_function=env.get("PARSER_FUNCTION") or "flight-parser",
                   tp_param=env.get("TP_PARAM") or "/flight/travelpayouts")
