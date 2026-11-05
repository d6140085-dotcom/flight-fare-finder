"""CloudFormation template for M1.4 Phase 1B: stack `flight-m1-4-subscriptions-api`.

Creates, and only creates:
  * DynamoDB table `subscription_quota` (PK email; on-demand; PITR; SSE; Retain on delete/replace)
  * Lambda `flight-subscriptions-api` (python3.12, code from the S3 artifact, existing role
    `flight-subscriptions-api-role`, which is created in the one-time IAM bootstrap, not here)
  * Log group `/aws/lambda/flight-subscriptions-api` (30-day retention)

It deliberately contains **no API Gateway integration, route or invoke permission**: until the
deployment gate (docs/m1.4-plan.md) passes, the function is reachable only by direct, IAM-
authorized invocation. The cutover that points `POST /subscribe`, `GET /subscriptions` and the
new routes at this function is a separate, reviewed change set (contract §8.3). The existing
`subscriptions` table, `notification_history`, the SQS trigger and the schedule are not referenced.

Traceability: the artifact key is ``artifacts/flight-subscriptions-api/<git-commit>/<sha256>.zip``
(immutable: written once with If-None-Match, see plan_m14_subscriptions_api.sh); the function's
Description and tags carry the commit and the CodeSha256, which the post-deploy check compares
with ``lambda get-function``.

Usage (prints the template; contacts nothing):
  SUPABASE_URL=https://<ref>.supabase.co SUPABASE_PUBLISHABLE_KEY=sb_publishable_... \\
    python3 aws/stacks/build_m14_subscriptions_api.py <account-id> <git-commit> <sha256-hex> <code-sha256-base64>
"""
import json
import os
import re
import sys

FUNCTION = "flight-subscriptions-api"
STACK = "flight-m1-4-subscriptions-api"
TAGS = [{"Key": "project", "Value": "flight-price-notifier"}, {"Key": "milestone", "Value": "m1.4"}]


# Never allowed in this (production) template; see subsapi/auth.py.
FORBIDDEN_ENV = {"ALLOW_DIRECT_TEST_INVOKE"}


def artifact_key(git_commit, sha256_hex):
    if not re.fullmatch(r"[0-9a-f]{40}", git_commit or ""):
        raise ValueError("git commit must be a full 40-character SHA")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256_hex or ""):
        raise ValueError("artifact sha256 must be 64 hex characters")
    return f"artifacts/{FUNCTION}/{git_commit}/{sha256_hex}.zip"


def template(account, git_commit, sha256_hex, code_sha256, supabase_url, publishable_key):
    if not publishable_key.startswith("sb_publishable_"):
        raise ValueError("SUPABASE_PUBLISHABLE_KEY must be a publishable key (sb_publishable_...)")
    if not supabase_url.startswith("https://"):
        raise ValueError("SUPABASE_URL must be https")
    s3_key = artifact_key(git_commit, sha256_hex)
    env = {
        "SUPABASE_URL": supabase_url,
        "SUPABASE_PUBLISHABLE_KEY": publishable_key,
        "SUBSCRIPTIONS_TABLE": "subscriptions",
        "QUOTA_TABLE": "subscription_quota",
        "MAX_SUBSCRIPTIONS": "10",
    }
    assert not FORBIDDEN_ENV & set(env)
    tags = TAGS + [{"Key": "git_commit", "Value": git_commit}, {"Key": "code_sha256", "Value": code_sha256}]
    desc = "Flight Price Notifier M1.4 - subscriptions API (no routes attached)"
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": desc,
        "Resources": {
            "QuotaTable": {
                "Type": "AWS::DynamoDB::Table",
                "DeletionPolicy": "Retain",
                "UpdateReplacePolicy": "Retain",
                "Properties": {
                    "TableName": "subscription_quota",
                    "BillingMode": "PAY_PER_REQUEST",
                    "AttributeDefinitions": [{"AttributeName": "email", "AttributeType": "S"}],
                    "KeySchema": [{"AttributeName": "email", "KeyType": "HASH"}],
                    "PointInTimeRecoverySpecification": {"PointInTimeRecoveryEnabled": True},
                    "SSESpecification": {"SSEEnabled": True},
                    "Tags": TAGS,
                },
            },
            "LogGroup": {
                "Type": "AWS::Logs::LogGroup",
                "DeletionPolicy": "Retain",
                "Properties": {"LogGroupName": f"/aws/lambda/{FUNCTION}", "RetentionInDays": 30},
            },
            "Function": {
                "Type": "AWS::Lambda::Function",
                "DependsOn": ["LogGroup", "QuotaTable"],
                "Properties": {
                    "FunctionName": FUNCTION,
                    "Runtime": "python3.12",
                    "Architectures": ["x86_64"],
                    "Handler": "index.handler",
                    "Role": f"arn:aws:iam::{account}:role/flight-subscriptions-api-role",
                    "Timeout": 10,
                    "MemorySize": 256,
                    "Description": f"commit {git_commit[:12]} code {code_sha256}",
                    "Code": {"S3Bucket": f"flight-artifacts-{account}", "S3Key": s3_key},
                    "Environment": {"Variables": env},
                    "Tags": tags,
                },
            },
        },
        "Outputs": {
            "FunctionArn": {"Value": {"Fn::GetAtt": ["Function", "Arn"]}},
            "GitCommit": {"Value": git_commit},
            "ArtifactKey": {"Value": s3_key},
            "ExpectedCodeSha256": {"Value": code_sha256},
        },
    }


if __name__ == "__main__":
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_PUBLISHABLE_KEY")
    if len(sys.argv) != 5 or not sys.argv[1].isdigit() or not url or not key:
        sys.exit(__doc__)
    json.dump(template(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], url, key), sys.stdout, indent=2)
