"""CloudFormation template for the temporary B4 test environment `flight-m14-it-<run-id>`.

Creates only (all deleted with the stack; DeletionPolicy Delete everywhere):
  * flight-m14-it-<run>-subscriptions  (PK email, SK route; same schema as production)
  * flight-m14-it-<run>-quota          (PK email)
  * /aws/lambda/flight-m14-it-<run>-runner log group (1-day retention)
  * IAM role flight-m14-it-<run>-runner-role: data-plane access to the two test tables and its
    own log group only. Explicit Deny statements: every production DynamoDB table by name; any
    DynamoDB resource other than the two test tables; every other AWS service by name (SQS,
    Lambda, EventBridge, Scheduler, API Gateway, SSM, Secrets Manager, SES, SNS, IAM, STS, S3,
    CloudFormation, KMS); any action outside DynamoDB and the two log-write actions; any log
    resource other than its own group; any region other than ap-southeast-2
  * Lambda flight-m14-it-<run>-runner with a placeholder; the runner ZIP
    (aws/integration/package_runner.py) is loaded afterwards with UpdateFunctionCode
No API Gateway, event source, schedule, queue, SSM, S3 or production resource is referenced.

Usage (prints the template, contacts nothing):
  python3 aws/integration/build_test_env.py <run-id> [expires-YYYY-MM-DD]
"""
import json
import re
import sys

ACCOUNT, REGION = "379959319681", "ap-southeast-2"
PROD_TABLES = ["subscriptions", "subscription_quota", "notification_history"]
DDB_ACTIONS = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem",
               "dynamodb:DeleteItem", "dynamodb:ConditionCheckItem", "dynamodb:DescribeTable"]
DENIED_SERVICES = ["sqs:*", "lambda:*", "events:*", "scheduler:*", "apigateway:*", "execute-api:*", "ssm:*",
                   "secretsmanager:*", "ses:*", "sns:*", "iam:*", "sts:*", "s3:*", "cloudformation:*", "kms:*"]
PLACEHOLDER = "def handler(event, context):\n    raise RuntimeError('runner code not loaded')\n"


def names(run_id):
    if not re.fullmatch(r"[a-z0-9]{6,12}", run_id or ""):
        raise ValueError("run id: 6-12 lowercase letters/digits")
    p = f"flight-m14-it-{run_id}"
    return {"stack": p, "subs": f"{p}-subscriptions", "quota": f"{p}-quota", "fn": f"{p}-runner",
            "role": f"{p}-runner-role", "logs": f"/aws/lambda/{p}-runner"}


def template(run_id, expires="2026-11-30"):
    n = names(run_id)
    tags = [{"Key": "project", "Value": "flight-price-notifier"}, {"Key": "purpose", "Value": "m14-b4-temporary-test"},
            {"Key": "run_id", "Value": run_id}, {"Key": "delete_by", "Value": expires}]
    table = lambda name, keys: {  # noqa: E731
        "Type": "AWS::DynamoDB::Table", "DeletionPolicy": "Delete", "UpdateReplacePolicy": "Delete",
        "Properties": {"TableName": name, "BillingMode": "PAY_PER_REQUEST",
                       "AttributeDefinitions": [{"AttributeName": k, "AttributeType": "S"} for k in keys],
                       "KeySchema": [{"AttributeName": k, "KeyType": t} for k, t in zip(keys, ("HASH", "RANGE"))],
                       "Tags": tags}}
    prod_arns = [f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{t}" for t in PROD_TABLES]
    policy = {"Version": "2012-10-17", "Statement": [
        {"Sid": "TestTablesOnly", "Effect": "Allow", "Action": DDB_ACTIONS,
         "Resource": [{"Fn::GetAtt": ["SubsTable", "Arn"]}, {"Fn::GetAtt": ["QuotaTable", "Arn"]}]},
        {"Sid": "OwnLogsOnly", "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": {"Fn::Sub": "${RunnerLogs.Arn}"}},
        {"Sid": "NeverProduction", "Effect": "Deny", "Action": "dynamodb:*",
         "Resource": prod_arns + [a + "/*" for a in prod_arns]},
        {"Sid": "NoTableButTheTestTables", "Effect": "Deny", "Action": "dynamodb:*",
         "NotResource": [{"Fn::GetAtt": ["SubsTable", "Arn"]}, {"Fn::Sub": "${SubsTable.Arn}/*"},
                         {"Fn::GetAtt": ["QuotaTable", "Arn"]}, {"Fn::Sub": "${QuotaTable.Arn}/*"}]},
        {"Sid": "NoOtherServices", "Effect": "Deny", "Action": DENIED_SERVICES, "Resource": "*"},
        {"Sid": "NothingElseAtAll", "Effect": "Deny",
         "NotAction": ["dynamodb:*", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "*"},
        {"Sid": "NoOtherLogGroup", "Effect": "Deny", "Action": "logs:*",
         "NotResource": [{"Fn::GetAtt": ["RunnerLogs", "Arn"]}, {"Fn::Sub": "${RunnerLogs.Arn}:*"}]},
        {"Sid": "SydneyOnly", "Effect": "Deny", "Action": "*", "Resource": "*",
         "Condition": {"StringNotEquals": {"aws:RequestedRegion": REGION}}},
    ]}
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": f"TEMPORARY M1.4 B4 DynamoDB concurrency test environment ({run_id}). Delete after use.",
        "Resources": {
            "SubsTable": table(n["subs"], ["email", "route"]),
            "QuotaTable": table(n["quota"], ["email"]),
            "RunnerLogs": {"Type": "AWS::Logs::LogGroup", "DeletionPolicy": "Delete",
                           "Properties": {"LogGroupName": n["logs"], "RetentionInDays": 1}},
            "RunnerRole": {"Type": "AWS::IAM::Role", "Properties": {
                "RoleName": n["role"], "Tags": tags,
                "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{
                    "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole",
                    "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT}}}]},
                "Policies": [{"PolicyName": "b4-test-tables-only", "PolicyDocument": policy}]}},
            "Runner": {"Type": "AWS::Lambda::Function", "DependsOn": ["RunnerLogs"], "Properties": {
                "FunctionName": n["fn"], "Runtime": "python3.12", "Handler": "index.handler",
                "Role": {"Fn::GetAtt": ["RunnerRole", "Arn"]}, "Timeout": 900, "MemorySize": 1769,
                "Environment": {"Variables": {"SUBS_TABLE": {"Ref": "SubsTable"}, "QUOTA_TABLE": {"Ref": "QuotaTable"},
                                              "EXPECTED_ACCOUNT": ACCOUNT, "EXPECTED_REGION": REGION,
                                              "RUN_PREFIX": n["stack"]}},
                "Code": {"ZipFile": PLACEHOLDER}, "Tags": tags}},
        },
        "Outputs": {k: {"Value": {"Ref": v}} for k, v in
                    (("SubsTable", "SubsTable"), ("QuotaTable", "QuotaTable"), ("Runner", "Runner"))},
    }


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    json.dump(template(*sys.argv[1:]), sys.stdout, indent=2)
