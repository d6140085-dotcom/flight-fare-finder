"""flight-parser (M1.4 Phase 2A): one work item -> Travelpayouts (v1 or v3) -> matching rows
-> SQS (live) or log only (DRY_RUN). Never logs the token or email addresses.

Env: FARE_API (v1|v3, default v1), DRY_RUN, FETCH_USD, V3_LIMIT, FARE_QUEUE_URL, TP_PARAM.
Handler: parser.handler. NOT DEPLOYED (docs/m1.4-plan.md Phase 2A).
"""
import json
from datetime import datetime, timezone

import boto3

from farecheck import config, effects, runner

_token = None


def get_token(name):
    """SSM value: a bare token or {"token": ...}; cached per container (as M1)."""
    global _token
    if _token is None:
        raw = boto3.client("ssm").get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"].strip()
        try:
            v = json.loads(raw)
        except ValueError:
            v = raw
        _token = v.get("token") if isinstance(v, dict) else str(v)
        if not _token:
            raise RuntimeError("Travelpayouts parameter has no token")
    return _token


def handler(event, context):
    settings = config.Settings.from_env()
    table = boto3.resource("dynamodb").Table("subscriptions")
    return runner.run_parser(event, settings=settings, catalog=config.load_catalog(),
                             token=get_token(settings.tp_param), reader=effects.TableReader(table),
                             now=datetime.now(timezone.utc), client_factory=boto3.client)
