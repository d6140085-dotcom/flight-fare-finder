"""flight-fare-notification v2 (M1.4 Phase 2B-1): SQS -> month-aware dedup -> Resend.

Handler: handler.handler. Same trigger contract as M1 (batch, partial-batch failures).
NOT DEPLOYED: replacing the M1 Lambda needs a reviewed change set
(docs/m1.4-phase2b1-month-dedup.md, "Deployment").
"""
import json
import os
from datetime import datetime, timezone

import boto3

from notify2 import config, history, keys, service

_ssm = boto3.client("ssm")
_ddb = boto3.resource("dynamodb")
_deps = None


def _param(name):
    return _ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"].strip()


def deps():
    global _deps
    if _deps is None:
        settings = config.Settings.from_env()
        cfg = {}

        def resend_cfg():
            if not cfg:
                raw = _param(settings.resend_param)
                v = json.loads(raw) if raw.startswith("{") else {"api_key": raw}
                cfg.update(key=v["api_key"], **{"from": v.get("from") or "onboarding@resend.dev"})
            return cfg
        _deps = service.Deps(
            settings=settings,
            history=history.History(_ddb.Table(history.TABLE), boto3.client("dynamodb")),
            pepper=_param(settings.pepper_param),          # fail closed: no pepper -> exception -> retry
            resend_cfg=resend_cfg,
            subscriptions=_ddb.Table("subscriptions") if settings.recheck_subscription else None,
            cities=json.loads(os.environ.get("CITIES") or "{}"), airlines=json.loads(os.environ.get("AIRLINES") or "{}"))
    return _deps


def handle(event, d, now_fn=lambda: datetime.now(timezone.utc)):
    failures = []
    for rec in event.get("Records", []):
        try:
            m = json.loads(rec["body"])
            service.process(m, rec.get("messageId"), now_fn(), d)
        except keys.InvalidMessage as e:
            d.log(f"dropped {rec.get('messageId')}: invalid message ({e})")   # never retried forever
        except Exception as e:
            d.log(f"retry {rec.get('messageId')}: {type(e).__name__}: {e}")
            failures.append({"itemIdentifier": rec["messageId"]})
    return {"batchItemFailures": failures}


def handler(event, context):
    try:
        d = deps()
    except Exception as e:  # configuration or pepper problem: fail closed, retry the whole batch
        print(f"retry batch: setup failed: {type(e).__name__}")
        return {"batchItemFailures": [{"itemIdentifier": r["messageId"]} for r in event.get("Records", [])]}
    return handle(event, d)
