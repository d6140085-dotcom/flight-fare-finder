"""Tests for the M1.4 flight-subscriptions-api Lambda (Phase 1B, code only).

* Unit tests (stdlib only): months, config, validation, messages, Supabase auth.
* Integration tests: the real handler, store and quota protocol against a **local**
  DynamoDB-compatible endpoint (see ddb_local.py). They skip when boto3/moto are not installed:
    pip install -r aws/requirements-test.txt
* Concurrency tests: ≥ 50 simultaneous requests per scenario; afterwards every user has
  ≤ 10 rows and the quota record's count equals the true number of rows.

No test reaches AWS: endpoints are localhost-only and credentials are fake.
Run: python3 -m unittest discover -s aws/tests -v
"""
import base64
import importlib.util
import io
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ddb_local  # noqa: E402  (must come before anything imports boto3)

API_DIR = Path(__file__).resolve().parents[1] / "lambdas" / "flight-subscriptions-api"
sys.path.insert(0, str(API_DIR))
from subsapi import config as config_mod  # noqa: E402
from subsapi import months, subkeys, validation  # noqa: E402
from subsapi.auth import resolve_email  # noqa: E402
from subsapi.errors import ApiError  # noqa: E402
from subsapi.messages import message  # noqa: E402

if ddb_local.boto3 is not None:
    # Bind the real boto3-backed modules now: other test modules swap boto3 for stubs at run time.
    from subsapi import service as service_mod  # noqa: E402
    from subsapi import store as store_mod  # noqa: E402

CFG = config_mod.load({})
NOW = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)  # 11:00 Taipei; window 2026-12 … 2027-11
USER = "user@example.com"
FRESH = object()  # test helpers: "send the row's current version, like an up-to-date page"
OTHER = "other@example.com"
DESTS = ["OSA", "NGO", "FUK", "OKA", "SPK", "KMJ", "PUS", "CJU", "HKG", "MFM", "BKK", "CNX", "HKT",
         "SIN", "KUL", "PEN", "BKI", "MNL", "CEB", "SGN", "HAN", "DAD", "DPS", "LON", "PAR", "FRA",
         "MUC", "AMS", "ZRH", "VIE", "ROM", "MIL", "NYC", "LAX", "SFO", "SEA", "SYD", "MEL", "AKL",
         "ZQN", "BER", "GVA", "VCE", "FLR", "MAD", "BCN", "LIS", "OPO", "PRG", "BUD", "CPH", "STO",
         "OSL", "HEL", "DUB", "ATH", "IST", "CHI", "BOS", "WAS"]


def fid(dest, month="2027-01", origin="TPE"):
    """Public subscription ID of a fixed-month row (R-MM-1): "TPE-LON_2027-01"."""
    return f"{origin}-{dest}_{month}"


def fkey(dest, month="2027-01", origin="TPE"):
    """DynamoDB sort key of a fixed-month row: "TPE-LON#2027-01"."""
    return f"{origin}-{dest}#{month}"


# =============================================================================================
# Unit tests (no boto3 needed)
# =============================================================================================
class MonthTests(unittest.TestCase):
    def test_taipei_calendar(self):
        self.assertEqual(months.current_month(datetime(2026, 11, 30, 16, 0, tzinfo=timezone.utc)), "2026-12")
        self.assertEqual(months.current_month(datetime(2026, 11, 30, 15, 59, tzinfo=timezone.utc)), "2026-11")

    def test_window_and_arithmetic(self):
        sel = months.selectable_months(NOW)
        self.assertEqual((len(sel), sel[0], sel[-1]), (12, "2026-12", "2027-11"))
        self.assertEqual(months.add_months("2026-12", 1), "2027-01")
        self.assertEqual(months.add_months("2027-01", -1), "2026-12")

    def test_effective_month_and_expiry(self):
        self.assertEqual(months.effective_month(None, NOW), "2026-12")  # legacy rolling
        self.assertEqual(months.effective_month("2027-04", NOW), "2027-04")
        self.assertTrue(months.is_expired("2026-10", NOW))
        self.assertFalse(months.is_expired("2026-11", NOW))  # the current month still counts
        self.assertFalse(months.is_expired(None, datetime(2040, 1, 1, tzinfo=timezone.utc)))


class ConfigTests(unittest.TestCase):
    def test_catalog_and_policy(self):
        self.assertEqual(sorted(CFG.origins), ["KHH", "RMQ", "TPE"])
        self.assertEqual(len(CFG.destinations), 95)
        self.assertEqual((CFG.max_subscriptions, CFG.min_target, CFG.max_target), (10, 1, 1_000_000))
        self.assertEqual((CFG.first_offset_months, CFG.window_months), (1, 12))

    def test_env_override_is_bounded(self):
        self.assertEqual(config_mod.load({"MAX_SUBSCRIPTIONS": "3"}).max_subscriptions, 3)
        for bad in ("0", "101"):
            with self.assertRaises(ValueError):
                config_mod.load({"MAX_SUBSCRIPTIONS": bad})


class ValidationTests(unittest.TestCase):
    def code(self, fn, *args):
        with self.assertRaises(ApiError) as e:
            fn(*args)
        return e.exception.code

    def test_create_order_and_values(self):
        ok = {"origin": "tpe", "destination": " lon ", "departure_month": "2027-03", "target_price": 28000}
        self.assertEqual(validation.create_body(CFG, ok, NOW), ("TPE", "LON", "2027-03", 28000))
        c = lambda **kw: self.code(validation.create_body, CFG, {**ok, **kw}, NOW)  # noqa: E731
        self.assertEqual(self.code(validation.create_body, CFG, [], NOW), "invalid_json")
        self.assertEqual(c(origin=None, destination=None), "origin_required")
        self.assertEqual(c(origin="NRT"), "unsupported_origin")
        self.assertEqual(c(destination=""), "destination_required")
        self.assertEqual(c(destination="JNB"), "unsupported_destination")
        self.assertEqual(c(destination="HND"), "unsupported_destination")  # covered by TYO
        self.assertEqual(c(destination="TPE"), "unsupported_destination")
        self.assertEqual(c(origin="KHH", destination="KHH"), "unsupported_destination")
        for m in (None, "2026-11", "2027-12", "2027-13", "2027-3", "rolling"):
            self.assertEqual(c(departure_month=m), "invalid_departure_month", m)
        for t in (0, 1_000_001, "9000", 9000.0, True, None):
            self.assertEqual(c(target_price=t), "invalid_target_price", t)

    def test_every_catalog_destination_is_accepted(self):
        for code in CFG.destinations:
            body = {"origin": "TPE", "destination": code, "departure_month": "2026-12", "target_price": 1}
            self.assertEqual(validation.create_body(CFG, body, NOW)[1], code)

    def test_patched_month(self):
        pm = validation.patched_month
        self.assertIsNone(pm(CFG, None, None, NOW))  # rolling stays rolling
        self.assertEqual(pm(CFG, "2026-09", "2026-09", NOW), "2026-09")  # unchanged expired month
        self.assertEqual(pm(CFG, "2027-02", None, NOW), "2027-02")  # rolling → fixed
        self.assertEqual(self.code(pm, CFG, None, "2027-01", NOW), "invalid_departure_month")
        self.assertEqual(self.code(pm, CFG, "2026-08", "2026-09", NOW), "invalid_departure_month")

    def test_patch_shape(self):
        pb = validation.patch_body
        self.assertEqual(self.code(pb, {}), "empty_patch")
        self.assertEqual(self.code(pb, {"email": OTHER}), "empty_patch")
        self.assertEqual(self.code(pb, {"target_price": 1, "colour": "red"}), "unknown_field")
        self.assertEqual(self.code(pb, {"origin": "TPE"}), "route_change_incomplete")
        self.assertEqual(self.code(pb, {"alerts_paused": "yes"}), "invalid_alerts_paused")
        self.assertEqual(self.code(pb, None), "invalid_json")
        self.assertEqual(self.code(validation.route_param, "tpe-tyo"), "invalid_route")
        self.assertEqual(validation.route_param("TPE-TYO"), "TPE-TYO")

    def test_subscription_id_param(self):
        ip = validation.id_param
        self.assertEqual(ip("TPE-TYO"), "TPE-TYO")  # legacy rolling row: unchanged ID
        self.assertEqual(ip("KHH-TYO_2027-01"), "KHH-TYO#2027-01")
        for bad in ("KHH-TYO#2027-01", "KHH-TYO%232027-01", "KHH-TYO_2027-13", "KHH-TYO_2027-1", "khh-tyo",
                    "KHH-TYO_", "KHH-TYO_2027-01_2027-02", "../KHH-TYO", "", None, 1):
            self.assertEqual(self.code(ip, bad), "invalid_route", bad)

    def test_legacy_target_keeps_m1_rules(self):
        self.assertEqual(validation.legacy_target_price(CFG, 9000.0), 9000)
        for t in (9000.5, True, "9000", 0):
            self.assertEqual(self.code(validation.legacy_target_price, CFG, t), "invalid_target_price")


class MessageTests(unittest.TestCase):
    def test_traditional_chinese_messages(self):
        self.assertEqual(message("duplicate_subscription", CFG, NOW, {"route": "TPE-LON", "departure_month": "2027-03"}),
                         "你已經訂閱過「台北 → 倫敦」 2027 年 3 月出發的航線，同一航線同一月份只能訂閱一次。")
        self.assertEqual(message("duplicate_subscription", CFG, NOW, {"route": "TPE-TYO", "departure_month": None}),
                         "你已經訂閱過「台北 → 東京」（每月滾動）出發的航線，同一航線同一月份只能訂閱一次。")
        self.assertEqual(message("invalid_departure_month", CFG, NOW),
                         "請選擇 2026 年 12 月 到 2027 年 11 月 之間的出發月份。")
        self.assertIn("最多 10 筆航線訂閱（已暫停與已過期的訂閱也算在內）", message("limit_reached", CFG, NOW))
        self.assertIn("1,000,000", message("invalid_target_price", CFG, NOW))


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


ENV = {"SUPABASE_URL": "https://example.supabase.co", "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_test"}


def apigw(route_key, body=None, token="good", path=None, query=None, raw=None, b64=False):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    text = raw if raw is not None else ("" if body is None else json.dumps(body))
    if b64:
        text = base64.b64encode(text.encode()).decode()
    return {"version": "2.0", "routeKey": route_key, "requestContext": {"http": {}}, "headers": headers,
            "body": text, "isBase64Encoded": b64, "pathParameters": path, "queryStringParameters": query}


class AuthTests(unittest.TestCase):
    def test_email_comes_only_from_supabase(self):
        seen = {}

        def opener(req, timeout):
            seen.update(url=req.full_url, headers=dict(req.header_items()), timeout=timeout)
            return FakeResponse(json.dumps({"email": "  User@Example.com "}).encode())

        email = resolve_email(apigw("GET /subscriptions", {"email": OTHER}), ENV, opener)
        self.assertEqual(email, USER)
        self.assertEqual(seen["url"], "https://example.supabase.co/auth/v1/user")
        self.assertEqual(seen["headers"]["Apikey"], "sb_publishable_test")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer good")
        self.assertEqual(seen["timeout"], 5)

    def test_rejections(self):
        def deny(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)

        self.assertIsNone(resolve_email(apigw("GET /subscriptions", token=None), ENV, deny))
        self.assertIsNone(resolve_email(apigw("GET /subscriptions", token=" "), ENV, deny))
        self.assertIsNone(resolve_email(apigw("GET /subscriptions"), ENV, deny))
        self.assertIsNone(resolve_email(apigw("GET /subscriptions"), ENV,
                                        lambda r, timeout: FakeResponse(b'{"id": "x"}')))

    def test_direct_invoke_only_when_enabled(self):
        event = {"routeKey": "GET /subscriptions", "test_email": USER}
        self.assertIsNone(resolve_email(event, ENV))  # disabled by default
        on = {**ENV, "ALLOW_DIRECT_TEST_INVOKE": "true", "AWS_LAMBDA_FUNCTION_NAME": "flight-subscriptions-api-test"}
        self.assertEqual(resolve_email(event, on), USER)
        for flag in ("TRUE", "True", "1", "yes", " true", ""):
            self.assertIsNone(resolve_email(event, {**on, "ALLOW_DIRECT_TEST_INVOKE": flag}), flag)

    def test_production_function_never_accepts_test_mode(self):
        prod = {**ENV, "ALLOW_DIRECT_TEST_INVOKE": "true", "AWS_LAMBDA_FUNCTION_NAME": "flight-subscriptions-api"}
        self.assertIsNone(resolve_email({"test_email": USER}, prod))

    def test_test_mode_cannot_act_as_a_real_user(self):
        on = {**ENV, "ALLOW_DIRECT_TEST_INVOKE": "true", "AWS_LAMBDA_FUNCTION_NAME": "x-test"}
        for email in ("someone@mail-provider.io", "user@example.com.evil.io", "user@notexample.com", "@example.com",
                      "example.com", "a@b@example.com", "a b@example.com", "a@.example.com", None, 123):
            self.assertIsNone(resolve_email({"test_email": email}, on), email)
        for email in ("a@example.org", "b@example.net", "c@qa.test", "D@Example.COM"):
            self.assertIsNotNone(resolve_email({"test_email": email}, on), email)

    def test_api_gateway_events_can_never_use_test_mode(self):
        on = {**ENV, "ALLOW_DIRECT_TEST_INVOKE": "true", "AWS_LAMBDA_FUNCTION_NAME": "x-test"}

        def deny(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)

        for ctx in ({"http": {}}, {}, None):
            event = {**apigw("GET /subscriptions"), "requestContext": ctx, "test_email": USER}
            self.assertIsNone(resolve_email(event, on, deny))  # goes to Supabase, which says no
        event = {**apigw("GET /subscriptions", token=None), "test_email": USER}
        self.assertIsNone(resolve_email(event, on, deny))
        self.assertIsNone(resolve_email("not-a-dict", on, deny))

    def test_refuses_secret_keys_and_plain_http(self):
        for bad in ({**ENV, "SUPABASE_PUBLISHABLE_KEY": "sb_secret_x"}, {**ENV, "SUPABASE_URL": "http://x"}):
            with self.assertRaises(RuntimeError):
                resolve_email(apigw("GET /subscriptions"), bad, lambda r, timeout: None)


class SourceHygieneTests(unittest.TestCase):
    def test_api_never_references_notification_paths(self):
        for p in API_DIR.rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            for forbidden in ("notification_history", "sqs", "ssm", "secretsmanager", "flight-fare-notification"):
                self.assertNotIn(forbidden, text.lower(), f"{p}: {forbidden}")


class PackagingTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())
        spec = importlib.util.spec_from_file_location(
            "pkg_subs", API_DIR.parents[1] / "scripts" / "package_subscriptions_api.py")
        self.pkg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.pkg)

    def test_zip_is_deterministic_and_self_contained(self):
        import zipfile

        a, b = self.pkg.build(self.tmp / "a"), self.pkg.build(self.tmp / "b")
        self.assertEqual(a["sha256"], b["sha256"])
        with zipfile.ZipFile(a["zip"]) as z:
            names = z.namelist()
            self.assertEqual(names, sorted(names))
            self.assertIn("index.py", names)
            self.assertIn("subsapi/store.py", names)
            for cfg in ("airports.json", "subscription-policy.json"):
                self.assertEqual(z.read(f"subsapi/config/{cfg}"),
                                 (API_DIR.parents[1] / "config" / cfg).read_bytes())
            self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc") or "test" in n])
            self.assertTrue(all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in z.infolist()))

    def test_zip_contains_no_secrets(self):
        import re
        import zipfile

        out = self.pkg.build(self.tmp / "s")
        patterns = [r"sb_secret_", r"sb_publishable_[A-Za-z0-9_-]{8,}", r"AKIA[0-9A-Z]{16}", r"ghp_[A-Za-z0-9]{20}",
                    r"github_pat_", r"\bre_[A-Za-z0-9]{16,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
                    r"aws_secret_access_key", r"@(gmail|yahoo|hotmail|outlook)\.com"]
        with zipfile.ZipFile(out["zip"]) as z:
            self.assertFalse([n for n in z.namelist() if n.startswith(".") or "/." in n or n.endswith(".env")])
            for name in z.namelist():
                text = z.read(name).decode("utf-8")
                for pat in patterns:
                    self.assertIsNone(re.search(pat, text), f"{name}: {pat}")

    def test_packaged_config_is_what_the_handler_loads(self):
        import zipfile

        out = self.pkg.build(self.tmp / "c")
        unpacked = self.tmp / "unpacked"
        with zipfile.ZipFile(out["zip"]) as z:
            z.extractall(unpacked)
        with mock.patch.dict(os.environ, {"SUBSAPI_CONFIG_DIR": str(unpacked / "subsapi" / "config")}):
            self.assertEqual(len(config_mod.load().destinations), 95)


class PlanScriptTests(unittest.TestCase):
    def setUp(self):
        self.text = (API_DIR.parents[1] / "scripts" / "plan_m14_subscriptions_api.sh").read_text()

    def test_cannot_execute_or_deploy(self):
        for forbidden in ("execute-change-set", "create-stack", "update-stack", "delete-stack",
                          "cloudformation deploy", "lambda update-function", "eval "):
            self.assertNotIn(forbidden, self.text)

    def test_dry_run_by_default_and_explicit_confirmation(self):
        self.assertIn('if [[ "$MODE" != "--create-change-set" ]]', self.text)
        self.assertIn('[[ "${CONFIRM:-}" == "create-change-set" ]]', self.text)
        dry = self.text.index('if [[ "$MODE" != "--create-change-set" ]]')
        self.assertLess(dry, self.text.index("\nupload\n"))  # nothing runs before the dry-run exit
        self.assertIn("--change-set-type CREATE", self.text)

    def test_write_once_traceable_artifact_from_a_clean_commit(self):
        self.assertIn("--if-none-match '*'", self.text)
        self.assertIn("--checksum-sha256", self.text)
        self.assertIn('KEY="artifacts/flight-subscriptions-api/${COMMIT}/${SHA_HEX}.zip"', self.text)
        self.assertIn("git status --porcelain", self.text)


class TemplateTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "b14", API_DIR.parents[1] / "stacks" / "build_m14_subscriptions_api.py")
        self.b = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.b)
        self.commit, self.sha = "a" * 40, "b" * 64
        self.t = self.b.template("379959319681", self.commit, self.sha, "c2hhMjU2PQ==",
                                 "https://example.supabase.co", "sb_publishable_example")

    def test_no_routes_and_nothing_existing_is_touched(self):
        types = sorted(r["Type"] for r in self.t["Resources"].values())
        self.assertEqual(types, ["AWS::DynamoDB::Table", "AWS::Lambda::Function", "AWS::Logs::LogGroup"])
        text = json.dumps(self.t)
        for forbidden in ("ApiGateway", "Lambda::Permission", "notification_history", "flight-fare-queue",
                          "AWS::Events", "EventSourceMapping", "sb_secret", "AWS::IAM"):
            self.assertNotIn(forbidden, text)

    def test_quota_table_and_function(self):
        q = self.t["Resources"]["QuotaTable"]
        self.assertEqual((q["DeletionPolicy"], q["UpdateReplacePolicy"]), ("Retain", "Retain"))
        self.assertEqual(q["Properties"]["TableName"], "subscription_quota")
        self.assertTrue(q["Properties"]["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"])
        f = self.t["Resources"]["Function"]["Properties"]
        self.assertEqual(f["Role"], "arn:aws:iam::379959319681:role/flight-subscriptions-api-role")
        self.assertEqual(f["Code"], {"S3Bucket": "flight-artifacts-379959319681",
                                     "S3Key": f"artifacts/flight-subscriptions-api/{self.commit}/{self.sha}.zip"})
        env = f["Environment"]["Variables"]
        self.assertEqual((env["SUBSCRIPTIONS_TABLE"], env["QUOTA_TABLE"]), ("subscriptions", "subscription_quota"))
        self.assertNotIn("ALLOW_DIRECT_TEST_INVOKE", env)
        self.assertEqual(f["FunctionName"], "flight-subscriptions-api")  # = auth.PRODUCTION_FUNCTION
        from subsapi.auth import PRODUCTION_FUNCTION
        self.assertEqual(f["FunctionName"], PRODUCTION_FUNCTION)

    def test_artifact_is_traceable_and_immutable(self):
        f = self.t["Resources"]["Function"]["Properties"]
        tags = {t["Key"]: t["Value"] for t in f["Tags"]}
        self.assertEqual((tags["git_commit"], tags["code_sha256"]), (self.commit, "c2hhMjU2PQ=="))
        self.assertIn(self.commit[:12], f["Description"])
        self.assertEqual(self.t["Outputs"]["GitCommit"]["Value"], self.commit)
        for bad_commit, bad_sha in (("abc", self.sha), (self.commit, "zz"), ("A" * 40, self.sha)):
            with self.assertRaises(ValueError):
                self.b.artifact_key(bad_commit, bad_sha)

    def test_rejects_secret_keys(self):
        with self.assertRaises(ValueError):
            self.b.template("1", self.commit, self.sha, "x", "https://x.supabase.co", "sb_secret_abc")
        with self.assertRaises(ValueError):
            self.b.template("1", self.commit, self.sha, "x", "http://x.supabase.co", "sb_publishable_x")

    def test_pipeline_can_create_only_the_quota_table(self):
        policy = json.loads((API_DIR.parents[1] / "iam" / "cfn-exec-policy.json").read_text())
        stmt = next(s for s in policy["Statement"] if s.get("Sid") == "QuotaTableOnly")
        self.assertEqual(stmt["Resource"], "arn:aws:dynamodb:ap-southeast-2:379959319681:table/subscription_quota")
        self.assertFalse([a for a in stmt["Action"] if "Delete" in a or a == "dynamodb:UpdateTable"])


# =============================================================================================
# Integration tests against a local DynamoDB-compatible endpoint
# =============================================================================================
def _load_index():
    spec = importlib.util.spec_from_file_location("flight_subscriptions_api_index", API_DIR / "index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TOKENS = {"good": USER, "other": OTHER}


@unittest.skipUnless(ddb_local.available(), "boto3/moto (or DDB_ENDPOINT) not available")
class LocalDynamoTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.Service, cls.Store = service_mod.Service, store_mod.Store
        cls.client = ddb_local.client()
        cls.index = _load_index()

    def setUp(self):
        self.subs_table, self.quota_table = ddb_local.create_tables(self.client)
        self.now = NOW
        self.store = self.Store(self.client, self.subs_table, self.quota_table)
        self.sleeps = []
        self.svc = self.Service(self.store, CFG, clock=lambda: self.now, sleep=self.sleeps.append)
        self.index.configure(self.svc)
        self.auth = mock.patch.object(self.index, "resolve_email",
                                      lambda event, env: TOKENS.get(self._token(event)))
        self.auth.start()
        self.quiet = mock.patch.object(self.index, "_log", lambda *a, **kw: None)
        self.quiet.start()

    def tearDown(self):
        self.auth.stop()
        self.quiet.stop()
        ddb_local.drop_tables(self.client, self.subs_table, self.quota_table)

    @staticmethod
    def _token(event):
        h = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        return h.get("authorization", "")[7:] or None

    # ---- helpers -----------------------------------------------------------------------
    def call(self, route_key, body=None, **kw):
        r = self.index.handler(apigw(route_key, body, **kw), None)
        return r["statusCode"], (json.loads(r["body"]) if r.get("body") else None)

    def create(self, dest, month="2027-01", target=9000, origin="TPE", token="good"):
        return self.call("POST /subscriptions", {"origin": origin, "destination": dest,
                                                 "departure_month": month, "target_price": target}, token=token)

    def current_version(self, sub_id, email=USER):
        """What an up-to-date page would send: the row's current public version (a well-formed
        unknown token when the row or the ID doesn't exist, so the API answers 404/400)."""
        key = subkeys.key_from_id(sub_id)
        row = self.store.get_row(email, key) if key else None
        return service_mod.Service.version_of(row) if row else "Z" * 22

    def patch(self, sub_id, body, token="good", version=FRESH):
        """PATCH as the page sends it: with the current if_version unless the body already has
        one or version=None (a raw request without it)."""
        if isinstance(body, dict) and "if_version" not in body and version is not None:
            body = {**body, "if_version": self.current_version(sub_id) if version is FRESH else version}
        return self.call("PATCH /subscriptions/{id}", body, path={"id": sub_id}, token=token)

    def delete(self, sub_id, if_version=FRESH):
        if if_version is FRESH:
            if_version = self.current_version(sub_id)
        return self.call("DELETE /subscriptions/{id}", path={"id": sub_id},
                         query={"if_version": if_version} if if_version is not None else None)

    def raw(self, email=USER):
        return {r["route"]: r for r in self.store.list_rows(email)}

    def quota(self, email=USER):
        return self.store.get_quota(email)

    def seed(self, email, route, **fields):
        """Write a row directly, as M1.1 or an earlier M1.4 write would have stored it."""
        o, _, d = route.partition("-")
        item = {"email": email, "route": route, "origin": o, "destination": d, "target_price": 9000,
                "currency": "TWD", "created_at": "2026-10-30T00:00:00+00:00",
                "updated_at": "2026-10-30T00:00:00+00:00", **fields}
        self.client.put_item(TableName=self.subs_table, Item=store_mod.to_item(item))

    def assert_invariants(self, email=USER):
        rows = self.raw(email)
        self.assertLessEqual(len(rows), CFG.max_subscriptions, sorted(rows))
        q = self.quota(email)
        if q is not None:
            self.assertEqual(q["count"], len(rows), "quota count drifted from the true row count")


class SafetyTests(LocalDynamoTestCase):
    def test_endpoint_is_local_and_credentials_are_fake(self):
        from urllib.parse import urlparse
        self.assertIn(urlparse(self.client.meta.endpoint_url).hostname, ddb_local.LOCAL_HOSTS)
        creds = self.client._request_signer._credentials
        self.assertEqual(creds.access_key, "testing")

    def test_only_test_tables_are_touched(self):
        names = self.client.list_tables()["TableNames"]
        self.assertTrue(all(n.startswith("test-") for n in names), names)
        self.assertNotIn("subscriptions", names)
        self.assertNotIn("notification_history", names)


class CrudTests(LocalDynamoTestCase):
    def test_create_and_list(self):
        status, body = self.create("LON", month="2027-03", target=28000)
        self.assertEqual(status, 201)
        self.assertRegex(body["version"], r"^[A-Za-z0-9_-]{22}$")
        self.assertEqual(body | {"created_at": None, "updated_at": None, "version": None}, {
            "id": "TPE-LON_2027-03", "route": "TPE-LON", "origin": "TPE", "destination": "LON",
            "origin_name": {"zh": "台北", "en": "Taipei"}, "destination_name": {"zh": "倫敦", "en": "London"},
            "departure_month": "2027-03", "month_mode": "fixed", "effective_month": "2027-03",
            "status": "active", "target_price": 28000, "currency": "TWD", "alerts_paused": False,
            "plan_name": None, "created_at": None, "updated_at": None, "version": None, "fare_check": None})
        self.assertRegex(body["updated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        status, listing = self.call("GET /subscriptions")
        self.assertEqual(status, 200)
        self.assertEqual([s["route"] for s in listing["subscriptions"]], ["TPE-LON"])
        self.assertEqual(listing["limits"], {"max": 10, "used": 1})
        self.assertEqual(listing["months"]["current"], "2026-11")
        self.assertEqual(listing["months"]["selectable"][0], "2026-12")
        self.assertEqual(self.quota(), {"email": USER, "count": 1, "version": 1, "updated_at": body["updated_at"]})

    def test_worldwide_destinations(self):
        for dest, origin in (("PAR", "TPE"), ("NYC", "KHH"), ("SAO", "TPE"), ("SYD", "RMQ"), ("AKL", "TPE")):
            self.assertEqual(self.create(dest, origin=origin)[0], 201, dest)
        self.assertEqual(len(self.raw()), 5)

    def test_validation_errors_through_the_api(self):
        status, body = self.create("JNB")
        self.assertEqual((status, body["error"]), (400, "unsupported_destination"))
        self.assertEqual(body["message"], "目前不支援這個目的地。")
        self.assertEqual(self.create("LON", month="2026-11")[1]["error"], "invalid_departure_month")
        self.assertEqual(self.create("LON", target=0)[1]["error"], "invalid_target_price")
        self.assertEqual(self.call("POST /subscriptions", raw="{not json")[1]["error"], "invalid_json")
        self.assertEqual(self.call("POST /subscriptions", raw="x" * 5000)[1]["error"], "invalid_json")
        self.assertEqual(self.raw(), {})

    def test_base64_body(self):
        body = {"origin": "TPE", "destination": "LON", "departure_month": "2027-01", "target_price": 1}
        self.assertEqual(self.call("POST /subscriptions", body, b64=True)[0], 201)

    def test_identity_from_session_only(self):
        self.call("POST /subscriptions", {"origin": "TPE", "destination": "LON", "departure_month": "2027-01",
                                          "target_price": 1, "email": OTHER})
        self.assertIn("TPE-LON#2027-01", self.raw(USER))
        self.assertEqual(self.raw(OTHER), {})
        self.assertEqual(self.call("GET /subscriptions", token=None)[0], 401)
        self.assertEqual(self.call("GET /subscriptions", token="forged")[1]["error"], "unauthorized")
        self.assertEqual(self.call("GET /subscriptions", token="other")[1]["subscriptions"], [])

    def test_several_months_of_one_route_are_independent(self):
        """R-MM-1: KHH-TYO for 2027-01, -02 and -03 are three subscriptions, three records."""
        ids = []
        for m in ("2027-01", "2027-02", "2027-03"):
            status, body = self.create("TYO", month=m, origin="KHH", target=9000 + int(m[-1]))
            self.assertEqual(status, 201, m)
            ids.append(body["id"])
        self.assertEqual(ids, ["KHH-TYO_2027-01", "KHH-TYO_2027-02", "KHH-TYO_2027-03"])
        self.assertEqual(sorted(self.raw()), ["KHH-TYO#2027-01", "KHH-TYO#2027-02", "KHH-TYO#2027-03"])
        self.assertEqual(self.quota()["count"], 3)
        listing = self.call("GET /subscriptions")[1]
        self.assertEqual([(s["id"], s["route"], s["departure_month"], s["target_price"])
                          for s in listing["subscriptions"]],
                         [("KHH-TYO_2027-01", "KHH-TYO", "2027-01", 9001), ("KHH-TYO_2027-02", "KHH-TYO", "2027-02", 9002),
                          ("KHH-TYO_2027-03", "KHH-TYO", "2027-03", 9003)])
        self.assertEqual(listing["limits"]["used"], 3)
        # Each one is edited and deleted on its own.
        self.assertEqual(self.patch("KHH-TYO_2027-02", {"alerts_paused": True})[1]["status"], "paused")
        self.assertEqual(self.delete("KHH-TYO_2027-01")[0], 204)
        statuses = {s["id"]: s["status"] for s in self.call("GET /subscriptions")[1]["subscriptions"]}
        self.assertEqual(statuses, {"KHH-TYO_2027-02": "paused", "KHH-TYO_2027-03": "active"})
        self.assertEqual(self.quota()["count"], 2)

    def test_duplicate_route_and_month(self):
        self.create("LON", month="2027-01")
        status, body = self.create("LON", month="2027-01")
        self.assertEqual((status, body["error"]), (409, "duplicate_subscription"))
        self.assertEqual(body["details"], {"route": "TPE-LON", "departure_month": "2027-01"})
        self.assertIn("台北 → 倫敦」 2027 年 1 月", body["message"])
        self.assertEqual(self.quota()["count"], 1)

    def test_unknown_route_key(self):
        self.assertEqual(self.call("PUT /subscriptions")[0], 404)

    def test_internal_errors_are_generic(self):
        with mock.patch.object(self.store, "list_rows", side_effect=RuntimeError("boom secret")):
            status, body = self.call("GET /subscriptions")
        self.assertEqual((status, body["error"]), (500, "internal_error"))
        self.assertNotIn("boom", json.dumps(body))


class LimitTests(LocalDynamoTestCase):
    def test_ten_total_records_including_paused_and_expired(self):
        for d in DESTS[:6]:
            self.create(d)
        for d in DESTS[6:8]:
            self.seed(USER, fkey(d, "2027-02"), alerts_paused=True, departure_month="2027-02")
        for d in DESTS[8:10]:
            self.seed(USER, fkey(d, "2026-05"), departure_month="2026-05")  # expired
        status, body = self.create("LON")
        self.assertEqual((status, body["error"]), (409, "limit_reached"))
        self.assertEqual(body["details"], {"max": 10, "used": 10})
        self.assertEqual(len(self.raw()), 10)
        statuses = [s["status"] for s in self.call("GET /subscriptions")[1]["subscriptions"]]
        self.assertEqual(sorted(set(statuses)), ["active", "expired", "paused"])

    def test_pause_frees_nothing_and_delete_frees_one(self):
        for d in DESTS[:10]:
            self.assertEqual(self.create(d)[0], 201)
        before = self.quota()
        self.assertEqual(self.patch(fid("OSA"), {"alerts_paused": True})[0], 200)
        self.assertEqual(self.create("LON")[1]["error"], "limit_reached")
        self.assertEqual(self.create("OSA", month="2027-02")[1]["error"], "limit_reached")  # another month counts too
        self.assertEqual(self.patch(fid("OSA"), {"alerts_paused": False})[0], 200)  # resume needs no slot
        self.assertEqual(self.quota(), before)  # pause/resume never touch the quota
        self.assertEqual(self.delete(fid("OSA"))[0], 204)
        self.assertEqual(self.quota()["count"], 9)
        self.assertEqual(self.create("LON")[0], 201)
        self.assertEqual(self.create("PAR")[1]["error"], "limit_reached")
        self.assert_invariants()

    def test_expiry_does_not_release(self):
        self.create("LON", month="2026-12")
        self.now = datetime(2027, 1, 5, tzinfo=timezone.utc)
        sub = self.call("GET /subscriptions")[1]["subscriptions"][0]
        self.assertEqual(sub["status"], "expired")
        self.assertEqual(self.call("GET /subscriptions")[1]["limits"]["used"], 1)


class PatchTests(LocalDynamoTestCase):
    def setUp(self):
        super().setUp()
        _, self.row = self.create("LON", month="2027-01", target=28000)
        self.id = self.row["id"]

    def test_target_and_month(self):
        status, body = self.patch(self.id, {"target_price": 25000, "departure_month": "2027-06"})
        self.assertEqual(status, 200)
        self.assertEqual((body["id"], body["target_price"], body["departure_month"]), ("TPE-LON_2027-06", 25000, "2027-06"))
        self.assertGreater(body["updated_at"], self.row["updated_at"])
        self.assertEqual(body["created_at"], self.row["created_at"])  # same subscription, new month
        rows = self.raw()
        self.assertEqual(sorted(rows), ["TPE-LON#2027-06"])  # the sort key moved with the month
        self.assertEqual((rows["TPE-LON#2027-06"]["departure_month"], rows["TPE-LON#2027-06"]["previous_route"]),
                         ("2027-06", "TPE-LON#2027-01"))
        self.assertEqual((self.quota()["count"], self.quota()["version"]), (1, 1))  # quota untouched

    def test_target_only_edit_keeps_the_id(self):
        status, body = self.patch(self.id, {"target_price": 25000})
        self.assertEqual((status, body["id"]), (200, self.id))
        self.assertEqual(sorted(self.raw()), ["TPE-LON#2027-01"])

    def test_month_change_onto_an_existing_month_is_a_duplicate(self):
        self.create("LON", month="2027-02")
        status, body = self.patch(self.id, {"departure_month": "2027-02"})
        self.assertEqual((status, body["error"]), (409, "duplicate_subscription"))
        self.assertEqual(sorted(self.raw()), ["TPE-LON#2027-01", "TPE-LON#2027-02"])

    def test_pause_resume_stores_flag_only_when_true(self):
        self.assertTrue(self.patch(self.id, {"alerts_paused": True})[1]["alerts_paused"])
        self.assertIs(self.raw()["TPE-LON#2027-01"]["alerts_paused"], True)
        body = self.patch(self.id, {"alerts_paused": False})[1]
        self.assertEqual((body["alerts_paused"], body["status"]), (False, "active"))
        self.assertNotIn("alerts_paused", self.raw()["TPE-LON#2027-01"])

    def test_paused_month_change_stays_paused(self):
        self.patch(self.id, {"alerts_paused": True})
        body = self.patch(self.id, {"departure_month": "2027-04"})[1]
        self.assertEqual((body["id"], body["status"]), ("TPE-LON_2027-04", "paused"))

    def test_no_op_writes_nothing(self):
        status, body = self.patch(self.id, {"target_price": 28000, "departure_month": "2027-01"})
        self.assertEqual((status, body["updated_at"]), (200, self.row["updated_at"]))

    def test_cannot_return_to_rolling_or_leave_the_window(self):
        self.assertEqual(self.patch(self.id, {"departure_month": None})[1]["error"], "invalid_departure_month")
        self.assertEqual(self.patch(self.id, {"departure_month": "2028-01"})[1]["error"], "invalid_departure_month")

    def test_route_change_is_atomic(self):
        self.patch(self.id, {"alerts_paused": True})
        status, body = self.patch(self.id, {"origin": "KHH", "destination": "PAR", "target_price": 30000})
        self.assertEqual(status, 200)
        self.assertEqual((body["id"], body["route"], body["status"], body["departure_month"]),
                         ("KHH-PAR_2027-01", "KHH-PAR", "paused", "2027-01"))
        rows = self.raw()
        self.assertEqual(sorted(rows), ["KHH-PAR#2027-01"])
        self.assertEqual(rows["KHH-PAR#2027-01"]["previous_route"], "TPE-LON#2027-01")
        self.assertNotEqual(rows["KHH-PAR#2027-01"]["created_at"], self.row["created_at"])
        self.assertEqual(self.quota()["count"], 1)  # unchanged, and untouched
        self.assertEqual(self.quota()["version"], 1)

    def test_route_change_onto_existing_route_and_month(self):
        self.create("PAR")
        status, body = self.patch(self.id, {"origin": "TPE", "destination": "PAR"})
        self.assertEqual((status, body["error"]), (409, "duplicate_subscription"))
        self.assertEqual(sorted(self.raw()), ["TPE-LON#2027-01", "TPE-PAR#2027-01"])
        # The same route in another month is fine.
        body = self.patch(self.id, {"origin": "TPE", "destination": "PAR", "departure_month": "2027-03"})[1]
        self.assertEqual(body["id"], "TPE-PAR_2027-03")

    def test_optimistic_concurrency(self):
        v1 = self.row["version"]
        status, body = self.patch(self.id, {"target_price": 1, "if_version": "Q" * 22})
        self.assertEqual((status, body["error"]), (409, "conflict"))
        ok = self.patch(self.id, {"target_price": 2, "if_version": v1})
        self.assertEqual(ok[0], 200)
        self.assertNotEqual(ok[1]["version"], v1)  # every successful mutation returns a new version
        stale = self.patch(self.id, {"target_price": 3, "if_version": v1})
        self.assertEqual(stale[1]["error"], "conflict")
        stale_month = self.patch(self.id, {"departure_month": "2027-04", "if_version": v1})
        self.assertEqual(stale_month[1]["error"], "conflict")
        self.assertEqual(self.patch(self.id, {"target_price": 4, "if_version": ok[1]["version"]})[0], 200)

    def test_version_is_required_and_validated(self):
        status, body = self.patch(self.id, {"target_price": 1}, version=None)
        self.assertEqual((status, body["error"]), (428, "version_required"))
        status, body = self.delete(self.id, if_version=None)
        self.assertEqual((status, body["error"]), (428, "version_required"))
        for bad in ("", "short", 123, "legacy-xyz", "x" * 23):
            self.assertEqual(self.patch(self.id, {"target_price": 1, "if_version": bad})[1]["error"], "invalid_version", bad)
        self.assertEqual(self.delete(self.id, if_version="short")[1]["error"], "invalid_version")
        # The old timestamp token is not accepted as a substitute (contract §4.5).
        status, body = self.patch(self.id, {"target_price": 1, "if_updated_at": self.row["updated_at"]}, version=None)
        self.assertEqual((status, body["error"]), (400, "unknown_field"))
        self.assertEqual(self.raw()[fkey("LON")]["target_price"], 28000)  # nothing written
        self.assertEqual(self.quota()["count"], 1)

    def test_versions_are_random_and_never_derived_from_the_clock(self):
        """Fixed clock: two rows created at the same instant share updated_at but never a version."""
        other = self.create("PAR")[1]
        self.assertEqual(other["updated_at"], self.row["updated_at"])
        self.assertNotEqual(other["version"], self.row["version"])
        self.assertRegex(other["version"], r"^[A-Za-z0-9_-]{22}$")
        self.assertEqual(self.raw()[fkey("PAR")]["version_token"], other["version"])

    def test_concurrent_change_between_read_and_write_is_a_conflict(self):
        real_get = self.store.get_row

        def get_then_someone_edits(email, route):
            row = real_get(email, route)
            self.store.update(email, row, {"target_price": 1, "updated_at": "2099-01-01T00:00:00.000Z",
                                           "version_token": "N" * 22}, [])
            return row

        with mock.patch.object(self.store, "get_row", side_effect=get_then_someone_edits):
            status, body = self.patch(self.id, {"target_price": 5})
        self.assertEqual((status, body["error"]), (409, "conflict"))

    def test_concurrent_change_before_a_month_change_is_a_conflict(self):
        real_get, seen = self.store.get_row, {"n": 0}

        def get_then_someone_edits(email, route):
            row = real_get(email, route)
            seen["n"] += 1
            if seen["n"] == 1:
                self.store.update(email, row, {"target_price": 1, "updated_at": "2099-01-01T00:00:00.000Z",
                                               "version_token": "N" * 22}, [])
            return row

        with mock.patch.object(self.store, "get_row", side_effect=get_then_someone_edits):
            status, body = self.patch(self.id, {"departure_month": "2027-04"})
        self.assertEqual((status, body["error"]), (409, "conflict"))
        self.assertEqual(sorted(self.raw()), ["TPE-LON#2027-01"])

    def test_shape_errors_and_missing_rows(self):
        self.assertEqual(self.patch(self.id, {})[1]["error"], "empty_patch")
        self.assertEqual(self.patch(self.id, {"origin": "KHH"})[1]["error"], "route_change_incomplete")
        self.assertEqual(self.patch(self.id, {"nope": 1})[1]["error"], "unknown_field")
        self.assertEqual(self.patch(fid("PAR"), {"target_price": 1})[0], 404)
        self.assertEqual(self.patch("TPE-LON", {"target_price": 1})[0], 404)  # the rolling ID is another row
        self.assertEqual(self.patch("TPE-LON_2027-02", {"target_price": 1})[0], 404)
        for bad in ("bad", "TPE-LON#2027-01", "TPE-LON%232027-01"):
            self.assertEqual(self.patch(bad, {"target_price": 1})[1]["error"], "invalid_route", bad)

    def test_delete(self):
        status, body = self.delete(self.id, if_version="Q" * 22)
        self.assertEqual((status, body["error"]), (409, "conflict"))
        self.assertEqual(self.delete(self.id, if_version=self.row["version"])[0], 204)
        self.assertEqual(self.delete(self.id, if_version=self.row["version"])[0], 404)
        self.assertEqual(self.quota()["count"], 0)

    def test_older_route_path_parameter_is_still_accepted(self):
        status, body = self.call("PATCH /subscriptions/{route}", {"target_price": 7, "if_version": self.row["version"]},
                                 path={"route": self.id})
        self.assertEqual((status, body["target_price"]), (200, 7))


class ExpiryTests(LocalDynamoTestCase):
    def test_expire_then_reactivate(self):
        self.create("LON", month="2026-12")
        self.now = datetime(2027, 1, 5, tzinfo=timezone.utc)  # window now 2027-02 … 2028-01
        listing = self.call("GET /subscriptions")[1]
        self.assertEqual(listing["subscriptions"][0]["status"], "expired")
        # Editing the target keeps the expired month; it stays expired.
        body = self.patch(fid("LON", "2026-12"), {"target_price": 1, "departure_month": "2026-12"})[1]
        self.assertEqual((body["status"], body["id"]), ("expired", "TPE-LON_2026-12"))
        self.assertEqual(self.patch(fid("LON", "2026-12"), {"departure_month": "2027-01"})[1]["error"],
                         "invalid_departure_month")
        body = self.patch(fid("LON", "2026-12"), {"departure_month": "2027-04"})[1]
        self.assertEqual((body["id"], body["status"], body["effective_month"]), ("TPE-LON_2027-04", "active", "2027-04"))
        self.assertEqual(sorted(self.raw()), ["TPE-LON#2027-04"])
        self.assertEqual(self.quota()["count"], 1)


class M1CompatibilityTests(LocalDynamoTestCase):
    def setUp(self):
        super().setUp()
        # Exactly what M1.1 stored: no departure_month, no alerts_paused, no quota record.
        self.seed(USER, "TPE-TYO", plan_name="tokyo")
        self.seed(USER, "TPE-SEL", plan_name="seoul", target_price=7000)

    def test_list_is_a_superset_of_m1(self):
        subs = {s["route"]: s for s in self.call("GET /subscriptions")[1]["subscriptions"]}
        tyo = subs["TPE-TYO"]
        for k in ("route", "plan_name", "target_price", "currency", "updated_at"):  # the M1.1 fields
            self.assertIn(k, tyo)
        self.assertEqual((tyo["plan_name"], tyo["target_price"], tyo["currency"]), ("tokyo", 9000, "TWD"))
        self.assertEqual((tyo["departure_month"], tyo["month_mode"], tyo["effective_month"], tyo["status"]),
                         (None, "rolling", "2026-12", "active"))
        self.assertIsNone(self.quota())  # reading never creates a quota record

    def test_m1_rows_count_and_first_write_creates_the_quota_record(self):
        for d in DESTS[:8]:
            self.assertEqual(self.create(d)[0], 201)
        self.assertEqual(self.quota()["count"], 10)
        self.assertEqual(self.create("LON")[1]["error"], "limit_reached")

    def test_rolling_stays_rolling_until_a_month_is_chosen(self):
        body = self.patch("TPE-TYO", {"target_price": 8500})[1]
        self.assertEqual((body["month_mode"], body["plan_name"], body["created_at"]),
                         ("rolling", "tokyo", "2026-10-30T00:00:00+00:00"))
        self.assertNotIn("departure_month", self.raw()["TPE-TYO"])
        self.assertEqual(self.patch("TPE-TYO", {"departure_month": None})[0], 200)  # no change
        body = self.patch("TPE-TYO", {"departure_month": "2027-02"})[1]
        # Choosing a month moves the row to its fixed-month key; the M1 plan marker stays with the
        # rolling key (it identifies the two M1 plan rows), the subscription keeps its created_at.
        self.assertEqual((body["id"], body["month_mode"], body["plan_name"], body["created_at"]),
                         ("TPE-TYO_2027-02", "fixed", None, "2026-10-30T00:00:00+00:00"))
        self.assertEqual(sorted(self.raw()), ["TPE-SEL", "TPE-TYO#2027-02"])
        self.assertIsNone(self.quota())  # a key change never touches the quota
        self.assertEqual(self.patch("TPE-TYO", {"target_price": 1})[0], 404)
        self.assertEqual(self.patch("TPE-TYO_2027-02", {"departure_month": None})[1]["error"], "invalid_departure_month")

    def test_rolling_and_fixed_rows_of_one_route_coexist(self):
        """R-MM-1: the legacy rolling TPE-TYO and a fixed TPE-TYO month are separate records,
        including the fixed month equal to the rolling row's effective month (2026-12 here)."""
        self.assertEqual(self.create("TYO", month="2026-12", target=8000)[0], 201)
        self.assertEqual(self.create("TYO", month="2027-03")[0], 201)
        subs = {s["id"]: s for s in self.call("GET /subscriptions")[1]["subscriptions"]}
        self.assertEqual(sorted(subs), ["TPE-SEL", "TPE-TYO", "TPE-TYO_2026-12", "TPE-TYO_2027-03"])
        self.assertEqual((subs["TPE-TYO"]["effective_month"], subs["TPE-TYO_2026-12"]["effective_month"]),
                         ("2026-12", "2026-12"))
        self.assertEqual(self.quota()["count"], 4)
        # Moving the rolling row onto an existing fixed month is a duplicate, not a merge.
        status, body = self.patch("TPE-TYO", {"departure_month": "2026-12"})
        self.assertEqual((status, body["error"]), (409, "duplicate_subscription"))

    def test_second_precision_m1_timestamp_still_advances(self):
        body = self.patch("TPE-TYO", {"target_price": 1})[1]
        self.assertGreater(body["updated_at"], "2026-10-30T00:00:00+00:00")


class LegacySubscribeTests(LocalDynamoTestCase):
    def legacy(self, plan, target=8000, token="good"):
        return self.call("POST /subscribe", {"plan_name": plan, "target_price": target}, token=token)

    def test_new_route_is_a_quota_checked_rolling_create(self):
        status, body = self.legacy("tokyo", 9000.0)
        self.assertEqual(status, 200)
        self.assertEqual(set(body), {"route", "plan_name", "target_price", "currency", "updated_at"})  # M1 shape
        self.assertEqual((body["route"], body["target_price"]), ("TPE-TYO", 9000))
        row = self.raw()["TPE-TYO"]
        self.assertNotIn("departure_month", row)
        self.assertEqual(self.quota()["count"], 1)

    def test_existing_rolling_row_updates_in_place(self):
        self.legacy("tokyo", 9000)
        self.patch("TPE-TYO", {"alerts_paused": True})
        created = self.raw()["TPE-TYO"]["created_at"]
        version = self.quota()["version"]
        self.assertEqual(self.legacy("tokyo", 7777)[0], 200)
        row = self.raw()["TPE-TYO"]
        self.assertEqual((row["target_price"], row["plan_name"], row["alerts_paused"], row["created_at"]),
                         (7777, "tokyo", True, created))
        self.assertNotIn("departure_month", row)
        self.assertEqual(self.quota()["version"], version)

    def test_fixed_month_rows_are_never_touched_by_the_legacy_endpoint(self):
        _, fixed = self.create("TYO", month="2027-03", target=5000)
        self.assertEqual(self.legacy("tokyo", 7777)[0], 200)  # creates the rolling row beside it
        rows = self.raw()
        self.assertEqual(sorted(rows), ["TPE-TYO", "TPE-TYO#2027-03"])
        self.assertEqual((rows["TPE-TYO#2027-03"]["target_price"], rows["TPE-TYO#2027-03"]["updated_at"]),
                         (5000, fixed["updated_at"]))
        self.assertEqual(self.quota()["count"], 2)

    def test_cannot_bypass_the_limit(self):
        for d in DESTS[:10]:
            self.create(d)
        status, body = self.legacy("seoul")
        self.assertEqual((status, body["error"]), (409, "limit_reached"))
        self.assertNotIn("TPE-SEL", self.raw())

    def test_m1_validation(self):
        self.assertEqual(self.legacy("osaka")[1]["error"], "invalid_plan")
        self.assertEqual(self.legacy("tokyo", 0)[1]["error"], "invalid_target_price")
        self.assertEqual(self.call("POST /subscribe", raw="[]")[1]["error"], "invalid_json")


class HandlerSecurityTests(LocalDynamoTestCase):
    """The real resolve_email (Supabase path) behind the handler, with a fake Supabase."""

    def setUp(self):
        super().setUp()
        self.auth.stop()
        import functools
        from subsapi import auth as auth_mod

        def supabase(req, timeout):
            token = req.headers.get("Authorization", "")[7:]
            if token not in TOKENS:
                raise urllib.error.HTTPError(req.full_url, 401, "invalid JWT", {}, None)
            return FakeResponse(json.dumps({"email": TOKENS[token], "aud": "authenticated"}).encode())

        self.auth = mock.patch.object(self.index, "resolve_email",
                                      functools.partial(auth_mod.resolve_email, opener=supabase))
        self.auth.start()
        self.env = mock.patch.dict(os.environ, ENV)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    ROUTES = [("GET /subscriptions", None, None), ("POST /subscriptions", {"origin": "TPE", "destination": "LON",
              "departure_month": "2027-01", "target_price": 1}, None),
              ("PATCH /subscriptions/{id}", {"target_price": 1}, {"id": "TPE-LON_2027-01"}),
              ("DELETE /subscriptions/{id}", None, {"id": "TPE-LON_2027-01"}),
              ("PATCH /subscriptions/{route}", {"target_price": 1}, {"route": "TPE-LON_2027-01"}),
              ("POST /subscribe", {"plan_name": "tokyo", "target_price": 1}, None)]

    def test_every_operation_requires_a_valid_session(self):
        self.seed(USER, fkey("LON"), departure_month="2027-01")
        before = self.raw()
        for key, body, path in self.ROUTES:
            for token in (None, "forged", ""):
                status, out = self.call(key, body, path=path, token=token)
                self.assertEqual((status, out["error"]), (401, "unauthorized"), (key, token))
        self.assertEqual(self.raw(), before)
        self.assertIsNone(self.quota())

    def test_direct_invocation_is_anonymous_in_production(self):
        prod = {"ALLOW_DIRECT_TEST_INVOKE": "true", "AWS_LAMBDA_FUNCTION_NAME": "flight-subscriptions-api"}
        with mock.patch.dict(os.environ, prod):
            r = self.index.handler({"routeKey": "POST /subscriptions", "test_email": USER,
                                    "body": {"origin": "TPE", "destination": "LON", "departure_month": "2027-01",
                                             "target_price": 1}}, None)
        self.assertEqual(r["statusCode"], 401)
        self.assertEqual(self.raw(), {})

    def test_api_gateway_request_cannot_switch_on_test_mode(self):
        with mock.patch.dict(os.environ, {"ALLOW_DIRECT_TEST_INVOKE": "true", "AWS_LAMBDA_FUNCTION_NAME": "x-test"}):
            event = apigw("GET /subscriptions", token="forged")
            event["test_email"] = USER
            r = self.index.handler(event, None)
        self.assertEqual(r["statusCode"], 401)

    def test_no_access_to_another_users_rows(self):
        self.seed(OTHER, fkey("LON"), departure_month="2027-01")
        theirs = self.raw(OTHER)
        self.assertEqual(self.call("GET /subscriptions")[1]["subscriptions"], [])
        # Knowing another user's subscription ID gives no access: the ID is only a sort key
        # inside the caller's own partition (the session's email).
        self.assertEqual(self.patch(fid("LON"), {"target_price": 1, "email": OTHER})[0], 404)
        self.assertEqual(self.call("DELETE /subscriptions/{id}", path={"id": fid("LON")},
                                   query={"email": OTHER, "if_version": "Z" * 22})[0], 404)
        status, body = self.create("LON")  # creates the caller's own row, not theirs
        self.assertEqual(status, 201)
        self.assertEqual(self.raw(OTHER), theirs)
        self.assertEqual(sorted(self.raw(USER)), [fkey("LON")])


class TransactionShapeTests(LocalDynamoTestCase):
    """Records every TransactWriteItems the handler sends and checks its shape."""

    def setUp(self):
        super().setUp()
        self.calls = []
        real = self.client.transact_write_items

        def spy(**kw):
            self.calls.append(kw)
            return real(**kw)

        self.spy = mock.patch.object(self.client, "transact_write_items", side_effect=spy)
        self.spy.start()

    def tearDown(self):
        self.spy.stop()
        super().tearDown()

    LEGACY_GUARD = "attribute_exists(email) AND attribute_not_exists(#gtok) AND "

    def kinds(self, call):
        """(op, table, condition); a legacy-row guard (one clause per stored attribute) is shown
        as "legacy-guard"."""
        def cond(c):
            return "legacy-guard" if c and c.startswith(self.LEGACY_GUARD) else c
        return [(op, body["TableName"], cond(body.get("ConditionExpression"))) for item in call["TransactItems"]
                for op, body in item.items()]

    def assert_well_formed(self, call):
        import uuid as _uuid
        _uuid.UUID(call["ClientRequestToken"])  # idempotency token on every transaction
        targets = [self.Store._target(i) for i in call["TransactItems"]]
        self.assertEqual(len(targets), len(set(targets)), "two operations on one item")

    def test_create_and_delete_always_update_the_quota_version(self):
        self.seed(USER, "TPE-TYO", plan_name="tokyo")  # M1 user, no quota record
        self.create("LON")  # first write: quota created with attribute_not_exists
        self.create("PAR")  # then version-checked
        self.delete("TPE-TYO")  # deleting an M1 row is quota-checked too
        self.call("POST /subscribe", {"plan_name": "seoul", "target_price": 1})  # legacy create
        self.assertEqual([self.kinds(c) for c in self.calls], [
            [("Put", self.subs_table, "attribute_not_exists(email)"),
             ("Update", self.quota_table, "attribute_not_exists(email)")],
            [("Put", self.subs_table, "attribute_not_exists(email)"), ("Update", self.quota_table, "#v = :v")],
            # bound to the version the request read (ABA guard, 2026-11-13)
            [("Delete", self.subs_table, "legacy-guard"), ("Update", self.quota_table, "#v = :v")],  # M1 row: no token
            [("Put", self.subs_table, "attribute_not_exists(email)"), ("Update", self.quota_table, "#v = :v")],
        ])
        for c in self.calls:
            self.assert_well_formed(c)
        self.assertEqual(self.quota(), {**self.quota(), "count": 3, "version": 4})  # TYO deleted; SEL, LON, PAR
        self.assert_invariants()

    def test_delete_of_a_row_without_updated_at_is_bound_to_that_row(self):
        """An M1 row with neither version_token nor updated_at: its public version is legacy-…,
        and the delete is guarded by every stored attribute (contract §4.5)."""
        self.seed(USER, "TPE-TYO", plan_name="tokyo")
        self.client.update_item(TableName=self.subs_table, Key=store_mod.to_item({"email": USER, "route": "TPE-TYO"}),
                                UpdateExpression="REMOVE updated_at")
        self.assertRegex(self.current_version("TPE-TYO"), r"^legacy-[0-9a-f]{32}$")
        self.calls.clear()
        self.assertEqual(self.delete("TPE-TYO")[0], 204)
        self.assertEqual(self.kinds(self.calls[0])[0],
                         ("Delete", self.subs_table, "legacy-guard"))
        cond = self.calls[0]["TransactItems"][0]["Delete"]
        self.assertEqual(sorted(cond["ExpressionAttributeNames"].values()),
                         sorted(["version_token", "created_at", "currency", "destination", "origin", "plan_name", "target_price"]))
        self.assertEqual(self.raw(), {})
        self.assert_invariants()

    def test_route_and_month_changes_are_one_transaction_without_the_quota(self):
        self.create("LON")
        for body in ({"origin": "KHH", "destination": "LON"}, {"departure_month": "2027-05"}):
            self.calls.clear()
            current = next(iter(self.raw()))
            self.patch(subkeys.public_id(current), body)
            self.assertEqual(len(self.calls), 1, body)
            self.assertEqual([k for k in self.kinds(self.calls[0])],
                             [("Delete", self.subs_table, "attribute_exists(email) AND #gtok = :gtok AND #gupd = :gupd"),
                              ("Put", self.subs_table, "attribute_not_exists(email)")], body)
            self.assert_well_formed(self.calls[0])
        self.assertEqual(sorted(self.raw()), ["KHH-LON#2027-05"])
        self.assertEqual((self.quota()["count"], self.quota()["version"]), (1, 1))

    def test_edits_pause_and_legacy_updates_are_not_transactions(self):
        self.create("TYO")
        self.call("POST /subscribe", {"plan_name": "tokyo", "target_price": 6})
        self.calls.clear()
        self.patch(fid("TYO"), {"target_price": 5, "alerts_paused": True, "departure_month": "2027-01"})
        self.patch("TPE-TYO", {"target_price": 7, "alerts_paused": True})
        self.call("POST /subscribe", {"plan_name": "tokyo", "target_price": 6})
        self.assertEqual(self.calls, [])  # single conditional UpdateItems; the count can't change

    def test_two_operations_on_one_item_are_refused_before_sending(self):
        item = {"Put": {"TableName": self.subs_table, "Item": store_mod.to_item({"email": USER, "route": "A-B"})}}
        with self.assertRaises(ValueError):
            self.store._transact([item, {"Delete": {"TableName": self.subs_table,
                                                    "Key": store_mod.to_item({"email": USER, "route": "A-B"})}}])
        self.assertEqual(self.calls, [])

    def test_parallel_first_writes_initialise_the_quota_once(self):
        self.seed(USER, "TPE-TYO", plan_name="tokyo")
        self.svc.sleep = time.sleep
        barrier = threading.Barrier(50)

        def go(d):
            barrier.wait()
            try:
                return self.svc.create(USER, {"origin": "TPE", "destination": d, "departure_month": "2027-01",
                                              "target_price": 1})[0]
            except ApiError as e:
                return e.code

        with ThreadPoolExecutor(50) as pool:
            list(pool.map(go, DESTS[:50]))
        inits = [c for c in self.calls if ("Update", self.quota_table, "attribute_not_exists(email)") in self.kinds(c)]
        self.assertGreaterEqual(len(inits), 1)
        q = self.quota()
        rows = self.raw()
        self.assertEqual(q["count"], len(rows))
        # Exactly one initialising transaction succeeded: every later version increments by one.
        self.assertEqual(q["version"], len(rows) - 1)  # 1 for the init + one per later create; TYO pre-existed
        self.assert_invariants()


class RetryHandlingTests(LocalDynamoTestCase):
    """How the service reacts to each TransactionCanceledException reason and other errors."""

    body = {"origin": "TPE", "destination": "LON", "departure_month": "2027-01", "target_price": 1}

    def run_create(self, *effects):
        with mock.patch.object(self.store, "create", side_effect=list(effects)) as m:
            try:
                out = self.svc.create(USER, dict(self.body))[0]
            except ApiError as e:
                out = e.code
        return out, m.call_count

    def test_reasons(self):
        Tx = store_mod.TxCancelled
        self.assertEqual(self.run_create(Tx(["ConditionalCheckFailed", "None"])), ("duplicate_subscription", 1))
        self.assertEqual(self.run_create(Tx(["None", "ConditionalCheckFailed"]), None), (201, 2))
        self.assertEqual(self.run_create(Tx(["TransactionConflict", "None"]), None), (201, 2))
        self.assertEqual(self.run_create(Tx(["None", "TransactionConflict"]), None), (201, 2))
        self.assertEqual(self.run_create(Tx([]), Tx([]), Tx([])), ("conflict", 3))  # bounded: 3 attempts
        self.assertEqual(len(self.sleeps), 0 + 1 + 1 + 1 + 2)  # one wait before each retry, none before attempt 1
        self.assertTrue(all(0 <= s <= 0.2 for s in self.sleeps))

    def test_conditional_check_failed_on_updates(self):
        self.create("LON")
        with mock.patch.object(self.store, "update", side_effect=store_mod.ConditionFailed()):
            self.assertEqual(self.patch(fid("LON"), {"target_price": 2})[1]["error"], "conflict")

    def test_month_change_transaction_conflict_is_409_and_writes_nothing(self):
        _, row = self.create("LON")
        for reasons in (["TransactionConflict", "None"], ["ConditionalCheckFailed", "None"]):
            with mock.patch.object(self.store, "change_route", side_effect=store_mod.TxCancelled(reasons)):
                status, body = self.patch(fid("LON"), {"departure_month": "2027-04"})
            self.assertEqual((status, body["error"]), (409, "conflict"), reasons)
        with mock.patch.object(self.store, "change_route",
                               side_effect=store_mod.TxCancelled(["None", "ConditionalCheckFailed"])):
            self.assertEqual(self.patch(fid("LON"), {"departure_month": "2027-04"})[1]["error"],
                             "duplicate_subscription")
        self.assertEqual(self.raw(), {fkey("LON"): self.raw()[fkey("LON")]})
        self.assertEqual(self.raw()[fkey("LON")]["updated_at"], row["updated_at"])

    def test_cancellation_reasons_are_parsed_from_dynamodb(self):
        self.create("LON")
        with self.assertRaises(store_mod.TxCancelled) as e:
            self.store.create(USER, {"route": fkey("LON"), "target_price": 1}, self.quota(), 2, "t")
        self.assertEqual(e.exception.reasons[0], "ConditionalCheckFailed")

    def test_other_dynamodb_errors_are_500_and_write_nothing(self):
        from botocore.exceptions import ClientError
        err = ClientError({"Error": {"Code": "ValidationException", "Message": "x"}}, "TransactWriteItems")
        with mock.patch.object(self.client, "transact_write_items", side_effect=err):
            status, body = self.create("LON")
        self.assertEqual((status, body["error"]), (500, "internal_error"))
        self.assertEqual(self.raw(), {})


class TransactionConflictRegressionTests(LocalDynamoTestCase):
    """Regression for B4 Stage C, scenario s10 (real DynamoDB, 2026-11-06): a single-item
    UpdateItem on a row that an in-flight TransactWriteItems is changing fails with
    TransactionConflictException ("Transaction is ongoing for the item"). Emulators never raise
    it, so these tests inject the exact error DynamoDB returned, as botocore raises it."""

    @staticmethod
    def dynamo_error(code, op="UpdateItem", msg="Transaction is ongoing for the item"):
        from botocore.exceptions import ClientError
        return ClientError({"Error": {"Code": code, "Message": msg},
                            "ResponseMetadata": {"HTTPStatusCode": 400}}, op)

    def fail_first_updates(self, n, code="TransactionConflictException"):
        """Make the first n update_item calls fail like DynamoDB did, then behave normally."""
        real, calls = self.client.update_item, {"n": 0}

        def side_effect(**kw):
            calls["n"] += 1
            if calls["n"] <= n:
                raise self.dynamo_error(code)
            return real(**kw)

        return mock.patch.object(self.client, "update_item", side_effect=side_effect), calls

    def legacy(self, target=7000):
        return self.call("POST /subscribe", {"plan_name": "tokyo", "target_price": target})

    def test_store_maps_the_error_to_write_conflict(self):
        self.create("LON")
        patcher, _ = self.fail_first_updates(1)
        with patcher, self.assertRaises(store_mod.WriteConflict) as e:
            self.store.update(USER, self.store.get_row(USER, fkey("LON")), {"target_price": 1}, [])
        self.assertEqual(e.exception.code, "TransactionConflictException")
        self.assertTrue(e.exception.retryable)

    def test_legacy_subscribe_retries_and_succeeds(self):
        self.legacy(9000)  # creates TPE-TYO (rolling); the next call takes the update path
        patcher, calls = self.fail_first_updates(1)
        with patcher:
            status, body = self.legacy(7000)
        self.assertEqual((status, body["target_price"]), (200, 7000))
        self.assertEqual(calls["n"], 2)  # one conflict, one successful retry
        self.assertEqual(len(self.sleeps), 1)  # one bounded backoff before the retry
        self.assertTrue(0 <= self.sleeps[0] <= 0.2)
        self.assertEqual(self.raw()["TPE-TYO"]["target_price"], 7000)

    def test_legacy_subscribe_gives_up_after_three_attempts_with_409(self):
        self.legacy(9000)
        before = self.raw()["TPE-TYO"]
        patcher, calls = self.fail_first_updates(99)
        with patcher:
            status, body = self.legacy(7000)
        self.assertEqual((status, body["error"]), (409, "conflict"))  # not 500
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(self.sleeps), 2)
        self.assertEqual(self.raw()["TPE-TYO"], before)  # nothing written

    def test_patch_returns_409_without_retrying(self):
        _, row = self.create("LON", target=28000)
        patcher, calls = self.fail_first_updates(99)
        # (A month change is a key change, i.e. a transaction: see RetryHandlingTests.)
        for body in ({"target_price": 1}, {"target_price": 3, "departure_month": "2027-01"}, {"alerts_paused": True},
                     {"alerts_paused": False, "target_price": 2}):
            with patcher:
                status, out = self.patch(fid("LON"), body)
            self.assertEqual((status, out["error"]), (409, "conflict"), body)
        self.assertEqual(calls["n"], 4)  # exactly one attempt per request: no blind retry
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.raw()[fkey("LON")]["updated_at"], row["updated_at"])

    def test_conditional_check_failed_handling_is_unchanged(self):
        self.create("LON")
        patcher, _ = self.fail_first_updates(1, code="ConditionalCheckFailedException")
        with patcher:
            self.assertEqual(self.patch(fid("LON"), {"target_price": 1})[1]["error"], "conflict")
        self.assertEqual(self.patch(fid("PAR"), {"target_price": 1})[0], 404)

    def test_transaction_in_progress_is_never_retried(self):
        """Our own re-sent transaction may still commit: report 409, don't create a second one."""
        err = self.dynamo_error("TransactionInProgressException", op="TransactWriteItems", msg="in progress")
        with mock.patch.object(self.client, "transact_write_items", side_effect=err) as m:
            status, body = self.create("LON")
        self.assertEqual((status, body["error"], m.call_count), (409, "conflict", 1))
        with mock.patch.object(self.client, "transact_write_items", side_effect=err) as m:
            status, body = self.legacy(7000)
        self.assertEqual((status, body["error"], m.call_count), (409, "conflict", 1))

    def test_transaction_conflict_raised_by_transact_is_retried(self):
        """Defensive: if TransactWriteItems itself ever raises TransactionConflictException, it
        is treated like a TransactionConflict cancellation (re-read and retry)."""
        real, n = self.client.transact_write_items, {"n": 0}

        def flaky(**kw):
            n["n"] += 1
            if n["n"] == 1:
                raise self.dynamo_error("TransactionConflictException", op="TransactWriteItems")
            return real(**kw)

        with mock.patch.object(self.client, "transact_write_items", side_effect=flaky):
            self.assertEqual(self.create("LON")[0], 201)
        self.assertEqual(n["n"], 2)
        self.assert_invariants()

    def test_unexpected_errors_stay_500_and_are_logged(self):
        self.create("LON")
        self.quiet.stop()  # capture the handler's log line for this test
        try:
            for code in ("ValidationException", "AccessDeniedException"):
                patcher, _ = self.fail_first_updates(1, code=code)
                with patcher, mock.patch("builtins.print") as log:
                    status, body = self.patch(fid("LON"), {"target_price": 1})
                self.assertEqual((status, body["error"]), (500, "internal_error"), code)
                line = json.loads(log.call_args[0][0])
                self.assertEqual((line["exception"], line["aws_error"]), ("ClientError", code))
                self.assertNotIn("@", log.call_args[0][0])  # no email in logs
        finally:
            self.quiet.start()


class LegacyStaleUpdateTests(LocalDynamoTestCase):
    """The legacy upsert is conditioned on the updated_at it read, so a retry can never write
    a stale updated_at over a newer one or overwrite a change it hasn't seen."""

    def test_legacy_update_rereads_after_a_concurrent_change(self):
        self.call("POST /subscribe", {"plan_name": "tokyo", "target_price": 9000})
        real_list = self.store.list_rows
        state = {"done": False}

        def list_then_concurrent_patch(email):
            rows = real_list(email)
            if not state["done"]:  # between our read and our write, someone pauses the row
                state["done"] = True
                self.svc.patch(USER, "TPE-TYO", {"alerts_paused": True, "target_price": 8888,
                                                 "if_version": self.current_version("TPE-TYO")})
            return rows

        with mock.patch.object(self.store, "list_rows", side_effect=list_then_concurrent_patch):
            status, body = self.call("POST /subscribe", {"plan_name": "tokyo", "target_price": 7000})
        self.assertEqual(status, 200)
        row = self.raw()["TPE-TYO"]
        self.assertEqual(row["target_price"], 7000)  # the later legacy request's own value
        self.assertIs(row["alerts_paused"], True)  # the concurrent pause is kept
        self.assertEqual(len(self.sleeps), 1)  # it re-read once instead of writing blindly
        self.assertEqual(row["updated_at"], body["updated_at"])


# =============================================================================================
# Concurrency: ≥ 50 simultaneous requests per scenario
# =============================================================================================
class ConcurrencyTests(LocalDynamoTestCase):
    """Safety: no interleaving may exceed the limit or desynchronise the quota count.
    Liveness is bounded on purpose: each request makes at most 3 attempts, so under extreme
    contention from one user some requests end in 409 conflict (nothing written) and the client
    retries; a follow-up pass shows the user can still fill exactly the free slots."""

    def setUp(self):
        super().setUp()
        self.svc.sleep = time.sleep  # real, bounded jittered backoff between attempts

    def fill_up(self, email=USER):
        """Sequential retries after the storm: the user reaches exactly the limit, never more."""
        for d in DESTS:
            status, _ = self.create_call(d, email=email)()
            if len(self.raw(email)) >= CFG.max_subscriptions:
                break
        self.assertNotIn("CNS", DESTS)
        self.assertEqual(self.create_call("CNS", email=email)()[1]["error"], "limit_reached")
        self.assertEqual(len(self.raw(email)), CFG.max_subscriptions)
        self.assert_invariants(email)

    def run_parallel(self, calls):
        """Start every call at the same moment (barrier); return their (status, body) results."""
        barrier = threading.Barrier(len(calls))

        def go(fn):
            barrier.wait()
            return fn()

        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            return list(pool.map(go, calls))

    def svc_call(self, method, *args):
        """Call the service directly (the handler's auth patch isn't thread-relevant)."""
        try:
            if method == "patch" and "if_version" not in args[2]:      # as an up-to-date page sends it
                args = (args[0], args[1], {**args[2], "if_version": self.current_version(args[1], args[0])})
            if method == "delete" and len(args) == 2:
                args = (*args, self.current_version(args[1], args[0]))
            return getattr(self.svc, method)(*args)
        except ApiError as e:
            return e.status, {"error": e.code}

    def create_call(self, dest, email=USER, month="2027-01"):
        body = {"origin": "TPE", "destination": dest, "departure_month": month, "target_price": 9000}
        return lambda: self.svc_call("create", email, body)

    def codes(self, results):
        out = {}
        for status, body in results:
            key = status if status < 400 else f"{status} {body['error']}"
            out[key] = out.get(key, 0) + 1
        return out

    def test_60_creates_from_empty_m1_user_without_quota_record(self):
        results = self.run_parallel([self.create_call(d) for d in DESTS[:60]])
        created = sum(s == 201 for s, _ in results)
        self.assertTrue(1 <= created <= 10, self.codes(results))
        self.assertTrue(all(s == 201 or (s == 409 and b["error"] in ("limit_reached", "conflict"))
                            for s, b in results), self.codes(results))
        self.assertEqual(len(self.raw()), created)  # every 201 is a row; every 409 wrote nothing
        self.assert_invariants()
        self.fill_up()

    def test_50_creates_at_nine(self):
        for d in DESTS[:9]:
            self.create(d)
        results = self.run_parallel([self.create_call(d) for d in DESTS[9:59]])
        self.assertLessEqual(sum(s == 201 for s, _ in results), 1, self.codes(results))
        self.assert_invariants()
        self.fill_up()

    def test_50_creates_of_the_same_route(self):
        results = self.run_parallel([self.create_call("LON") for _ in range(50)])
        self.assertEqual(sum(s == 201 for s, _ in results), 1, self.codes(results))
        self.assertTrue(all(b["error"] in ("duplicate_subscription", "conflict")
                            for s, b in results if s != 201), self.codes(results))
        self.assertEqual(sorted(self.raw()), [fkey("LON")])
        self.assert_invariants()

    def test_60_creates_of_one_route_across_twelve_months(self):
        """R-MM-1: each month is its own record; the limit still caps the total at 10."""
        months_ = ["2026-12"] + [f"2027-{m:02d}" for m in range(1, 12)]
        results = self.run_parallel([self.create_call("TYO", month=months_[i % 12]) for i in range(60)])
        rows = self.raw()
        created = [b["id"] for s, b in results if s == 201]
        self.assertTrue(1 <= len(created) <= 10, self.codes(results))
        self.assertEqual(len(created), len(set(created)), "a month was created twice")
        self.assertEqual(sorted(rows), sorted(subkeys.key_from_id(i) for i in created))
        self.assertTrue(all(s == 201 or b["error"] in ("duplicate_subscription", "limit_reached", "conflict")
                            for s, b in results), self.codes(results))
        self.assert_invariants()
        for m in months_:  # sequential follow-up: exactly 10, the 11th month refused
            self.create_call("TYO", month=m)()
        self.assertEqual(len(self.raw()), 10)
        self.assert_invariants()

    def test_50_duplicate_creates_of_one_month_beside_other_months(self):
        for m in ("2027-01", "2027-02"):
            self.create("TYO", month=m)
        results = self.run_parallel([self.create_call("TYO", month="2027-03") for _ in range(50)])
        self.assertEqual(sum(s == 201 for s, _ in results), 1, self.codes(results))
        self.assertEqual(sorted(self.raw()), [fkey("TYO", m) for m in ("2027-01", "2027-02", "2027-03")])
        self.assert_invariants()

    def test_creates_and_deletes_at_the_limit(self):
        for d in DESTS[:10]:
            self.create(d)
        deletes = [lambda d=d: self.svc_call("delete", USER, fid(d)) for d in DESTS[:5]]
        creates = [self.create_call(d) for d in DESTS[10:55]]
        results = self.run_parallel(deletes + creates)
        deleted = sum(s == 204 for s, _ in results[:5])
        created = sum(s == 201 for s, _ in results[5:])
        # A delete can lose all 3 attempts to the create storm: 409 conflict, nothing deleted.
        self.assertTrue(all(s == 204 or b["error"] == "conflict" for s, b in results[:5]), self.codes(results[:5]))
        self.assertEqual(len(self.raw()), 10 - deleted + created)
        self.assertLessEqual(created, deleted)
        self.assert_invariants()
        self.fill_up()

    def test_legacy_and_new_endpoint_race_at_nine(self):
        for d in DESTS[:9]:
            self.create(d)
        legacy = [lambda: self.svc_call("legacy_subscribe", USER, {"plan_name": "tokyo", "target_price": 8000})
                  for _ in range(25)]
        creates = [self.create_call(d) for d in DESTS[9:34]]
        results = self.run_parallel(legacy + creates)
        self.assert_invariants()
        rows = self.raw()
        self.assertLessEqual(len(rows), 10, self.codes(results))
        legacy_won = "TPE-TYO" in rows
        creates_won = sum(s == 201 for s, _ in results[25:])
        self.assertEqual(int(legacy_won) + creates_won, len(rows) - 9, self.codes(results))
        if not legacy_won:  # no legacy request may have written a row
            self.assertTrue(all(s == 409 for s, _ in results[:25]), self.codes(results[:25]))
        self.fill_up()

    def test_pauses_resumes_and_route_edits_alongside_creates(self):
        for d in DESTS[:8]:
            self.create(d)
        pauses = [lambda i=i: self.svc_call("patch", USER, fid(DESTS[i % 4]), {"alerts_paused": i % 2 == 0})
                  for i in range(20)]
        edits = [lambda: self.svc_call("patch", USER, fid("SPK"), {"origin": "KHH", "destination": "SPK"})
                 for _ in range(5)]
        creates = [self.create_call(d) for d in DESTS[10:40]]
        results = self.run_parallel(pauses + edits + creates)
        self.assertTrue(all(s in (200, 409) for s, _ in results[:20]), self.codes(results[:20]))
        self.assertTrue(all(s in (200, 404, 409) for s, _ in results[20:25]), self.codes(results[20:25]))
        self.assertEqual(sum(s == 200 for s, _ in results[20:25]), 1, self.codes(results[20:25]))
        self.assertLessEqual(sum(s == 201 for s, _ in results[25:]), 2, self.codes(results[25:]))
        self.assertIn(fkey("SPK", origin="KHH"), self.raw())
        self.assertNotIn(fkey("SPK"), self.raw())
        self.assert_invariants()
        self.fill_up()

    def test_many_users_at_once(self):
        users = [f"user{i}@example.com" for i in range(6)]
        calls = [self.create_call(d, email=u) for u in users for d in DESTS[:12]]  # 72 requests
        self.run_parallel(calls)
        for u in users:
            self.assert_invariants(u)
            self.fill_up(u)


if __name__ == "__main__":
    unittest.main()
