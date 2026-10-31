"""Build the CloudFormation template for M1 Part 1.3 (email on target).

Stack flight-m1-email-on-target: the flight-fare-notification Lambda (inline code = repo code),
its log group, and the SQS trigger on flight-fare-queue (partial-batch retries).

Before creating/updating with the trigger: the role must allow sqs:ReceiveMessage,
DeleteMessage and GetQueueAttributes on the queue, the queue's VisibilityTimeout must exceed
the Lambda timeout, and stale messages should be purged.

Usage: python3 aws/stacks/build_m1_email_on_target.py <account-id> > template.json
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_m1_fetch_prices import REGION, source  # noqa: E402

FN = "flight-fare-notification"


def template(account):
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Flight Price Notifier M1 Part 1.3: email on target (SQS -> dedup -> Resend)",
        "Resources": {
            "NotificationLogs": {
                "Type": "AWS::Logs::LogGroup",
                "Properties": {"LogGroupName": f"/aws/lambda/{FN}", "RetentionInDays": 30},
            },
            "Notification": {
                "Type": "AWS::Lambda::Function",
                "DependsOn": ["NotificationLogs"],
                "Properties": {
                    "FunctionName": FN,
                    "Runtime": "python3.12",
                    "Handler": "index.handler",
                    "Role": f"arn:aws:iam::{account}:role/flight-lambda-role",
                    "Timeout": 30,
                    "MemorySize": 128,
                    "Environment": {"Variables": {
                        "RESEND_PARAM": "/flight/resend",
                        "NOTIFY_FLOOR_HOURS": "24",
                        "REALERT_PCT": "20",
                        "REALERT_ABS_TWD": "2000",
                    }},
                    "Code": {"ZipFile": source(FN)},
                },
            },
            "FareQueueTrigger": {
                "Type": "AWS::Lambda::EventSourceMapping",
                "Properties": {
                    "FunctionName": {"Ref": "Notification"},
                    "EventSourceArn": f"arn:aws:sqs:{REGION}:{account}:flight-fare-queue",
                    "BatchSize": 5,
                    "FunctionResponseTypes": ["ReportBatchItemFailures"],
                    "Enabled": True,
                },
            },
        },
    }


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        sys.exit(__doc__)
    json.dump(template(sys.argv[1]), sys.stdout, separators=(",", ":"), ensure_ascii=False)
