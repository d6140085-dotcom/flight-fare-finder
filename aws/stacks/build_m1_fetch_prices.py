"""Build the CloudFormation template for M1 Part 1.2 (fetch prices on a schedule).

The Lambda sources in aws/lambdas/*/index.py are embedded inline (Code.ZipFile, max 4096 chars),
so the deployed code is byte-for-byte the code in this repo.

Prereqs created outside this stack (Part 1.1 / Part 1.2 setup):
  role flight-lambda-role, bucket flight-config-<account>, queue flight-fare-queue,
  SSM SecureString /flight/travelpayouts.

Usage: python3 aws/stacks/build_m1_fetch_prices.py <account-id> > template.json
"""
import json
import sys
from pathlib import Path

REGION = "ap-southeast-2"
ROOT = Path(__file__).resolve().parents[1]
INLINE_LIMIT = 4096


def source(name):
    code = (ROOT / "lambdas" / name / "index.py").read_text()
    if len(code) > INLINE_LIMIT:
        sys.exit(f"{name}/index.py is {len(code)} chars; inline CFN allows {INLINE_LIMIT}")
    return code


def template(account):
    role = f"arn:aws:iam::{account}:role/flight-lambda-role"
    bucket = f"flight-config-{account}"
    queue_url = f"https://sqs.{REGION}.amazonaws.com/{account}/flight-fare-queue"

    def fn(name, timeout, env):
        return {
            "Type": "AWS::Lambda::Function",
            "DependsOn": [f"{name.title().replace('-', '')}Logs"],
            "Properties": {
                "FunctionName": name,
                "Runtime": "python3.12",
                "Handler": "index.handler",
                "Role": role,
                "Timeout": timeout,
                "MemorySize": 128,
                "Environment": {"Variables": env},
                "Code": {"ZipFile": source(name)},
            },
        }

    def logs(name):
        return {
            "Type": "AWS::Logs::LogGroup",
            "Properties": {"LogGroupName": f"/aws/lambda/{name}", "RetentionInDays": 30},
        }

    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Flight Price Notifier M1 Part 1.2: scheduled fare fetch (parser + wrapper + EventBridge)",
        "Resources": {
            "FlightParserLogs": logs("flight-parser"),
            "FlightParserWrapperLogs": logs("flight-parser-wrapper"),
            "Parser": fn("flight-parser", 30, {"FARE_QUEUE_URL": queue_url, "TP_PARAM": "/flight/travelpayouts"}),
            "Wrapper": fn("flight-parser-wrapper", 60, {"CONFIG_BUCKET": bucket, "PARSER_FUNCTION": "flight-parser"}),
            "Schedule": {
                "Type": "AWS::Events::Rule",
                "Properties": {
                    "Name": "flight-price-check",
                    "ScheduleExpression": "rate(30 minutes)",
                    "State": "ENABLED",
                    "Targets": [{"Id": "flight-parser-wrapper", "Arn": {"Fn::GetAtt": ["Wrapper", "Arn"]}}],
                },
            },
            "SchedulePermission": {
                "Type": "AWS::Lambda::Permission",
                "Properties": {
                    "FunctionName": {"Ref": "Wrapper"},
                    "Action": "lambda:InvokeFunction",
                    "Principal": "events.amazonaws.com",
                    "SourceArn": {"Fn::GetAtt": ["Schedule", "Arn"]},
                },
            },
        },
    }


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        sys.exit(__doc__)
    json.dump(template(sys.argv[1]), sys.stdout, separators=(",", ":"))
