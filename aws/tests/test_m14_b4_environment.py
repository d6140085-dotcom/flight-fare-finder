"""Static safety checks for the B4 test environment (stdlib only; contacts nothing)."""
import importlib.util
import json
import sys
import unittest
from pathlib import Path

AWS = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestEnvTemplate(unittest.TestCase):
    def setUp(self):
        self.b = load(AWS / "integration" / "build_test_env.py", "b4env")
        self.t = self.b.template("b4test01")
        self.text = json.dumps(self.t)

    def test_only_temporary_resources(self):
        self.assertEqual(sorted(r["Type"] for r in self.t["Resources"].values()), [
            "AWS::DynamoDB::Table", "AWS::DynamoDB::Table", "AWS::IAM::Role", "AWS::Lambda::Function",
            "AWS::Logs::LogGroup"])
        for r in self.t["Resources"].values():
            if "DeletionPolicy" in r:
                self.assertEqual(r["DeletionPolicy"], "Delete")
        tables = [r["Properties"]["TableName"] for r in self.t["Resources"].values() if r["Type"] == "AWS::DynamoDB::Table"]
        self.assertEqual(tables, ["flight-m14-it-b4test01-subscriptions", "flight-m14-it-b4test01-quota"])
        for forbidden in ("ApiGateway", "EventSourceMapping", "AWS::Events", "AWS::SQS", "AWS::S3",
                          "Lambda::Permission", "flight-fare-notification", "flight-lambda-role"):
            self.assertNotIn(forbidden, self.text)
        allows = json.dumps([s for s in self.policy() if s["Effect"] == "Allow"])
        for forbidden in ("ssm:", "sqs:", "lambda:", "events:", "apigateway:", "ses:", "iam:", "s3:"):
            self.assertNotIn(forbidden, allows)

    def policy(self):
        return self.t["Resources"]["RunnerRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]

    def test_explicit_denies(self):
        by_sid = {s["Sid"]: s for s in self.policy()}
        self.assertTrue(all(s["Effect"] == "Deny" for k, s in by_sid.items()
                            if k not in ("TestTablesOnly", "OwnLogsOnly")))
        for svc in ("sqs:*", "lambda:*", "events:*", "scheduler:*", "apigateway:*", "execute-api:*", "ssm:*", "ses:*",
                    "iam:*", "sts:*", "s3:*", "secretsmanager:*", "sns:*", "cloudformation:*", "kms:*"):
            self.assertIn(svc, by_sid["NoOtherServices"]["Action"])
        self.assertEqual(by_sid["NoOtherServices"]["Resource"], "*")
        self.assertEqual(sorted(by_sid["NothingElseAtAll"]["NotAction"]),
                         ["dynamodb:*", "logs:CreateLogStream", "logs:PutLogEvents"])
        self.assertEqual(len(by_sid["NoTableButTheTestTables"]["NotResource"]), 4)
        self.assertEqual(by_sid["SydneyOnly"]["Condition"], {"StringNotEquals": {"aws:RequestedRegion": "ap-southeast-2"}})
        env = self.t["Resources"]["Runner"]["Properties"]["Environment"]["Variables"]
        self.assertEqual((env["EXPECTED_ACCOUNT"], env["EXPECTED_REGION"], env["RUN_PREFIX"]),
                         ("379959319681", "ap-southeast-2", "flight-m14-it-b4test01"))

    def test_least_privilege_and_production_denied(self):
        stmts = self.t["Resources"]["RunnerRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        allow = [s for s in stmts if s["Effect"] == "Allow"]
        for s in allow:
            self.assertNotIn("*", json.dumps(s["Action"]))
            self.assertNotIn('"Resource": "*"', json.dumps(s))
        ddb = next(s for s in allow if s["Sid"] == "TestTablesOnly")
        self.assertFalse({"dynamodb:Scan", "dynamodb:DeleteTable", "dynamodb:CreateTable", "dynamodb:UpdateTable"} & set(ddb["Action"]))
        deny = next(s for s in stmts if s["Effect"] == "Deny")
        for t in ("subscriptions", "subscription_quota", "notification_history"):
            self.assertIn(f"arn:aws:dynamodb:ap-southeast-2:379959319681:table/{t}", deny["Resource"])
        trust = self.t["Resources"]["RunnerRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
        self.assertEqual(trust["Principal"], {"Service": "lambda.amazonaws.com"})

    def test_run_id_is_validated(self):
        for bad in ("", "UPPER123", "a", "x" * 13, "../x", "prod-1"):
            with self.assertRaises(ValueError):
                self.b.template(bad)


class RunnerGuards(unittest.TestCase):
    def test_refuses_production_and_mismatched_tables(self):
        import importlib.util as iu
        if iu.find_spec("botocore") is None:  # real SDK absent (other modules may stub boto3)
            self.skipTest("boto3/botocore not installed")
        for p in (AWS / "integration", AWS / "lambdas" / "flight-subscriptions-api"):
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))
        r = load(AWS / "integration" / "runner" / "index.py", "b4runner")
        for subs, quota in (("subscriptions", "subscription_quota"), ("flight-m14-it-abc123-subscriptions", "notification_history"),
                            ("flight-m14-it-abc123-subscriptions", "flight-m14-it-zzz999-quota"), (None, None)):
            with self.assertRaises(RuntimeError):
                r.check_tables(subs, quota)
        r.check_tables("flight-m14-it-abc123-subscriptions", "flight-m14-it-abc123-quota")

        p = "flight-m14-it-abc123"
        good_arn = f"arn:aws:lambda:ap-southeast-2:379959319681:function:{p}-runner"
        env = {"EXPECTED_ACCOUNT": "379959319681", "EXPECTED_REGION": "ap-southeast-2",
               "AWS_REGION": "ap-southeast-2", "RUN_PREFIX": p}
        tables = (f"{p}-subscriptions", f"{p}-quota")
        r.check_environment(good_arn, env, *tables)
        bad = [(good_arn.replace("ap-southeast-2", "us-east-1"), env, tables),
               (good_arn.replace("379959319681", "111111111111"), env, tables),
               ("arn:aws:lambda:ap-southeast-2:379959319681:function:flight-fare-notification", env, tables),
               (None, env, tables),
               (good_arn, {**env, "AWS_REGION": "us-east-1"}, tables),
               (good_arn, {**env, "RUN_PREFIX": "flight-m14-it-zzz999"}, tables),
               (good_arn, env, ("flight-m14-it-zzz999-subscriptions", "flight-m14-it-zzz999-quota"))]
        for arn, e, t in bad:
            with self.assertRaises(RuntimeError):
                r.check_environment(arn, e, *t)

        class FakeDdb:
            def __init__(self, arn): self.arn = arn
            def describe_table(self, TableName): return {"Table": {"TableArn": self.arn.format(TableName)}}
        r.check_table_arns(FakeDdb("arn:aws:dynamodb:ap-southeast-2:379959319681:table/{}"), *tables)
        for wrong in ("arn:aws:dynamodb:us-east-1:379959319681:table/{}", "arn:aws:dynamodb:ap-southeast-2:222222222222:table/{}"):
            with self.assertRaises(RuntimeError):
                r.check_table_arns(FakeDdb(wrong), *tables)


class WorkflowIsManualOnly(unittest.TestCase):
    def test_dispatch_only_and_no_aws_credentials(self):
        text = (AWS.parent / ".github" / "workflows" / "m14-dynamodb-local.yml").read_text()
        on = text[text.index("\non:"):text.index("\npermissions:")]
        self.assertEqual(on.strip(), "on:\n  workflow_dispatch:")
        for forbidden in ("id-token", "aws-actions/configure-aws-credentials", "secrets.", "role-to-assume"):
            self.assertNotIn(forbidden, text)
        self.assertIn("AWS_ACCESS_KEY_ID: testing", text)


if __name__ == "__main__":
    unittest.main()
