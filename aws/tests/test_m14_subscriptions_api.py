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
from subsapi import months, validation  # noqa: E402
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
OTHER = "other@example.com"
DESTS = ["OSA", "NGO", "FUK", "OKA", "SPK", "KMJ", "PUS", "CJU", "HKG", "MFM", "BKK", "CNX", "HKT",
         "SIN", "KUL", "PEN", "BKI", "MNL", "CEB", "SGN", "HAN", "DAD", "DPS", "LON", "PAR", "FRA",
         "MUC", "AMS", "ZRH", "VIE", "ROM", "MIL", "NYC", "LAX", "SFO", "SEA", "SYD", "MEL", "AKL",
         "ZQN", "BER", "GVA", "VCE", "FLR", "MAD", "BCN", "LIS", "OPO", "PRG", "BUD", "CPH", "STO",
         "OSL", "HEL", "DUB", "ATH", "IST", "CHI", "BOS", "WAS"]


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

    def test_legacy_target_keeps_m1_rules(self):
        self.assertEqual(validation.legacy_target_price(CFG, 9000.0), 9000)
        for t in (9000.5, True, "9000", 0):
            self.assertEqual(self.code(validation.legacy_target_price, CFG, t), "invalid_target_price")


class MessageTests(unittest.TestCase):
    def test_traditional_chinese_messages(self):
        self.assertEqual(message("duplicate_subscription", CFG, NOW, {"route": "TPE-LON"}),
                         "你已經訂閱過「台北 → 倫敦」這條航線（每條航線只能訂閱一個月份）。")
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
        self.assertIsNone(resolve_email(event, ENV))
        self.assertEqual(resolve_email(event, {**ENV, "ALLOW_DIRECT_TEST_INVOKE": "true"}), USER)

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

    def test_packaged_config_is_what_the_handler_loads(self):
        import zipfile

        out = self.pkg.build(self.tmp / "c")
        unpacked = self.tmp / "unpacked"
        with zipfile.ZipFile(out["zip"]) as z:
            z.extractall(unpacked)
        with mock.patch.dict(os.environ, {"SUBSAPI_CONFIG_DIR": str(unpacked / "subsapi" / "config")}):
            self.assertEqual(len(config_mod.load().destinations), 95)


class TemplateTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "b14", API_DIR.parents[1] / "stacks" / "build_m14_subscriptions_api.py")
        self.b = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.b)
        self.t = self.b.template("379959319681", "artifacts/flight-subscriptions-api/x.zip",
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
                                     "S3Key": "artifacts/flight-subscriptions-api/x.zip"})
        env = f["Environment"]["Variables"]
        self.assertEqual((env["SUBSCRIPTIONS_TABLE"], env["QUOTA_TABLE"]), ("subscriptions", "subscription_quota"))
        self.assertNotIn("ALLOW_DIRECT_TEST_INVOKE", env)

    def test_rejects_secret_keys(self):
        with self.assertRaises(ValueError):
            self.b.template("1", "k", "https://x.supabase.co", "sb_secret_abc")

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

    def patch(self, route, body, token="good"):
        return self.call("PATCH /subscriptions/{route}", body, path={"route": route}, token=token)

    def delete(self, route, if_updated_at=None):
        return self.call("DELETE /subscriptions/{route}", path={"route": route},
                         query={"if_updated_at": if_updated_at} if if_updated_at else None)

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
        self.assertEqual(body | {"created_at": None, "updated_at": None}, {
            "route": "TPE-LON", "origin": "TPE", "destination": "LON",
            "origin_name": {"zh": "台北", "en": "Taipei"}, "destination_name": {"zh": "倫敦", "en": "London"},
            "departure_month": "2027-03", "month_mode": "fixed", "effective_month": "2027-03",
            "status": "active", "target_price": 28000, "currency": "TWD", "alerts_paused": False,
            "plan_name": None, "created_at": None, "updated_at": None, "fare_check": None})
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
        self.assertIn("TPE-LON", self.raw(USER))
        self.assertEqual(self.raw(OTHER), {})
        self.assertEqual(self.call("GET /subscriptions", token=None)[0], 401)
        self.assertEqual(self.call("GET /subscriptions", token="forged")[1]["error"], "unauthorized")
        self.assertEqual(self.call("GET /subscriptions", token="other")[1]["subscriptions"], [])

    def test_duplicate_route_even_with_another_month(self):
        self.create("LON", month="2027-01")
        status, body = self.create("LON", month="2027-05")
        self.assertEqual((status, body["error"]), (409, "duplicate_subscription"))
        self.assertIn("台北 → 倫敦", body["message"])
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
            self.seed(USER, f"TPE-{d}", alerts_paused=True, departure_month="2027-02")
        for d in DESTS[8:10]:
            self.seed(USER, f"TPE-{d}", departure_month="2026-05")  # expired
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
        self.assertEqual(self.patch("TPE-OSA", {"alerts_paused": True})[0], 200)
        self.assertEqual(self.create("LON")[1]["error"], "limit_reached")
        self.assertEqual(self.patch("TPE-OSA", {"alerts_paused": False})[0], 200)  # resume needs no slot
        self.assertEqual(self.quota(), before)  # pause/resume never touch the quota
        self.assertEqual(self.delete("TPE-OSA")[0], 204)
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

    def test_target_and_month(self):
        status, body = self.patch("TPE-LON", {"target_price": 25000, "departure_month": "2027-06"})
        self.assertEqual(status, 200)
        self.assertEqual((body["target_price"], body["departure_month"]), (25000, "2027-06"))
        self.assertGreater(body["updated_at"], self.row["updated_at"])
        self.assertEqual(body["created_at"], self.row["created_at"])
        self.assertEqual(self.raw()["TPE-LON"]["departure_month"], "2027-06")

    def test_pause_resume_stores_flag_only_when_true(self):
        self.assertTrue(self.patch("TPE-LON", {"alerts_paused": True})[1]["alerts_paused"])
        self.assertIs(self.raw()["TPE-LON"]["alerts_paused"], True)
        body = self.patch("TPE-LON", {"alerts_paused": False})[1]
        self.assertEqual((body["alerts_paused"], body["status"]), (False, "active"))
        self.assertNotIn("alerts_paused", self.raw()["TPE-LON"])

    def test_no_op_writes_nothing(self):
        status, body = self.patch("TPE-LON", {"target_price": 28000, "departure_month": "2027-01"})
        self.assertEqual((status, body["updated_at"]), (200, self.row["updated_at"]))

    def test_cannot_return_to_rolling_or_leave_the_window(self):
        self.assertEqual(self.patch("TPE-LON", {"departure_month": None})[1]["error"], "invalid_departure_month")
        self.assertEqual(self.patch("TPE-LON", {"departure_month": "2028-01"})[1]["error"], "invalid_departure_month")

    def test_route_change_is_atomic(self):
        self.patch("TPE-LON", {"alerts_paused": True})
        status, body = self.patch("TPE-LON", {"origin": "KHH", "destination": "PAR", "target_price": 30000})
        self.assertEqual(status, 200)
        self.assertEqual((body["route"], body["status"], body["departure_month"]), ("KHH-PAR", "paused", "2027-01"))
        rows = self.raw()
        self.assertEqual(sorted(rows), ["KHH-PAR"])
        self.assertEqual(rows["KHH-PAR"]["previous_route"], "TPE-LON")
        self.assertNotEqual(rows["KHH-PAR"]["created_at"], self.row["created_at"])
        self.assertEqual(self.quota()["count"], 1)  # unchanged, and untouched
        self.assertEqual(self.quota()["version"], 1)

    def test_route_change_onto_existing_route(self):
        self.create("PAR")
        status, body = self.patch("TPE-LON", {"origin": "TPE", "destination": "PAR"})
        self.assertEqual((status, body["error"]), (409, "duplicate_subscription"))
        self.assertEqual(sorted(self.raw()), ["TPE-LON", "TPE-PAR"])

    def test_optimistic_concurrency(self):
        status, body = self.patch("TPE-LON", {"target_price": 1, "if_updated_at": "2020-01-01T00:00:00.000Z"})
        self.assertEqual((status, body["error"]), (409, "conflict"))
        ok = self.patch("TPE-LON", {"target_price": 2, "if_updated_at": self.row["updated_at"]})
        self.assertEqual(ok[0], 200)
        stale = self.patch("TPE-LON", {"target_price": 3, "if_updated_at": self.row["updated_at"]})
        self.assertEqual(stale[1]["error"], "conflict")

    def test_concurrent_change_between_read_and_write_is_a_conflict(self):
        real_get = self.store.get_row

        def get_then_someone_edits(email, route):
            row = real_get(email, route)
            self.store.update(email, route, {"target_price": 1, "updated_at": "2099-01-01T00:00:00.000Z"}, [])
            return row

        with mock.patch.object(self.store, "get_row", side_effect=get_then_someone_edits):
            status, body = self.patch("TPE-LON", {"target_price": 5})
        self.assertEqual((status, body["error"]), (409, "conflict"))

    def test_shape_errors_and_missing_rows(self):
        self.assertEqual(self.patch("TPE-LON", {})[1]["error"], "empty_patch")
        self.assertEqual(self.patch("TPE-LON", {"origin": "KHH"})[1]["error"], "route_change_incomplete")
        self.assertEqual(self.patch("TPE-LON", {"nope": 1})[1]["error"], "unknown_field")
        self.assertEqual(self.patch("TPE-PAR", {"target_price": 1})[0], 404)
        self.assertEqual(self.patch("bad", {"target_price": 1})[1]["error"], "invalid_route")

    def test_delete(self):
        status, body = self.delete("TPE-LON", if_updated_at="2020-01-01T00:00:00.000Z")
        self.assertEqual((status, body["error"]), (409, "conflict"))
        self.assertEqual(self.delete("TPE-LON", if_updated_at=self.row["updated_at"])[0], 204)
        self.assertEqual(self.delete("TPE-LON")[0], 404)
        self.assertEqual(self.quota()["count"], 0)


class ExpiryTests(LocalDynamoTestCase):
    def test_expire_then_reactivate(self):
        self.create("LON", month="2026-12")
        self.now = datetime(2027, 1, 5, tzinfo=timezone.utc)  # window now 2027-02 … 2028-01
        listing = self.call("GET /subscriptions")[1]
        self.assertEqual(listing["subscriptions"][0]["status"], "expired")
        # Editing the target keeps the expired month; it stays expired.
        body = self.patch("TPE-LON", {"target_price": 1, "departure_month": "2026-12"})[1]
        self.assertEqual(body["status"], "expired")
        self.assertEqual(self.patch("TPE-LON", {"departure_month": "2027-01"})[1]["error"], "invalid_departure_month")
        body = self.patch("TPE-LON", {"departure_month": "2027-04"})[1]
        self.assertEqual((body["status"], body["effective_month"]), ("active", "2027-04"))


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
        self.assertEqual((body["month_mode"], body["plan_name"], body["created_at"]),
                         ("fixed", "tokyo", "2026-10-30T00:00:00+00:00"))
        self.assertEqual(self.patch("TPE-TYO", {"departure_month": None})[1]["error"], "invalid_departure_month")

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

    def test_existing_route_updates_in_place(self):
        self.create("TYO", month="2027-03")
        self.patch("TPE-TYO", {"alerts_paused": True})
        created = self.raw()["TPE-TYO"]["created_at"]
        version = self.quota()["version"]
        self.assertEqual(self.legacy("tokyo", 7777)[0], 200)
        row = self.raw()["TPE-TYO"]
        self.assertEqual((row["target_price"], row["plan_name"], row["departure_month"], row["alerts_paused"],
                          row["created_at"]), (7777, "tokyo", "2027-03", True, created))
        self.assertEqual(self.quota()["version"], version)

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
        self.assertEqual(sorted(self.raw()), ["TPE-LON"])
        self.assert_invariants()

    def test_creates_and_deletes_at_the_limit(self):
        for d in DESTS[:10]:
            self.create(d)
        deletes = [lambda d=d: self.svc_call("delete", USER, f"TPE-{d}") for d in DESTS[:5]]
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
        pauses = [lambda i=i: self.svc_call("patch", USER, f"TPE-{DESTS[i % 4]}", {"alerts_paused": i % 2 == 0})
                  for i in range(20)]
        edits = [lambda: self.svc_call("patch", USER, "TPE-SPK", {"origin": "KHH", "destination": "SPK"})
                 for _ in range(5)]
        creates = [self.create_call(d) for d in DESTS[10:40]]
        results = self.run_parallel(pauses + edits + creates)
        self.assertTrue(all(s in (200, 409) for s, _ in results[:20]), self.codes(results[:20]))
        self.assertTrue(all(s in (200, 404, 409) for s, _ in results[20:25]), self.codes(results[20:25]))
        self.assertEqual(sum(s == 200 for s, _ in results[20:25]), 1, self.codes(results[20:25]))
        self.assertLessEqual(sum(s == 201 for s, _ in results[25:]), 2, self.codes(results[25:]))
        self.assertIn("KHH-SPK", self.raw())
        self.assertNotIn("TPE-SPK", self.raw())
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
