"""Temporary B4 validation runner (Lambda). Runs aws/integration/m14_scenarios.py against the two
temporary test tables named in its environment, and nothing else. Never deployed to production;
created and deleted with the owner-approved stack flight-m14-it-<run-id>.

Invoke with {} for every scenario, or {"scenarios": ["s01_...", ...]} for a subset.
Returns the full report; logs only a one-line summary (no emails: scenarios use example.com).
"""
import json
import os
import re

import boto3
from botocore.config import Config

import m14_scenarios as sc

TEST_TABLE = re.compile(r"^flight-m14-it-[a-z0-9]{6,12}-(subscriptions|quota)$")
PRODUCTION_TABLES = {"subscriptions", "subscription_quota", "notification_history"}


def check_tables(subs, quota):
    for t in (subs, quota):
        if t in PRODUCTION_TABLES or not TEST_TABLE.match(t or ""):
            raise RuntimeError(f"refusing to run against table {t!r}")
    if subs.rsplit("-", 1)[0] != quota.rsplit("-", 1)[0]:
        raise RuntimeError("tables belong to different test runs")


def handler(event, context):
    subs, quota = os.environ.get("SUBS_TABLE"), os.environ.get("QUOTA_TABLE")
    check_tables(subs, quota)
    client = boto3.client("dynamodb", config=Config(max_pool_connections=128,
                                                    retries={"mode": "standard", "max_attempts": 4}))
    run_id = (event or {}).get("run_id") or context.aws_request_id.replace("-", "")[:8]
    ctx = sc.Ctx(client, subs, quota, run_id, "aws")
    report = sc.run_all(ctx, (event or {}).get("scenarios"))
    print(json.dumps({"passed": report["passed"], "failed": [r["scenario"] for r in report["results"] if not r["passed"]],
                      "committed": report["transactions_committed"], "reasons": report["cancellation_reasons"]}))
    return report
