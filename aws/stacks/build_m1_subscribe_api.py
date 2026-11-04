"""Build the CloudFormation template for the existing M1 Part 1.1 stack `flight-m1-subscribe-api`.

This reproduces the deployed stack exactly (verified against the live template): the HTTP API
`flight-api` with `POST /subscribe` and `GET /subscriptions`, and the Lambdas
`flight-save-subscription` / `flight-list-subscriptions` whose inline code is
aws/lambdas/<name>/index.py.

The Supabase URL and publishable key are inputs, not committed values. The publishable key is
browser-safe by design, but it is kept out of the repo like every other environment value.

Usage:
  SUPABASE_URL=https://<ref>.supabase.co SUPABASE_PUBLISHABLE_KEY=sb_publishable_... \
    python3 aws/stacks/build_m1_subscribe_api.py <account-id> > template.json
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_m1_fetch_prices import source  # noqa: E402

TAGS = [{"Key": "project", "Value": "flight-price-notifier"}]


def template(account, supabase_url, publishable_key):
    role = f"arn:aws:iam::{account}:role/flight-lambda-role"
    env = {"Variables": {"SUPABASE_URL": supabase_url, "SUPABASE_PUBLISHABLE_KEY": publishable_key}}

    def fn(name):
        return {
            "Type": "AWS::Lambda::Function",
            "Properties": {
                "FunctionName": name,
                "Runtime": "python3.12",
                "Handler": "index.handler",
                "Role": role,
                "Timeout": 10,
                "MemorySize": 128,
                "Environment": env,
                "Code": {"ZipFile": source(name)},
                "Tags": TAGS,
            },
        }

    def integ(fn_logical):
        return {
            "Type": "AWS::ApiGatewayV2::Integration",
            "Properties": {
                "ApiId": {"Ref": "Api"},
                "IntegrationType": "AWS_PROXY",
                "IntegrationUri": {"Fn::GetAtt": [fn_logical, "Arn"]},
                "PayloadFormatVersion": "2.0",
            },
        }

    def route(key, integ_logical):
        return {
            "Type": "AWS::ApiGatewayV2::Route",
            "Properties": {
                "ApiId": {"Ref": "Api"},
                "RouteKey": key,
                "Target": {"Fn::Join": ["", ["integrations/", {"Ref": integ_logical}]]},
            },
        }

    def perm(fn_logical):
        return {
            "Type": "AWS::Lambda::Permission",
            "Properties": {
                "Action": "lambda:InvokeFunction",
                "FunctionName": {"Ref": fn_logical},
                "Principal": "apigateway.amazonaws.com",
                "SourceArn": {"Fn::Sub": "arn:aws:execute-api:${AWS::Region}:${AWS::AccountId}:${Api}/*"},
            },
        }

    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "Flight Price Notifier M1.1 - subscribe API",
        "Resources": {
            "SaveFn": fn("flight-save-subscription"),
            "ListFn": fn("flight-list-subscriptions"),
            "Api": {
                "Type": "AWS::ApiGatewayV2::Api",
                "Properties": {
                    "Name": "flight-api",
                    "ProtocolType": "HTTP",
                    "CorsConfiguration": {
                        "AllowOrigins": ["*"],
                        "AllowMethods": ["GET", "POST", "OPTIONS"],
                        "AllowHeaders": ["content-type", "authorization"],
                        "MaxAge": 600,
                    },
                },
            },
            "SaveInteg": integ("SaveFn"),
            "ListInteg": integ("ListFn"),
            "SaveRoute": route("POST /subscribe", "SaveInteg"),
            "ListRoute": route("GET /subscriptions", "ListInteg"),
            "Stage": {
                "Type": "AWS::ApiGatewayV2::Stage",
                "Properties": {
                    "ApiId": {"Ref": "Api"},
                    "StageName": "$default",
                    "AutoDeploy": True,
                    "DefaultRouteSettings": {"ThrottlingBurstLimit": 20, "ThrottlingRateLimit": 10},
                },
            },
            "SavePerm": perm("SaveFn"),
            "ListPerm": perm("ListFn"),
        },
        "Outputs": {"ApiUrl": {"Value": {"Fn::GetAtt": ["Api", "ApiEndpoint"]}}},
    }


if __name__ == "__main__":
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_PUBLISHABLE_KEY")
    if len(sys.argv) != 2 or not sys.argv[1].isdigit() or not url or not key:
        sys.exit(__doc__)
    if not key.startswith("sb_publishable_"):
        sys.exit("SUPABASE_PUBLISHABLE_KEY must be a publishable key (sb_publishable_...), never a secret key")
    json.dump(template(sys.argv[1], url, key), sys.stdout, separators=(",", ":"))
