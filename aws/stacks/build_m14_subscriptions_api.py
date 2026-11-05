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

Usage (prints the template; contacts nothing):
  SUPABASE_URL=https://<ref>.supabase.co SUPABASE_PUBLISHABLE_KEY=sb_publishable_... \\
    python3 aws/stacks/build_m14_subscriptions_api.py <account-id> <s3-key> [code-sha256-base64]
"""
import json
import os
import sys

FUNCTION = "flight-subscriptions-api"
STACK = "flight-m1-4-subscriptions-api"
TAGS = [{"Key": "project", "Value": "flight-price-notifier"}, {"Key": "milestone", "Value": "m1.4"}]


def template(account, s3_key, supabase_url, publishable_key, code_sha256=None):
    if not publishable_key.startswith("sb_publishable_"):
        raise ValueError("SUPABASE_PUBLISHABLE_KEY must be a publishable key (sb_publishable_...)")
    if not supabase_url.startswith("https://"):
        raise ValueError("SUPABASE_URL must be https")
    desc = "Flight Price Notifier M1.4 - subscriptions API (no routes attached)"
    if code_sha256:
        desc += f"; code {code_sha256}"
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
                    "Code": {"S3Bucket": f"flight-artifacts-{account}", "S3Key": s3_key},
                    "Environment": {"Variables": {
                        "SUPABASE_URL": supabase_url,
                        "SUPABASE_PUBLISHABLE_KEY": publishable_key,
                        "SUBSCRIPTIONS_TABLE": "subscriptions",
                        "QUOTA_TABLE": "subscription_quota",
                        "MAX_SUBSCRIPTIONS": "10",
                    }},
                    "Tags": TAGS,
                },
            },
        },
        "Outputs": {"FunctionArn": {"Value": {"Fn::GetAtt": ["Function", "Arn"]}}},
    }


if __name__ == "__main__":
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_PUBLISHABLE_KEY")
    if len(sys.argv) not in (3, 4) or not sys.argv[1].isdigit() or not url or not key:
        sys.exit(__doc__)
    json.dump(template(sys.argv[1], sys.argv[2], url, key, sys.argv[3] if len(sys.argv) == 4 else None),
              sys.stdout, indent=2)
