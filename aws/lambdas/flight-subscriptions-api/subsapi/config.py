"""Catalog and policy, read from the same JSON files the front end builds from.

The package script copies aws/config/airports.json and subscription-policy.json into
subsapi/config/; in the repository (tests) they are read from aws/config/ directly.
"""
import json
import os
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent


def config_dir():
    override = os.environ.get("SUBSAPI_CONFIG_DIR")
    if override:
        return Path(override)
    bundled = HERE / "config"
    if (bundled / "airports.json").exists():
        return bundled
    return HERE.parents[2] / "config"  # aws/lambdas/flight-subscriptions-api/subsapi -> aws/config


@dataclass(frozen=True)
class Config:
    origins: dict
    destinations: dict
    max_subscriptions: int
    min_target: int
    max_target: int
    first_offset_months: int
    window_months: int

    def place(self, code):
        return self.origins.get(code) or self.destinations.get(code)


def load(env=None):
    env = os.environ if env is None else env
    d = config_dir()
    airports = json.loads((d / "airports.json").read_text(encoding="utf-8"))
    policy = json.loads((d / "subscription-policy.json").read_text(encoding="utf-8"))
    month = policy["departure_month"]
    if month["timezone"] != "Asia/Taipei" or month["format"] != "YYYY-MM":
        raise ValueError("unsupported departure_month policy")  # months.py assumes UTC+8, no DST
    max_subs = policy["max_subscriptions_per_user"]
    if env.get("MAX_SUBSCRIPTIONS"):
        max_subs = int(env["MAX_SUBSCRIPTIONS"])
    if not 1 <= max_subs <= 100:
        raise ValueError("MAX_SUBSCRIPTIONS out of range")
    return Config(
        origins={a["code"]: a for a in airports["origins"]},
        destinations={a["code"]: a for a in airports["destinations"]},
        max_subscriptions=max_subs,
        min_target=policy["target_price_twd"]["min"],
        max_target=policy["target_price_twd"]["max"],
        first_offset_months=month["first_offset_months"],
        window_months=month["window_months"],
    )
