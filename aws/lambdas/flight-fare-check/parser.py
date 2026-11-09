"""flight-parser (M1.4 Phase 2A): one work item -> Travelpayouts (v1 or v3) -> matching rows
-> SQS. Month rules use the wrapper's `as_of` from the payload (not this function's clock).

In dry run (FARECHECK_MODE=dry_run, or a payload with "dry_run": true) only `ssm` can be
created: no SQS client exists and nothing is sent. A missing or conflicting mode refuses to run.
Never logs the token or email addresses.

Handler: parser.handler. NOT DEPLOYED (docs/m1.4-plan.md Phase 2A).
"""
from datetime import datetime, timezone

import boto3

from farecheck import config, effects, runner

_token = None


def handler(event, context):
    global _token
    settings = config.Settings.from_env()          # ConfigError before any AWS client
    dry_run = settings.dry_run or event.get("dry_run") is True
    factory = effects.guarded_factory(boto3.client, {"ssm"} if dry_run else {"ssm", "sqs"})
    if _token is None:
        _token = effects.get_token(settings.tp_param, factory)
    table = effects.ReadOnlyTable(boto3.resource("dynamodb").Table("subscriptions"))
    return runner.run_parser(event, settings=settings, catalog=config.load_catalog(), token=_token,
                             reader=effects.TableReader(table), now=datetime.now(timezone.utc),
                             client_factory=factory)
