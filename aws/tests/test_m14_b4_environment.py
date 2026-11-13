"""Static safety checks for the B4 test environment (stdlib only; contacts nothing)."""
import fnmatch
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
        allowed_actions = [a for s in self.policy() if s["Effect"] == "Allow" for a in
                           (s["Action"] if isinstance(s["Action"], list) else [s["Action"]])]
        self.assertEqual(sorted({a.split(":")[0] for a in allowed_actions}), ["dynamodb", "kms", "logs"])
        self.assertEqual([a for a in allowed_actions if a.startswith("kms:")], ["kms:Decrypt"])

    def policy(self):
        return self.t["Resources"]["RunnerRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]

    def test_explicit_denies(self):
        by_sid = {s["Sid"]: s for s in self.policy()}
        self.assertTrue(all(s["Effect"] == "Deny" for k, s in by_sid.items()
                            if k not in ("TestTablesOnly", "OwnLogsOnly", "OwnEnvironmentDecryptOnly")))
        for svc in ("sqs:*", "lambda:*", "events:*", "scheduler:*", "apigateway:*", "execute-api:*", "ssm:*", "ses:*",
                    "iam:*", "sts:*", "s3:*", "secretsmanager:*", "sns:*", "cloudformation:*"):
            self.assertIn(svc, by_sid["NoOtherServices"]["Action"])
        # KMS is no longer blanket-denied there (it blocked Lambda's own env decryption, Stage C
        # 2026-11-13); its own statements below allow exactly one decrypt and deny everything else.
        self.assertNotIn("kms:*", by_sid["NoOtherServices"]["Action"])
        self.assertEqual(by_sid["NoOtherServices"]["Resource"], "*")
        self.assertEqual(sorted(by_sid["NothingElseAtAll"]["NotAction"]),
                         ["dynamodb:*", "kms:Decrypt", "logs:CreateLogStream", "logs:PutLogEvents"])
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


def _list(v):
    return v if isinstance(v, list) else [v]


def _match(patterns, value, case_insensitive):
    f = (lambda x: x.lower()) if case_insensitive else (lambda x: x)
    return any(fnmatch.fnmatchcase(f(value), f(p)) for p in _list(patterns))


def _condition(cond, ctx):
    for op, pairs in (cond or {}).items():
        for key, want in pairs.items():
            have, want = ctx.get(key), _list(want)
            if op == "StringEquals":
                ok = have is not None and have in want
            elif op == "StringNotEquals":
                ok = have is None or have not in want          # absent key: true (IAM semantics)
            elif op == "Null":
                ok = (have is None) == (want[0] == "true")
            else:
                raise AssertionError(f"evaluator does not model {op}")
            if not ok:
                return False
    return True


def evaluate(policy, action, resource, ctx=None):
    """Identity-policy evaluation for the operators this policy uses (explicit deny > allow >
    implicit deny). Key policies, SCPs and session policies are out of scope (see the docs)."""
    ctx = {"aws:RequestedRegion": "ap-southeast-2", **(ctx or {})}
    def applies(s):
        a = _match(s["Action"], action, True) if "Action" in s else not _match(s["NotAction"], action, True)
        r = _match(s["Resource"], resource, False) if "Resource" in s else not _match(s["NotResource"], resource, False)
        return a and r and _condition(s.get("Condition"), ctx)
    hits = [s for s in policy["Statement"] if applies(s)]
    if any(s["Effect"] == "Deny" for s in hits):
        return "explicitDeny"
    return "allowed" if hits else "implicitDeny"


class RunnerPolicyEvaluation(unittest.TestCase):
    """The resolved runner policy, case by case (the same cases are run through the AWS IAM
    policy simulator before every change set — B4 plan §7.10)."""
    RUN = "b4test01"
    KEY = "arn:aws:kms:ap-southeast-2:379959319681:key/76477f4c-aaca-47e9-aadd-33b0f21fffb9"
    FN = "arn:aws:lambda:ap-southeast-2:379959319681:function:flight-m14-it-b4test01-runner"

    def setUp(self):
        self.b = load(AWS / "integration" / "build_test_env.py", "b4env")
        self.p = self.b.runner_policy(self.RUN)

    def ev(self, action, resource, **ctx):
        return evaluate(self.p, action, resource, ctx)

    def test_lambda_init_decrypt_is_allowed(self):
        # As in CloudTrail 2026-11-13: the role's own session, no kms:ViaService, the
        # aws:lambda:FunctionArn encryption context, the aws/lambda key.
        ctx = {"kms:EncryptionContext:aws:lambda:FunctionArn": self.FN}
        self.assertEqual(self.ev("kms:Decrypt", self.KEY, **ctx), "allowed")
        self.assertEqual(self.ev("kms:Decrypt", self.KEY, **ctx, **{"kms:ViaService": "lambda.ap-southeast-2.amazonaws.com"}), "allowed")

    def test_every_other_kms_use_is_explicitly_denied(self):
        ctx = {"kms:EncryptionContext:aws:lambda:FunctionArn": self.FN}
        other_fn = "arn:aws:lambda:ap-southeast-2:379959319681:function:flight-fare-notification"
        cases = [
            ("kms:Decrypt", self.KEY, {}),                                                       # direct, no context
            ("kms:Decrypt", self.KEY, {"kms:EncryptionContext:aws:lambda:FunctionArn": other_fn}),  # another function's secrets
            ("kms:Decrypt", self.KEY, {"kms:EncryptionContext:LambdaFunctionName": "x"}),         # helper-style context
            ("kms:Decrypt", self.KEY.replace("76477f4c", "11111111"), ctx),                     # another key
            ("kms:Decrypt", self.KEY, {**ctx, "kms:ViaService": "s3.ap-southeast-2.amazonaws.com"}),      # via another service
            ("kms:Decrypt", self.KEY, {**ctx, "kms:ViaService": "secretsmanager.ap-southeast-2.amazonaws.com"}),
            ("kms:Decrypt", self.KEY, {**ctx, "aws:RequestedRegion": "us-east-1"}),             # another region
        ] + [(a, self.KEY, ctx) for a in ("kms:Encrypt", "kms:GenerateDataKey", "kms:ReEncryptFrom", "kms:ReEncryptTo",
                                           "kms:CreateGrant", "kms:DescribeKey", "kms:ListGrants", "kms:PutKeyPolicy",
                                           "kms:ScheduleKeyDeletion", "kms:CreateKey", "kms:Sign")]
        for action, resource, c in cases:
            self.assertEqual(evaluate(self.p, action, resource, c), "explicitDeny", (action, resource, c))

    def test_dynamodb_only_the_two_test_tables(self):
        test = [f"arn:aws:dynamodb:ap-southeast-2:379959319681:table/flight-m14-it-{self.RUN}-{t}" for t in ("subscriptions", "quota")]
        for t in test:
            for a in ("dynamodb:GetItem", "dynamodb:Query", "dynamodb:PutItem", "dynamodb:UpdateItem",
                      "dynamodb:DeleteItem", "dynamodb:ConditionCheckItem", "dynamodb:DescribeTable"):
                self.assertEqual(self.ev(a, t), "allowed", (a, t))
            for a in ("dynamodb:Scan", "dynamodb:DeleteTable", "dynamodb:UpdateTable", "dynamodb:CreateBackup"):
                self.assertNotEqual(self.ev(a, t), "allowed", (a, t))
        for prod in ("subscriptions", "subscription_quota", "notification_history"):
            arn = f"arn:aws:dynamodb:ap-southeast-2:379959319681:table/{prod}"
            for a in ("dynamodb:GetItem", "dynamodb:Scan", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:Query"):
                self.assertEqual(self.ev(a, arn), "explicitDeny", (a, prod))
            self.assertEqual(self.ev("dynamodb:Query", arn + "/index/x"), "explicitDeny")
        self.assertEqual(self.ev("dynamodb:GetItem", "arn:aws:dynamodb:ap-southeast-2:379959319681:table/flight-m14-it-other1-quota"),
                         "explicitDeny")

    def test_logs_only_its_own_group(self):
        own = f"arn:aws:logs:ap-southeast-2:379959319681:log-group:/aws/lambda/flight-m14-it-{self.RUN}-runner"
        for a in ("logs:CreateLogStream", "logs:PutLogEvents"):
            self.assertEqual(self.ev(a, own + ":log-stream:2026/11/13/[$LATEST]abc"), "allowed")
            self.assertEqual(self.ev(a, own + ":*"), "allowed")
            self.assertEqual(self.ev(a, "arn:aws:logs:ap-southeast-2:379959319681:log-group:/aws/lambda/flight-parser:log-stream:x"),
                             "explicitDeny")
        self.assertEqual(self.ev("logs:DeleteLogGroup", own + ":*"), "explicitDeny")

    def test_other_services_stay_denied(self):
        for a in ("sqs:SendMessage", "sqs:ReceiveMessage", "lambda:InvokeFunction", "events:EnableRule", "events:PutRule",
                  "scheduler:CreateSchedule", "apigateway:POST", "execute-api:Invoke", "ssm:GetParameter",
                  "secretsmanager:GetSecretValue", "ses:SendEmail", "sns:Publish", "iam:PassRole", "sts:AssumeRole",
                  "s3:GetObject", "cloudformation:DeleteStack", "ec2:RunInstances"):
            self.assertEqual(self.ev(a, "*"), "explicitDeny", a)


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


class RunnerPreflight(unittest.TestCase):
    """{"preflight": true}: env-variable checks run, then the handler returns before creating any
    AWS client — no DynamoDB call, no scenario."""

    def setUp(self):
        import importlib.util as iu
        if iu.find_spec("botocore") is None:
            self.skipTest("boto3/botocore not installed")
        for p in (AWS / "integration", AWS / "lambdas" / "flight-subscriptions-api"):
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))
        self.r = load(AWS / "integration" / "runner" / "index.py", "b4runner_pf")
        self.prefix = "flight-m14-it-abc123"
        self.env = {"SUBS_TABLE": f"{self.prefix}-subscriptions", "QUOTA_TABLE": f"{self.prefix}-quota",
                    "EXPECTED_ACCOUNT": "379959319681", "EXPECTED_REGION": "ap-southeast-2",
                    "AWS_REGION": "ap-southeast-2", "RUN_PREFIX": self.prefix}

        class Ctx:
            invoked_function_arn = f"arn:aws:lambda:ap-southeast-2:379959319681:function:{self.prefix}-runner"
            aws_request_id = "00000000-0000-0000-0000-000000000000"
        self.ctx = Ctx()

    def run_handler(self, event, env=None):
        from unittest import mock
        import io, contextlib
        out = io.StringIO()
        with mock.patch.dict("os.environ", env or self.env, clear=True), \
             mock.patch.object(self.r.boto3, "client", side_effect=AssertionError("AWS client created")) as client, \
             mock.patch.object(self.r.sc, "run_all", side_effect=AssertionError("scenarios started")) as run_all, \
             contextlib.redirect_stdout(out):
            result = self.r.handler(event, self.ctx)
        return result, out.getvalue(), client, run_all

    def test_preflight_touches_nothing(self):
        result, printed, client, run_all = self.run_handler({"preflight": True})
        self.assertEqual(result["preflight"], "ok")
        self.assertEqual((result["subs_table"], result["quota_table"]), (self.env["SUBS_TABLE"], self.env["QUOTA_TABLE"]))
        self.assertEqual(json.loads(printed), result)              # the log line written
        client.assert_not_called()
        run_all.assert_not_called()

    def test_preflight_still_enforces_the_guards(self):
        with self.assertRaises(RuntimeError):
            self.run_handler({"preflight": True}, {**self.env, "SUBS_TABLE": "subscriptions"})
        with self.assertRaises(RuntimeError):
            self.run_handler({"preflight": True}, {**self.env, "AWS_REGION": "us-east-1"})

    def test_only_an_explicit_true_selects_preflight(self):
        for event in ({"preflight": "true"}, {"preflight": 1}, {}, None):
            with self.assertRaises(AssertionError):  # went on to create the DynamoDB client
                self.run_handler(event)


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
