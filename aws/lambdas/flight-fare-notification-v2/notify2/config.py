"""Settings for the v2 notification Lambda (environment variables).

The M1 re-alert thresholds keep their M1 names and defaults, so the deployed values carry over.
"""
import os
from dataclasses import dataclass
from decimal import Decimal

FALSE_WORDS = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    """Unsafe or incomplete configuration; nothing is sent."""


def flag(env, name, default):
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in FALSE_WORDS


@dataclass(frozen=True)
class Settings:
    floor_hours: Decimal = Decimal("24")        # NOTIFY_FLOOR_HOURS (M1)
    realert_pct: Decimal = Decimal("20")        # REALERT_PCT (M1)
    realert_abs_twd: Decimal = Decimal("2000")  # REALERT_ABS_TWD (M1)
    claim_seconds: int = 120                    # CLAIM_SECONDS: Lambda timeout < claim < queue visibility
    legacy_read: bool = True                    # LEGACY_HISTORY_READ: honour M1 rows (pk "{email}#{route}")
    legacy_dual_write: bool = True              # LEGACY_DUAL_WRITE: also write the M1 row (rollback safety)
    recheck_subscription: bool = True           # RECHECK_SUBSCRIPTION: re-read the row before sending
    pepper_param: str = "/flight/dedup-pepper"  # DEDUP_PEPPER_PARAM: SSM SecureString for subscriber ids
    resend_param: str = "/flight/resend"        # RESEND_PARAM (M1)
    lambda_timeout_seconds: int = 30            # LAMBDA_TIMEOUT_SECONDS: for the claim sanity check
    queue_visibility_seconds: int = 180         # QUEUE_VISIBILITY_SECONDS: for the claim sanity check

    def __post_init__(self):
        if not self.lambda_timeout_seconds < self.claim_seconds < self.queue_visibility_seconds:
            raise ConfigError("CLAIM_SECONDS must be above the Lambda timeout and below the queue visibility "
                              "timeout, so a crashed claim expires before SQS redelivers the message")

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        try:
            return cls(floor_hours=Decimal(env.get("NOTIFY_FLOOR_HOURS") or "24"),
                       realert_pct=Decimal(env.get("REALERT_PCT") or "20"),
                       realert_abs_twd=Decimal(env.get("REALERT_ABS_TWD") or "2000"),
                       claim_seconds=int(env.get("CLAIM_SECONDS") or 120),
                       legacy_read=flag(env, "LEGACY_HISTORY_READ", True),
                       legacy_dual_write=flag(env, "LEGACY_DUAL_WRITE", True),
                       recheck_subscription=flag(env, "RECHECK_SUBSCRIPTION", True),
                       pepper_param=env.get("DEDUP_PEPPER_PARAM") or "/flight/dedup-pepper",
                       resend_param=env.get("RESEND_PARAM") or "/flight/resend",
                       lambda_timeout_seconds=int(env.get("LAMBDA_TIMEOUT_SECONDS") or 30),
                       queue_visibility_seconds=int(env.get("QUEUE_VISIBILITY_SECONDS") or 180))
        except (ArithmeticError, ValueError) as e:
            if isinstance(e, ConfigError):
                raise
            raise ConfigError(f"invalid setting: {e}") from None
