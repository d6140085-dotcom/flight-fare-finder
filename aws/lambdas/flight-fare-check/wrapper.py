"""flight-parser-wrapper (M1.4 Phase 2A): build work items from `subscriptions` (read without
email) and invoke the parser once per distinct (origin, destination, effective month).

Env: PARSER_FUNCTION (default flight-parser), DRY_RUN, MAX_WORK_ITEMS. Handler: wrapper.handler.
NOT DEPLOYED: replacing the M1 wrapper needs a reviewed change set (docs/m1.4-plan.md Phase 2A).
"""
from datetime import datetime, timezone

import boto3

from farecheck import config, effects, runner


def handler(event, context):
    settings = config.Settings.from_env()
    table = boto3.resource("dynamodb").Table("subscriptions")
    invoker = effects.LambdaInvoker(settings.parser_function, boto3.client, dry_run_payloads=settings.dry_run)
    return runner.run_wrapper(settings=settings, catalog=config.load_catalog(), reader=effects.TableReader(table),
                              invoker=invoker, now=datetime.now(timezone.utc))
