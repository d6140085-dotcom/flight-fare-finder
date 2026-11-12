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
ACCOUNT, REGION = "379959319681", "ap-southeast-2"
FUNCTION_ARN = re.compile(r"^arn:aws:lambda:([a-z0-9-]+):(\d{12}):function:(flight-m14-it-[a-z0-9]{6,12})-runner$")


def check_tables(subs, quota):
    for t in (subs, quota):
        if t in PRODUCTION_TABLES or not TEST_TABLE.match(t or ""):
            raise RuntimeError(f"refusing to run against table {t!r}")
    if subs.rsplit("-", 1)[0] != quota.rsplit("-", 1)[0]:
        raise RuntimeError("tables belong to different test runs")


def check_environment(function_arn, env, subs, quota):
    """Account, region and naming guard: this function, its region and both tables must belong
    to the one temporary stack flight-m14-it-<run> in account 379959319681, ap-southeast-2."""
    m = FUNCTION_ARN.match(function_arn or "")
    if not m:
        raise RuntimeError(f"refusing to run as {function_arn!r}: not a flight-m14-it-<run>-runner function")
    region, account, prefix = m.groups()
    if (region, account) != (REGION, ACCOUNT):
        raise RuntimeError(f"refusing to run in {account}/{region}")
    if (env.get("EXPECTED_ACCOUNT"), env.get("EXPECTED_REGION"), env.get("AWS_REGION")) != (ACCOUNT, REGION, REGION):
        raise RuntimeError("account/region environment does not match ap-southeast-2 / the test account")
    if env.get("RUN_PREFIX") != prefix or (subs, quota) != (f"{prefix}-subscriptions", f"{prefix}-quota"):
        raise RuntimeError("tables do not belong to this runner's stack")


def check_table_arns(client, subs, quota):
    """The tables actually reached must live in the test account and region."""
    for t in (subs, quota):
        arn = client.describe_table(TableName=t)["Table"]["TableArn"]
        if arn != f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{t}":
            raise RuntimeError(f"refusing table {arn!r}")


def handler(event, context):
    subs, quota = os.environ.get("SUBS_TABLE"), os.environ.get("QUOTA_TABLE")
    check_tables(subs, quota)
    check_environment(getattr(context, "invoked_function_arn", None), os.environ, subs, quota)
    client = boto3.client("dynamodb", region_name=REGION,
                          config=Config(max_pool_connections=128, retries={"mode": "standard", "max_attempts": 4}))
    check_table_arns(client, subs, quota)
    run_id = (event or {}).get("run_id") or context.aws_request_id.replace("-", "")[:8]
    ctx = sc.Ctx(client, subs, quota, run_id, "aws")
    report = sc.run_all(ctx, (event or {}).get("scenarios"))
    print(json.dumps({"passed": report["passed"], "failed": [r["scenario"] for r in report["results"] if not r["passed"]],
                      "committed": report["transactions_committed"], "reasons": report["cancellation_reasons"]}))
    return report
