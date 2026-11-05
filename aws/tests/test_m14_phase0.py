"""Offline tests for M1.4 Phase 0: the imported M1.1 Lambdas, the M1.4 config files, the IAM
design documents, and a guard that the frozen notification Lambda is unchanged.

boto3 and the network are stubbed; no AWS calls.
Run: python3 -m unittest discover -s aws/tests -v
"""
import hashlib
import importlib.util
import io
import json
import re
import sys
import types
import unittest
import urllib.error
from decimal import Decimal
from pathlib import Path
from unittest import mock

AWS = Path(__file__).resolve().parents[1]
LAMBDAS = AWS / "lambdas"


def _stub_boto3():
    boto3 = types.ModuleType("boto3")
    boto3.client = mock.MagicMock(name="client")
    boto3.resource = mock.MagicMock(name="resource")
    dynamodb = types.ModuleType("boto3.dynamodb")
    conditions = types.ModuleType("boto3.dynamodb.conditions")

    class Key:
        def __init__(self, name):
            self.name = name

        def eq(self, value):
            return ("eq", self.name, value)

    conditions.Key = conditions.Attr = Key
    sys.modules.update({"boto3": boto3, "boto3.dynamodb": dynamodb, "boto3.dynamodb.conditions": conditions})


def _load(path, name):
    _stub_boto3()
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _apigw(body=None, auth="Bearer good-token"):
    headers = {"Authorization": auth} if auth else {}
    return {"requestContext": {"http": {"method": "POST"}}, "headers": headers, "body": json.dumps(body or {})}


ENV = {"SUPABASE_URL": "https://example.supabase.co", "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_test"}


class SaveSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.patch_env = mock.patch.dict("os.environ", ENV)
        self.patch_env.start()
        self.s = _load(LAMBDAS / "flight-save-subscription" / "index.py", "save_sub")
        self.s.T = mock.MagicMock()

    def tearDown(self):
        self.patch_env.stop()

    def _supabase(self, email="User@Example.com", fail=False):
        if fail:
            return mock.patch.object(self.s.urllib.request, "urlopen",
                                     side_effect=urllib.error.HTTPError("u", 401, "no", {}, None))
        return mock.patch.object(self.s.urllib.request, "urlopen",
                                 side_effect=lambda *a, **k: io.BytesIO(json.dumps({"email": email}).encode()))

    def status(self, out):
        return out["statusCode"], json.loads(out["body"])

    def test_requires_a_valid_supabase_session(self):
        self.assertEqual(self.status(self.s.handler(_apigw({"plan_name": "tokyo"}, auth=None), None))[0], 401)
        with self._supabase(fail=True):
            self.assertEqual(self.status(self.s.handler(_apigw({"plan_name": "tokyo"}), None))[0], 401)
        self.s.T.update_item.assert_not_called()

    def test_identity_comes_from_the_session_not_the_body(self):
        with self._supabase():
            out = self.s.handler(_apigw({"plan_name": "tokyo", "target_price": 9000, "email": "attacker@example.com"}), None)
        self.assertEqual(out["statusCode"], 200)
        self.assertEqual(self.s.T.update_item.call_args.kwargs["Key"], {"email": "user@example.com", "route": "TPE-TYO"})

    def test_validates_plan_and_target_price(self):
        with self._supabase():
            self.assertEqual(self.status(self.s.handler(_apigw({"plan_name": "paris", "target_price": 9000}), None)),
                             (400, {"error": "invalid_plan"}))
            for bad in (0, 1000001, 12.5, True, "9000", None):
                code, body = self.status(self.s.handler(_apigw({"plan_name": "seoul", "target_price": bad}), None))
                self.assertEqual((code, body["error"]), (400, "invalid_target_price"), bad)
        self.s.T.update_item.assert_not_called()

    def test_writes_twd_row_without_subscription_status(self):
        with self._supabase():
            code, body = self.status(self.s.handler(_apigw({"plan_name": "seoul", "target_price": 6000}), None))
        self.assertEqual((code, body["route"], body["currency"]), (200, "TPE-SEL", "TWD"))
        kw = self.s.T.update_item.call_args.kwargs
        self.assertEqual(kw["ExpressionAttributeValues"][":t"], Decimal(6000))
        self.assertNotIn("subscription_status", kw["UpdateExpression"])
        self.assertIn("created_at=if_not_exists(created_at,:n)", kw["UpdateExpression"])

    def test_direct_invoke_uses_test_email(self):
        out = self.s.handler({"test_email": " Tester@Example.com ", "plan_name": "tokyo", "target_price": 9000}, None)
        self.assertEqual(out["statusCode"], 200)
        self.assertEqual(self.s.T.update_item.call_args.kwargs["Key"]["email"], "tester@example.com")


class ListSubscriptionsTests(unittest.TestCase):
    def test_lists_only_the_callers_rows(self):
        with mock.patch.dict("os.environ", ENV):
            l = _load(LAMBDAS / "flight-list-subscriptions" / "index.py", "list_sub")
        l.T = mock.MagicMock()
        l.T.query.return_value = {"Items": [{"email": "user@example.com", "route": "TPE-TYO", "plan_name": "tokyo",
                                             "target_price": Decimal(9000), "updated_at": "2026-11-04T07:17:29+00:00"}]}
        out = l.handler({"test_email": "user@example.com"}, None)
        body = json.loads(out["body"])
        self.assertEqual(body["subscriptions"][0]["target_price"], 9000)
        self.assertEqual(body["subscriptions"][0]["currency"], "TWD")
        self.assertEqual(l.T.query.call_args.kwargs["KeyConditionExpression"], ("eq", "email", "user@example.com"))
        self.assertEqual(l.handler({}, None)["statusCode"], 401)


class SubscribeApiTemplateTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("b11", AWS / "stacks" / "build_m1_subscribe_api.py")
        self.b = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.b)

    def test_template_embeds_repo_code_and_keeps_existing_routes(self):
        t = self.b.template("123456789012", ENV["SUPABASE_URL"], ENV["SUPABASE_PUBLISHABLE_KEY"])
        for logical, name in (("SaveFn", "flight-save-subscription"), ("ListFn", "flight-list-subscriptions")):
            props = t["Resources"][logical]["Properties"]
            self.assertEqual(props["Code"]["ZipFile"], (LAMBDAS / name / "index.py").read_text(encoding="utf-8"))
            self.assertEqual(props["Environment"]["Variables"], ENV)
        keys = sorted(r["Properties"]["RouteKey"] for r in t["Resources"].values() if r["Type"] == "AWS::ApiGatewayV2::Route")
        self.assertEqual(keys, ["GET /subscriptions", "POST /subscribe"])

    def test_no_publishable_key_is_committed(self):
        for p in [AWS / "stacks" / "build_m1_subscribe_api.py", *(LAMBDAS.glob("*/index.py"))]:
            self.assertNotRegex(p.read_text(encoding="utf-8"), r"sb_(publishable|secret)_[A-Za-z0-9_-]{10,}", p)


class AirportsConfigTests(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads((AWS / "config" / "airports.json").read_text(encoding="utf-8"))

    def test_schema(self):
        self.assertEqual(self.cfg["schema_version"], 2)
        regions = [r["id"] for r in self.cfg["regions"]]
        self.assertEqual(regions, ["asia", "middle_east", "europe", "north_america", "latin_america",
                                   "australia", "new_zealand"])
        for r in self.cfg["regions"]:
            self.assertTrue(r["name_zh"].strip() and r["name_en"].strip())
        for group in ("origins", "destinations"):
            for a in self.cfg[group]:
                self.assertRegex(a["code"], r"^[A-Z]{3}$")
                self.assertIn(a["type"], {"city", "airport"})
                self.assertRegex(a["country"], r"^[A-Z]{2}$")
                self.assertTrue(a["name_zh"].strip() and a["name_en"].strip())
                if a["type"] == "city":
                    # A city code names the airports it searches; an airport entry lists none.
                    self.assertTrue(a["airports"], a["code"])
                    self.assertEqual(len(a["airports"]), len(set(a["airports"])))
                    for ap in a["airports"]:
                        self.assertRegex(ap, r"^[A-Z]{3}$")
                else:
                    self.assertNotIn("airports", a)
        for a in self.cfg["destinations"]:
            self.assertIn(a["region"], regions)

    def test_lists(self):
        self.assertEqual([a["code"] for a in self.cfg["origins"]], ["TPE", "KHH", "RMQ"])
        dest = [a["code"] for a in self.cfg["destinations"]]
        self.assertTrue(80 <= len(dest) <= 100, len(dest))
        self.assertEqual(len(dest), len(set(dest)), "duplicate destination codes")
        self.assertFalse(set(dest) & {a["code"] for a in self.cfg["origins"]})
        names = [(a["name_zh"], a["name_en"]) for a in self.cfg["destinations"]]
        self.assertEqual(len(names), len(set(names)), "duplicate destination names")

    def test_original_destinations_preserved(self):
        original = ["TYO", "OSA", "NGO", "FUK", "OKA", "SPK", "KMJ", "SEL", "PUS", "CJU", "HKG", "MFM",
                    "BKK", "CNX", "HKT", "SIN", "KUL", "PEN", "BKI", "MNL", "CEB", "SGN", "HAN", "DAD", "DPS"]
        self.assertEqual([a["code"] for a in self.cfg["destinations"]][:25], original)

    def test_requested_destinations_present(self):
        dest = {a["code"]: a for a in self.cfg["destinations"]}
        requested = {
            "europe": "LON PAR FRA MUC AMS ZRH VIE ROM MIL VCE MAD BCN LIS PRG CPH OSL HEL IST",
            "north_america": "LAX SFO NYC SEA CHI BOS WAS LAS MIA DFW HNL YVR YTO YMQ YYC MEX CUN",
            "latin_america": "SAO BUE",
            "australia": "SYD MEL BNE PER ADL CNS OOL",
            "new_zealand": "AKL CHC WLG ZQN",
        }
        for region, codes in requested.items():
            for code in codes.split():
                self.assertEqual(dest[code]["region"], region, code)
        countries = {a["country"] for a in self.cfg["destinations"] if a["region"] == "europe"}
        self.assertTrue({"GB", "FR", "DE", "NL", "CH", "AT", "IT", "ES", "PT", "CZ", "HU", "DK", "SE",
                         "NO", "FI", "IE", "GR", "TR"} <= countries)

    def test_city_and_airport_codes_do_not_overlap(self):
        # An airport searched by a city entry must not also be its own destination entry
        # (e.g. TYO and HND), or the same flights would be offered twice.
        cities = {a["code"]: set(a["airports"]) for a in self.cfg["destinations"] if a["type"] == "city"}
        airports = {a["code"] for a in self.cfg["destinations"] if a["type"] == "airport"}
        for city, aps in cities.items():
            self.assertFalse(aps & airports, city)

    def test_known_metro_codes(self):
        dest = {a["code"]: a for a in self.cfg["destinations"]}
        for code in ("TYO", "SEL", "LON", "PAR", "NYC", "CHI", "WAS", "YTO", "YMQ", "ROM", "MIL", "SAO", "BUE"):
            self.assertEqual(dest[code]["type"], "city", code)
        self.assertEqual(dest["NYC"]["airports"], ["JFK", "EWR", "LGA"])
        for code in ("FRA", "AMS", "SYD", "AKL", "HNL"):
            self.assertEqual(dest[code]["type"], "airport", code)

    def test_existing_m1_routes_remain_valid(self):
        origins = {a["code"] for a in self.cfg["origins"]}
        dest = {a["code"] for a in self.cfg["destinations"]}
        for route in ("TPE-TYO", "TPE-SEL"):
            o, d = route.split("-")
            self.assertIn(o, origins)
            self.assertIn(d, dest)

    def test_names_match_notification_display_names(self):
        # names.json feeds the live notification Lambda; the allowlist must not contradict it.
        names = json.loads((AWS / "config" / "names.json").read_text(encoding="utf-8"))["cities"]
        allow = {a["code"]: a["name_zh"] for a in self.cfg["origins"] + self.cfg["destinations"]}
        for code, name in names.items():
            self.assertEqual(allow[code], name)


class PolicyConfigTests(unittest.TestCase):
    def test_policy(self):
        p = json.loads((AWS / "config" / "subscription-policy.json").read_text(encoding="utf-8"))
        self.assertEqual(p["schema_version"], 2)
        self.assertEqual(p["max_subscriptions_per_user"], 10)
        self.assertNotIn("max_active_subscriptions_per_user", p)
        self.assertEqual(p["departure_month"], {"format": "YYYY-MM", "first_offset_months": 1,
                                                "window_months": 12, "timezone": "Asia/Taipei"})
        self.assertEqual(p["currency"], "TWD")
        self.assertEqual(p["target_price_twd"], {"min": 1, "max": 1000000})


class IamDesignTests(unittest.TestCase):
    def setUp(self):
        self.docs = {p.stem: json.loads(p.read_text()) for p in (AWS / "iam").glob("*.json")}

    def allows(self, name):
        return [s for s in self.docs[name]["Statement"] if s["Effect"] == "Allow"]

    def test_no_wildcard_actions_in_any_allow(self):
        for name, doc in self.docs.items():
            for s in doc["Statement"]:
                if s["Effect"] != "Allow":
                    continue
                for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"]):
                    self.assertNotRegex(a, r"(^\*$|:\*$)", f"{name}: {a}")

    def test_resources_are_scoped(self):
        for name in [n for n in self.docs if n.endswith("-policy")]:
            for s in self.allows(name):
                res = s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]]
                if res == ["*"]:
                    self.assertEqual(s["Action"], ["cloudformation:ValidateTemplate"], f"{name}: {s['Sid']}")

    def test_github_trust_is_pinned_to_this_repo(self):
        for name, env_only in (("gha-plan-trust", False), ("gha-deploy-trust", True)):
            cond = self.docs[name]["Statement"][0]["Condition"]
            self.assertEqual(cond["StringEquals"]["token.actions.githubusercontent.com:aud"], "sts.amazonaws.com")
            subs = cond["StringEquals"]["token.actions.githubusercontent.com:sub"]
            self.assertTrue(all(s.startswith("repo:d6140085-dotcom/flight-fare-finder:") for s in subs))
            if env_only:
                self.assertEqual(subs, ["repo:d6140085-dotcom/flight-fare-finder:environment:production"])

    def test_plan_role_can_never_execute(self):
        deny = [s for s in self.docs["gha-plan-policy"]["Statement"] if s["Effect"] == "Deny"][0]
        self.assertIn("cloudformation:ExecuteChangeSet", deny["Action"])

    def test_subscriptions_api_has_no_secret_access(self):
        actions = [a for s in self.allows("subscriptions-api-role-policy") for a in s["Action"]]
        self.assertFalse([a for a in actions if a.split(":")[0] in {"ssm", "kms", "secretsmanager", "sqs", "lambda"}])
        resources = [s["Resource"] for s in self.allows("subscriptions-api-role-policy")]
        self.assertIn("arn:aws:dynamodb:ap-southeast-2:379959319681:table/subscriptions", resources)
        self.assertNotIn("notification_history", json.dumps(resources))

    def test_cfn_exec_cannot_touch_data_secrets_or_iam(self):
        deny = [s for s in self.docs["cfn-exec-policy"]["Statement"] if s["Effect"] == "Deny"][0]["Action"]
        for a in ("dynamodb:DeleteTable", "dynamodb:UpdateTable", "sqs:PurgeQueue", "ssm:GetParameter*", "iam:Create*"):
            self.assertIn(a, deny)


class FrozenM1Tests(unittest.TestCase):
    def test_notification_lambda_is_unchanged(self):
        # sha256 of aws/lambdas/flight-fare-notification/index.py as merged to main (220986a) and deployed.
        code = (LAMBDAS / "flight-fare-notification" / "index.py").read_bytes()
        self.assertEqual(hashlib.sha256(code).hexdigest(),
                         "f1ccdf0292c03c6a595381ab027e0a893a2d5a5e9487b991496de311c80c969f")


if __name__ == "__main__":
    unittest.main()
