"""Shared HTTP contract examples for the M1.4 subscriptions API (Phase 3B-A).

aws/tests/vectors/subscriptions-http.json holds concrete request/response pairs. They are
checked from both sides:

* here: every example is replayed through the real Lambda handler (index.handler, the real
  Service and Store) against a **local** DynamoDB-compatible endpoint (ddb_local.py). The status
  code, the complete response body and the stored rows afterwards must match exactly;
* in src/lib/m14/http-client.contract.test.ts: HttpSubscriptionsClient must send exactly the
  example's request and return (or throw) exactly what its response means.

So the frontend and backend cannot drift apart without one of the suites failing. The clock and
the version-token generator are fixed per example, so bodies are deterministic. Supabase is
replaced by a fake that knows one test token; there is no network access and no AWS.

Run: python3 -m unittest -v aws/tests/test_m14_http_contract.py
"""
import importlib.util
import json
import re
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ddb_local  # noqa: E402  (must come before anything imports boto3)

API_DIR = Path(__file__).resolve().parents[1] / "lambdas" / "flight-subscriptions-api"
sys.path.insert(0, str(API_DIR))
from subsapi import config as config_mod  # noqa: E402
from subsapi import subkeys  # noqa: E402

if ddb_local.boto3 is not None:
    from subsapi import service as service_mod  # noqa: E402
    from subsapi import store as store_mod  # noqa: E402

VECTORS_PATH = Path(__file__).resolve().parent / "vectors" / "subscriptions-http.json"
VECTORS = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
CFG = config_mod.load({})
NOW = datetime.fromisoformat(VECTORS["now"].replace("Z", "+00:00"))
USER, OTHER = VECTORS["user"], VECTORS["other_user"]
OPS = {"list": "GET", "create": "POST", "update": "PATCH", "remove": "DELETE"}
TEST_DOMAIN = re.compile(r"^[a-z0-9.+-]+@example\.(com|org|net)$")


def route_of(method, path):
    """API Gateway route key and (decoded) path parameters for a request path."""
    if path == "/subscriptions":
        return f"{method} /subscriptions", {}
    m = re.fullmatch(r"/subscriptions/([^/]+)", path)
    if not m:
        raise ValueError(f"unexpected path {path!r}")
    return f"{method} /subscriptions/{{id}}", {"id": unquote(m.group(1))}


def load_index():
    spec = importlib.util.spec_from_file_location("flight_subscriptions_api_index_contract", API_DIR / "index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class VectorShapeTests(unittest.TestCase):
    """Stdlib only: the examples themselves are consistent and contain no real personal data."""

    def test_users_are_reserved_test_addresses(self):
        for email in (USER, OTHER):
            self.assertRegex(email, TEST_DOMAIN)
        text = VECTORS_PATH.read_text(encoding="utf-8")
        for email in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", text):
            self.assertRegex(email.lower(), TEST_DOMAIN, email)

    def test_names_are_unique_and_seeds_exist(self):
        names = [c["name"] for c in VECTORS["cases"]]
        self.assertEqual(len(names), len(set(names)))
        for case in VECTORS["cases"]:
            for seed in case.get("seed", []) + case.get("seed_other", []):
                self.assertIn(seed, VECTORS["rows"], (case["name"], seed))

    def test_client_operation_matches_http_request(self):
        for case in VECTORS["cases"]:
            client, http = case["client"], case["http"]
            with self.subTest(case["name"]):
                self.assertEqual(OPS[client["op"]], http["method"])
                if client["op"] in ("update", "remove"):
                    self.assertEqual(http["path"], f"/subscriptions/{client['id']}")
                if client["op"] == "remove":
                    v = client.get("if_version")
                    self.assertEqual(http["query"], None if v is None else {"if_version": v})
                    self.assertIsNone(http["body"])
                if client["op"] == "update":
                    self.assertEqual(http["body"], client["patch"])
                if client["op"] == "create":
                    self.assertEqual(http["body"], client["request"])

    def test_examples_cover_the_contract(self):
        seen = {(c["response"]["status"], (c["response"]["body"] or {}).get("error")) for c in VECTORS["cases"]}
        for expected in [(200, None), (201, None), (204, None), (400, "invalid_target_price"),
                         (400, "invalid_departure_month"), (400, "invalid_version"), (404, "not_found"),
                         (409, "conflict"), (409, "duplicate_subscription"), (409, "limit_reached"),
                         (428, "version_required")]:
            self.assertIn(expected, seen)
        rows = VECTORS["rows"]
        self.assertTrue(any("version_token" not in r and "#" not in r["route"] for r in rows.values()),
                        "a legacy rolling row")
        self.assertTrue(any("updated_at" not in r for r in rows.values()), "a row without timestamps")


@unittest.skipUnless(ddb_local.available(), "boto3/moto (or DDB_ENDPOINT) not available")
class HttpContractTests(unittest.TestCase):
    """Replays every example through the real handler and compares the complete result."""

    @classmethod
    def setUpClass(cls):
        cls.client = ddb_local.client()
        cls.index = load_index()

    def setUp(self):
        self.subs_table, self.quota_table = ddb_local.create_tables(self.client)
        self.store = store_mod.Store(self.client, self.subs_table, self.quota_table)
        token = VECTORS["token"]

        def resolve(event, env):
            h = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
            return USER if h.get("authorization") == f"Bearer {token}" else None

        self.patches = [mock.patch.object(self.index, "resolve_email", resolve),
                        mock.patch.object(self.index, "_log", lambda *a, **kw: None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        ddb_local.drop_tables(self.client, self.subs_table, self.quota_table)

    def seed(self, email, names):
        for name in names:
            item = {"email": email, **VECTORS["rows"][name]}
            self.client.put_item(TableName=self.subs_table, Item=store_mod.to_item(item))

    def stored(self, email):
        return {r["route"]: r for r in self.store.list_rows(email)}

    def run_case(self, case):
        """(status, body, stored rows of USER, stored rows of OTHER) for one example."""
        self.seed(USER, case.get("seed", []))
        self.seed(OTHER, case.get("seed_other", []))
        tokens = iter(case.get("new_tokens", []))
        svc = service_mod.Service(self.store, CFG, clock=lambda: NOW, sleep=lambda s: None,
                                  new_token=lambda: next(tokens))
        self.index.configure(svc)
        http = case["http"]
        route_key, params = route_of(http["method"], http["path"])
        event = {"version": "2.0", "routeKey": route_key, "requestContext": {"http": {}},
                 "headers": {"Authorization": f"Bearer {VECTORS['token']}"},
                 "body": "" if http["body"] is None else json.dumps(http["body"]),
                 "isBase64Encoded": False, "pathParameters": params or None,
                 "queryStringParameters": http["query"]}
        r = self.index.handler(event, None)
        body = json.loads(r["body"]) if r.get("body") else None
        return r["statusCode"], body, self.stored(USER), self.stored(OTHER)

    def test_every_example_matches_the_real_handler(self):
        for case in VECTORS["cases"]:
            with self.subTest(case["name"]):
                try:
                    status, body, mine, theirs = self.run_case(case)
                    expected = case["response"]
                    self.assertEqual(status, expected["status"])
                    self.assertEqual(body, expected["body"])
                    self.assertEqual(sorted(mine), sorted(case["stored_keys_after"]))
                    seeded_other = {VECTORS["rows"][n]["route"] for n in case.get("seed_other", [])}
                    self.assertEqual(set(theirs), seeded_other, "another user's rows never change")
                    if status >= 400:  # failed writes change nothing at all
                        seeded = {VECTORS["rows"][n]["route"]: {"email": USER, **VECTORS["rows"][n]}
                                  for n in case.get("seed", [])}
                        self.assertEqual(json.loads(json.dumps(mine, default=int)), seeded)
                finally:
                    self.tearDown()
                    self.setUp()

    def test_example_semantics(self):
        """Readable spot checks, so the examples cannot silently encode wrong behaviour."""
        cases = {c["name"]: c for c in VECTORS["cases"]}
        listed = {s["id"]: s for s in cases["list_mixed"]["response"]["body"]["subscriptions"]}
        rolling = listed["TPE-TYO"]
        self.assertEqual((rolling["month_mode"], rolling["departure_month"], rolling["plan_name"]),
                         ("rolling", None, "tokyo"))
        self.assertTrue(rolling["version"].startswith("legacy-"))
        no_stamps = listed["TPE-SEL"]
        self.assertEqual((no_stamps["created_at"], no_stamps["updated_at"]), (None, None))
        self.assertEqual(listed["TPE-OSA_2027-01"]["version"], VECTORS["rows"]["fixed_jan"]["version_token"])
        self.assertEqual(listed["KHH-OKA_2027-02"]["status"], "paused")
        self.assertEqual(listed["RMQ-SEL_2026-10"]["status"], "expired")
        self.assertNotIn("TPE-PAR_2027-01", listed, "another user's subscription is never listed")
        self.assertEqual(cases["list_at_limit"]["response"]["body"]["limits"], {"max": 10, "used": 10})
        self.assertEqual(cases["create_limit_reached"]["response"]["body"]["details"], {"max": 10, "used": 10})
        created = cases["create_fixed_month"]["response"]["body"]
        self.assertEqual(created["version"], cases["create_fixed_month"]["new_tokens"][0])
        moved = cases["update_month_move"]["response"]["body"]
        self.assertEqual(moved["id"], "TPE-OSA_2027-02")
        self.assertEqual(subkeys.public_id("TPE-OSA#2027-02"), moved["id"])
        paused = cases["update_pause_legacy_rolling"]
        self.assertTrue(paused["client"]["patch"]["if_version"].startswith("legacy-"))
        self.assertEqual(paused["response"]["body"]["status"], "paused")
        self.assertFalse(paused["response"]["body"]["version"].startswith("legacy-"))


if __name__ == "__main__":
    unittest.main()
