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
        for forbidden in ("ApiGateway", "EventSourceMapping", "AWS::Events", "AWS::SQS", "AWS::S3", "ssm:", "sqs:",
                          "Lambda::Permission", "flight-fare-notification", "flight-lambda-role"):
            self.assertNotIn(forbidden, self.text)

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
