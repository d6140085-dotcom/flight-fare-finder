"""flight-parser-wrapper (M1.4 Phase 2A).

FARECHECK_MODE=live: build work items from `subscriptions` (read without email) and invoke the
parser once per distinct (origin, destination, effective month), passing the month and this
run's `as_of`.

FARECHECK_MODE=dry_run: evaluate the work items in-process and return a report. Only an `ssm`
client (the Travelpayouts token) can be created; the parser, SQS, the notification Lambda and
email are unreachable, and `subscriptions` is read-only. A missing or conflicting mode refuses
to run before any client exists.

Handler: wrapper.handler. NOT DEPLOYED (docs/m1.4-plan.md Phase 2A).
"""
from datetime import datetime, timezone

import boto3

from farecheck import config, effects, runner
from farecheck.travelpayouts import Client


def handler(event, context):
    settings = config.Settings.from_env()          # ConfigError before any AWS client
    now = datetime.now(timezone.utc)               # the run's single as_of
    catalog = config.load_catalog()
    reader = effects.TableReader(effects.ReadOnlyTable(boto3.resource("dynamodb").Table("subscriptions")))
    if settings.dry_run:
        factory = effects.guarded_factory(boto3.client, allowed={"ssm"})
        client = Client(effects.get_token(settings.tp_param, factory), catalog, now,
                        api=settings.fare_api, v3_limit=settings.v3_limit)
        evaluator = runner.DryRunEvaluator(settings=settings, catalog=catalog, client=client, as_of=now)
        return runner.run_wrapper(settings=settings, catalog=catalog, reader=reader, now=now, dry_evaluator=evaluator)
    invoker = effects.LambdaInvoker(settings.parser_function, effects.guarded_factory(boto3.client, {"lambda"}),
                                    dry_run=settings.dry_run)
    return runner.run_wrapper(settings=settings, catalog=catalog, reader=reader, now=now, invoker=invoker)
