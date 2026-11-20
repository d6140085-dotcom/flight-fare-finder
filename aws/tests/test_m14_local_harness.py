"""Phase 3B-B: tests for the localhost-only HTTP harness (aws/tools/local_api.py).

* Unit tests (stdlib only): route matching, the payload-2.0 event, CORS decisions and the
  planned B3 CORS configuration, and every start-up isolation guard.
* Start-up refusals: the harness *process* refuses unsafe environments (exit 2, no URL printed).
* Exclusion: the harness is not in the Lambda ZIP, the app source or any automatic workflow.
* HTTP integration: real HTTP requests to a harness process -> the real index.handler -> the
  local DynamoDB. With ``DDB_ENDPOINT`` set this is **DynamoDB Local**; otherwise moto, which is
  reported as such and is not a DynamoDB Local result. Covers all 18 shared contract examples
  (aws/tests/vectors/subscriptions-http.json), authentication, cross-user isolation, multi-month
  operations, the 10-subscription limit, 50-request concurrency bursts, version protection,
  gateway behaviour (404, 413, 429/502/503/504) and CORS.

Run: python3 -m unittest -v aws/tests/test_m14_local_harness.py
     DDB_ENDPOINT=http://127.0.0.1:8000 python3 -m unittest -v aws/tests/test_m14_local_harness.py
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "aws" / "tools" / "local_api.py"
VECTORS = json.loads((ROOT / "aws" / "tests" / "vectors" / "subscriptions-http.json").read_text(encoding="utf-8"))
ORIGIN = "http://localhost:5173"
EVIL = "https://evil.example"
M14_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")

spec = importlib.util.spec_from_file_location("local_api_under_test", HARNESS)
local_api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local_api)


def clean_env(**changes):
    """A minimal environment: no proxies, no real credentials. DDB_ENDPOINT is passed through."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"),
           "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    if os.environ.get("DDB_ENDPOINT"):
        env["DDB_ENDPOINT"] = os.environ["DDB_ENDPOINT"]
    for k, v in changes.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    return env


def deps_available():
    find = importlib.util.find_spec
    if find("boto3") is None or find("cryptography") is None:
        return False
    return bool(os.environ.get("DDB_ENDPOINT")) or find("moto") is not None


# =================================================================================================
# Unit tests
# =================================================================================================

class RouteAndEventTests(unittest.TestCase):
    def test_routes(self):
        m = local_api.match_route
        self.assertEqual(m("GET", "/subscriptions"), ("GET /subscriptions", None))
        self.assertEqual(m("POST", "/subscriptions"), ("POST /subscriptions", None))
        self.assertEqual(m("POST", "/subscribe"), ("POST /subscribe", None))
        self.assertEqual(m("PATCH", "/subscriptions/TPE-OSA_2027-01"),
                         ("PATCH /subscriptions/{id}", {"id": "TPE-OSA_2027-01"}))
        self.assertEqual(m("DELETE", "/subscriptions/a%2Fb%20c"), ("DELETE /subscriptions/{id}", {"id": "a/b c"}))
        for method, path in [("PUT", "/subscriptions"), ("GET", "/subscriptions/"), ("GET", "/subscriptions/x"),
                             ("PATCH", "/subscriptions/a/b"), ("GET", "/"), ("GET", "/subscribe"),
                             ("DELETE", "/subscriptions"), ("PATCH", "/subscriptions/")]:
            self.assertIsNone(m(method, path), (method, path))

    def test_payload_v2_event(self):
        e = local_api.to_event("DELETE", "/subscriptions/TPE-TYO", "if_version=legacy-ab%2Bc&x=1&x=2",
                               [("Authorization", "Bearer t"), ("X-Multi", "a"), ("x-multi", "b")], b"",
                               "DELETE /subscriptions/{id}", {"id": "TPE-TYO"})
        self.assertEqual(e["version"], "2.0")
        self.assertEqual(e["routeKey"], "DELETE /subscriptions/{id}")
        self.assertEqual(e["rawPath"], "/subscriptions/TPE-TYO")
        self.assertEqual(e["headers"], {"authorization": "Bearer t", "x-multi": "a,b"})
        self.assertEqual(e["queryStringParameters"], {"if_version": "legacy-ab+c", "x": "1,2"})
        self.assertEqual(e["pathParameters"], {"id": "TPE-TYO"})
        self.assertNotIn("body", e)
        self.assertFalse(e["isBase64Encoded"])
        self.assertIn("requestContext", e)  # so the handler always takes the Supabase path
        self.assertEqual(e["requestContext"]["http"]["method"], "DELETE")

    def test_body_is_kept_or_base64_encoded(self):
        e = local_api.to_event("POST", "/subscriptions", "", [], '{"a":"台北"}'.encode(), "POST /subscriptions", None)
        self.assertEqual(e["body"], '{"a":"台北"}')
        self.assertNotIn("queryStringParameters", e)
        e = local_api.to_event("POST", "/subscriptions", "", [], b"\xff\xfe", "POST /subscriptions", None)
        self.assertTrue(e["isBase64Encoded"])
        self.assertEqual(e["body"], "//4=")


class CorsTests(unittest.TestCase):
    cors = local_api.load_cors()

    def test_planned_b3_configuration_is_explicit(self):
        c = self.cors
        self.assertTrue(c["b3_allow_origins"])
        for origin in c["b3_allow_origins"]:
            self.assertRegex(origin, r"^https://[a-z0-9.-]+$")
            self.assertNotIn("*", origin)
            self.assertNotIn("-git-", origin, "Vercel preview origins must not call the production API")
        self.assertEqual(c["allow_methods"], ["DELETE", "GET", "OPTIONS", "PATCH", "POST"])
        self.assertEqual(c["allow_headers"], ["authorization", "content-type"])
        self.assertFalse(c["allow_credentials"])
        for origin in c["local_allow_origins"]:
            self.assertTrue(local_api._is_loopback_url(origin), origin)

    def test_decisions(self):
        h = local_api.cors_headers
        allowed = [ORIGIN]
        self.assertEqual(h(self.cors, allowed, ORIGIN), {"Access-Control-Allow-Origin": ORIGIN})
        self.assertEqual(h(self.cors, allowed, EVIL), {})
        self.assertEqual(h(self.cors, allowed, None), {})
        pre = h(self.cors, allowed, ORIGIN, ("PATCH", "Authorization, Content-Type"))
        self.assertEqual(pre["Access-Control-Allow-Methods"], "DELETE,GET,OPTIONS,PATCH,POST")
        self.assertEqual(pre["Access-Control-Allow-Headers"], "authorization,content-type")
        self.assertEqual(pre["Access-Control-Max-Age"], "600")
        self.assertEqual(h(self.cors, allowed, ORIGIN, ("PUT", "")), {})
        self.assertEqual(h(self.cors, allowed, ORIGIN, ("GET", "x-evil")), {})
        self.assertEqual(h(self.cors, allowed, EVIL, ("GET", "")), {})
        self.assertNotIn("Access-Control-Allow-Credentials", pre)

    def test_wildcard_origin_is_refused(self):
        with self.assertRaises(local_api.Refused):
            local_api.Harness(["*"])


class GuardTests(unittest.TestCase):
    BAD = {
        "DDB_ENDPOINT": ("https://dynamodb.ap-southeast-2.amazonaws.com", "DDB_ENDPOINT"),
        "SUPABASE_URL": ("https://abcdefghijklmnop.supabase.co", "SUPABASE_URL"),
        "SUPABASE_PUBLISHABLE_KEY": ("sb_publishable_realLookingKey123", "SUPABASE_PUBLISHABLE_KEY"),
        "SUPABASE_SERVICE_ROLE_KEY": ("x", "SUPABASE_SERVICE_ROLE_KEY"),
        "AWS_ACCESS_KEY_ID": ("AKIAABCDEFGHIJKLMNOP", "AWS_ACCESS_KEY_ID"),
        "AWS_SECRET_ACCESS_KEY": ("not-testing", "AWS_SECRET_ACCESS_KEY"),
        "AWS_SESSION_TOKEN": ("IQoJb3JpZ2lu", "AWS_SESSION_TOKEN"),
        "AWS_PROFILE": ("default", "AWS_PROFILE"),
        "AWS_ROLE_ARN": ("arn:aws:iam::379959319681:role/x", "AWS_ROLE_ARN"),
        "AWS_CONTAINER_CREDENTIALS_FULL_URI": ("http://169.254.170.2/x", "AWS_CONTAINER_CREDENTIALS_FULL_URI"),
        "AWS_LAMBDA_FUNCTION_NAME": ("flight-subscriptions-api", "AWS_LAMBDA_FUNCTION_NAME"),
        "SUBSCRIPTIONS_TABLE": ("subscriptions", "SUBSCRIPTIONS_TABLE"),
        "QUOTA_TABLE": ("subscription_quota", "QUOTA_TABLE"),
        "VITE_FLIGHT_API_URL": ("https://vgc6ctabv7.execute-api.ap-southeast-2.amazonaws.com", "API Gateway URL"),
        "SOME_SECRET": ("sb_secret_abcdef", "secret key"),
    }

    def test_clean_environment_passes(self):
        self.assertEqual(local_api.environment_problems(clean_env()), [])
        local_api.check_environment(clean_env())
        self.assertEqual(local_api.environment_problems(
            {**clean_env(), **local_api.FAKE_AWS, "SUBSCRIPTIONS_TABLE": "test-subscriptions-1",
             "DDB_ENDPOINT": "http://127.0.0.1:8000", "SUPABASE_URL": "https://127.0.0.1:4443",
             "SUPABASE_PUBLISHABLE_KEY": local_api.FAKE_SUPABASE_KEY}), [])

    def test_each_unsafe_setting_is_refused(self):
        for name, (value, reason) in self.BAD.items():
            with self.subTest(name):
                problems = local_api.environment_problems({**clean_env(), name: value})
                self.assertTrue(any(reason in p for p in problems), problems)
                with self.assertRaises(local_api.Refused):
                    local_api.check_environment({**clean_env(), name: value})

    def test_only_loopback_binding(self):
        for host in ("0.0.0.0", "192.168.1.10", "::"):
            with self.assertRaises(local_api.Refused):
                local_api.check_environment(clean_env(), host=host)

    def test_fault_injection_accepts_only_gateway_faults(self):
        h = local_api.Harness()
        for status in (429, 502, 503, 504):
            h.queue_fault(status)
        for bad in (200, 401, 500, "503", None):
            with self.assertRaises(ValueError):
                h.queue_fault(bad)
        with self.assertRaises(ValueError):
            h.queue_fault(503, count=0)


@unittest.skipUnless(deps_available(), "boto3/cryptography and moto (or DDB_ENDPOINT) not available")
class StartupRefusalTests(unittest.TestCase):
    """The harness process itself refuses to start: exit 2, a reason on stderr, no URL printed."""

    def run_harness(self, env, *args):
        return subprocess.run([sys.executable, str(HARNESS), "serve", *args], env=env, capture_output=True,
                              text=True, timeout=60)

    def test_refusals(self):
        cases = {
            "non-local DynamoDB": clean_env(DDB_ENDPOINT="http://dynamodb.ap-southeast-2.amazonaws.com"),
            "real-looking AWS key": clean_env(AWS_ACCESS_KEY_ID="AKIAABCDEFGHIJKLMNOP", AWS_SECRET_ACCESS_KEY="x"),
            "AWS profile": clean_env(AWS_PROFILE="default"),
            "production table": clean_env(SUBSCRIPTIONS_TABLE="subscriptions"),
            "external Supabase": clean_env(SUPABASE_URL="https://abcdefghijklmnop.supabase.co"),
            "production API URL": clean_env(VITE_FLIGHT_API_URL="https://x1.execute-api.ap-southeast-2.amazonaws.com"),
            "Supabase secret": clean_env(SUPABASE_SERVICE_ROLE_KEY="eyJhbGciOi"),
        }
        for name, env in cases.items():
            with self.subTest(name):
                r = self.run_harness(env)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn("refusing to start the local harness", r.stderr)
                self.assertEqual(r.stdout, "")

    def test_wildcard_origin_refused(self):
        r = self.run_harness(clean_env(), "--allow-origin", "*")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")


class ExclusionTests(unittest.TestCase):
    """The harness never ships: not in the Lambda ZIP, the app source or an automatic workflow."""

    def test_not_in_the_lambda_zip(self):
        import tempfile
        import zipfile

        spec_ = importlib.util.spec_from_file_location("pkg_subs_harness",
                                                       ROOT / "aws" / "scripts" / "package_subscriptions_api.py")
        pkg = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(pkg)
        out = pkg.build(Path(tempfile.mkdtemp()))
        with zipfile.ZipFile(out["zip"]) as z:
            names = z.namelist()
            self.assertFalse([n for n in names if "local_api" in n or "tools/" in n or n.endswith("cors.json")])
            for n in names:
                self.assertNotIn(b"FakeSupabase", z.read(n), n)

    def test_not_imported_by_the_app(self):
        for path in (ROOT / "src").rglob("*"):
            if path.suffix in (".ts", ".tsx") and ".test." not in path.name:
                self.assertNotIn("local_api", path.read_text(encoding="utf-8"), path)
                self.assertNotIn("aws/tools", path.read_text(encoding="utf-8"), path)

    def test_workflows_that_run_it_cannot_reach_or_deploy_anything(self):
        """Phase 3B-D runs the harness in automatic CI. Any workflow that does must be read-only,
        secret-free and deployment-free, with fake AWS credentials and no pull_request_target."""
        runners = []
        for wf in (ROOT / ".github" / "workflows").glob("*.y*ml"):
            text = wf.read_text(encoding="utf-8")
            if not ("local_api" in text or "local_harness" in text or "test:harness" in text
                    or "test:e2e" in text):
                continue
            runners.append(wf.name)
            with self.subTest(wf.name):
                self.assertRegex(text, r"(?m)^permissions:\s*\n\s+contents: read\s*$")
                self.assertNotRegex(text, r"(?m)^\s+(?!contents)[a-z-]+: write\s*$")
                self.assertNotIn("pull_request_target", text)
                self.assertNotIn("secrets.", text)
                self.assertNotIn("id-token", text)
                self.assertNotIn("aws-actions/", text)
                self.assertNotRegex(text, r"(?i)\b(vercel deploy|cloudformation deploy|sam deploy|cdk deploy|"
                                          r"aws (lambda|cloudformation|iam|ssm|dynamodb|apigateway|events)\b)")
                for value in re.findall(r"(?m)^\s+AWS_(?:ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN): (\S+)", text):
                    self.assertEqual(value, "testing")
                for endpoint in re.findall(r"(?m)DDB_ENDPOINT: (\S+)", text):
                    self.assertTrue(local_api._is_loopback_url(endpoint), endpoint)
                for uses in re.findall(r"(?m)uses: ([^\s#]+)", text):
                    self.assertRegex(uses, r"@[0-9a-f]{40}$", f"{uses} must be pinned to a commit SHA")
        self.assertIn("ci.yml", runners)


# =================================================================================================
# HTTP integration through a harness process
# =================================================================================================

class Http:
    """A tiny HTTP client that never uses a proxy and never follows redirects."""

    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method, path, body=None, token="harness-token-user", headers=None, raw=None):
        h = dict(headers or {})
        if token is not None:
            h["Authorization"] = f"Bearer {token}"
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        if data is not None:
            h.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with self.opener.open(req, timeout=60) as r:
                status, hdrs, payload = r.status, dict(r.headers.items()), r.read()
        except urllib.error.HTTPError as e:
            status, hdrs, payload = e.code, dict(e.headers.items()), e.read()
        hdrs = {k.lower(): v for k, v in hdrs.items()}
        return status, hdrs, (json.loads(payload) if payload else None), payload


@unittest.skipUnless(deps_available(), "boto3/cryptography and moto (or DDB_ENDPOINT) not available")
class HarnessIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proc = subprocess.Popen([sys.executable, str(HARNESS), "serve", "--control"], env=clean_env(),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        line = cls.proc.stdout.readline()
        if not line:
            raise RuntimeError("harness did not start: " + cls.proc.stderr.read())
        cls.info = json.loads(line)
        cls.http = Http(cls.info["api_url"])
        cls.ctl = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        print(f"\n[local harness engine: {cls.info['engine']}]", file=sys.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=20)
        cls.proc.stdout.close()
        cls.proc.stderr.close()

    def control(self, method, path, body=None, secret=None):
        req = urllib.request.Request(self.info["control_url"] + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Harness-Control": secret or self.info["control_token"],
                                              "Content-Type": "application/json"})
        try:
            with self.ctl.open(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def reset(self, now=None, new_tokens=None, rows=None):
        status, out = self.control("POST", "/reset", {"now": now, "new_tokens": new_tokens, "rows": rows or {}})
        self.assertEqual(status, 200, out)
        self.assertTrue(all(t.startswith("test-") for t in out["tables"]))

    def setUp(self):
        self.reset()

    def state(self):
        return self.control("GET", "/state")[1]

    def rows(self, email="user@example.com"):
        return {r["route"]: r for r in self.state()["rows"].get(email, [])}

    def create(self, dest, month, target=9000, token="harness-token-user", origin="TPE"):
        return self.http.call("POST", "/subscriptions", {"origin": origin, "destination": dest,
                                                         "departure_month": month, "target_price": target},
                              token=token)

    # ---- harness itself ------------------------------------------------------------------
    def test_engine_and_isolation_report(self):
        expected = "dynamodb-local" if os.environ.get("DDB_ENDPOINT") else "moto"
        self.assertEqual(self.info["engine"], expected)
        for key in ("api_url", "control_url"):
            self.assertRegex(self.info[key], r"^http://127\.0\.0\.1:\d+$")
        self.assertNotIn("*", self.info["allow_origins"])

    def test_control_port_requires_the_secret(self):
        self.assertEqual(self.control("GET", "/state", secret="wrong")[0], 403)
        self.assertEqual(self.control("POST", "/fault", {"status": 503}, secret="wrong")[0], 403)
        self.assertEqual(self.control("POST", "/fault", {"status": 200})[0], 400)
        # The API port has no control routes.
        self.assertEqual(self.http.call("POST", "/reset", {})[0], 404)

    # ---- the 18 shared contract examples ------------------------------------------------
    def test_shared_contract_examples(self):
        rows = VECTORS["rows"]
        for case in VECTORS["cases"]:
            with self.subTest(case["name"]):
                seeded = {VECTORS["user"]: [rows[n] for n in case.get("seed", [])],
                          VECTORS["other_user"]: [rows[n] for n in case.get("seed_other", [])]}
                self.reset(VECTORS["now"], case.get("new_tokens", []), seeded)
                http = case["http"]
                path = http["path"]
                if http["query"]:
                    path += "?" + "&".join(f"{k}={urllib.request.quote(v, safe='')}" for k, v in http["query"].items())
                status, headers, body, raw = self.http.call(http["method"], path, http["body"], token=VECTORS["token"],
                                                            headers={"Origin": ORIGIN})
                expected = case["response"]
                self.assertEqual(status, expected["status"])
                self.assertEqual(body, expected["body"])
                self.assertEqual(headers.get("access-control-allow-origin"), ORIGIN)
                if status == 204:
                    self.assertEqual(raw, b"")
                    self.assertEqual(headers.get("content-length"), "0")
                else:
                    self.assertEqual(headers.get("content-type"), "application/json; charset=utf-8")
                st = self.state()
                mine = {r["route"]: r for r in st["rows"].get(VECTORS["user"], [])}
                theirs = st["rows"].get(VECTORS["other_user"], [])
                self.assertEqual(sorted(mine), sorted(case["stored_keys_after"]))
                self.assertEqual(theirs, [{**r, "email": VECTORS["other_user"]} for r in seeded[VECTORS["other_user"]]])
                if status >= 400:  # nothing written
                    self.assertEqual(list(mine.values()),
                                     sorted(({**r, "email": VECTORS["user"]} for r in seeded[VECTORS["user"]]),
                                            key=lambda r: r["route"]))
                if http["method"] in ("POST", "PATCH") and status in (200, 201):  # stored = returned version
                    key = next(k for k in mine if k.replace("#", "_") == body["id"])
                    self.assertEqual(mine[key]["version_token"], body["version"])
                    self.assertEqual(body["version"], case["new_tokens"][0])
                    self.assertRegex(mine[key]["updated_at"], M14_STAMP)
                route_key = {"GET": "GET /subscriptions", "POST": "POST /subscriptions",
                             "PATCH": "PATCH /subscriptions/{id}", "DELETE": "DELETE /subscriptions/{id}"}
                self.assertEqual(st["requests"], {route_key[http["method"]]: 1}, "exactly one request, no retry")

    # ---- A. authentication -----------------------------------------------------------------
    def test_authentication(self):
        self.reset(rows={"user@example.com": [VECTORS["rows"]["fixed_jan"]]})
        before = self.state()["rows"]
        for token, headers in [(None, {}), ("unknown-token", {}), ("harness-token-expired", {}), ("", {}),
                               (None, {"Authorization": "Basic dXNlcjpwdw=="}),
                               (None, {"Authorization": "Bearer"})]:
            for method, path, body in [("GET", "/subscriptions", None),
                                       ("POST", "/subscriptions", {"origin": "TPE", "destination": "LON",
                                                                   "departure_month": "2027-03", "target_price": 1}),
                                       ("PATCH", "/subscriptions/TPE-OSA_2027-01",
                                        {"target_price": 1, "if_version": VECTORS["rows"]["fixed_jan"]["version_token"]}),
                                       ("DELETE", "/subscriptions/TPE-OSA_2027-01?if_version="
                                        + VECTORS["rows"]["fixed_jan"]["version_token"], None)]:
                with self.subTest(token=token, headers=headers, method=method):
                    status, _, out, _ = self.http.call(method, path, body, token=token, headers=headers)
                    self.assertEqual((status, out["error"]), (401, "unauthorized"))
        self.assertEqual(self.state()["rows"], before)
        status, _, out, _ = self.http.call("GET", "/subscriptions")
        self.assertEqual([s["id"] for s in out["subscriptions"]], ["TPE-OSA_2027-01"])

    def test_cross_user_isolation(self):
        theirs = VECTORS["rows"]["other_users_fixed"]
        self.reset(rows={"other@example.com": [theirs]})
        self.assertEqual(self.http.call("GET", "/subscriptions?email=other%40example.com")[2]["subscriptions"], [])
        self.assertEqual(self.http.call("PATCH", "/subscriptions/TPE-PAR_2027-01",
                                        {"target_price": 1, "if_version": theirs["version_token"]})[0], 404)
        self.assertEqual(self.http.call("DELETE", "/subscriptions/TPE-PAR_2027-01?if_version="
                                        + theirs["version_token"] + "&email=other%40example.com")[0], 404)
        # An email in the body is ignored (contract §2): the row is created for the session's user.
        status, _, body, _ = self.http.call("POST", "/subscriptions", {"origin": "TPE", "destination": "PAR",
                                                                       "departure_month": "2027-01", "target_price": 1,
                                                                       "email": "other@example.com"})
        self.assertEqual((status, body["id"]), (201, "TPE-PAR_2027-01"))
        st = self.state()["rows"]
        self.assertEqual(st["other@example.com"], [{**theirs, "email": "other@example.com"}])
        self.assertEqual([r["route"] for r in st["user@example.com"]], ["TPE-PAR#2027-01"])
        other_view = self.http.call("GET", "/subscriptions", token="harness-token-other")[2]
        self.assertEqual([s["id"] for s in other_view["subscriptions"]], ["TPE-PAR_2027-01"])
        self.assertEqual(other_view["subscriptions"][0]["target_price"], 20000)
        third = self.http.call("GET", "/subscriptions", token="harness-token-third")[2]
        self.assertEqual(third["subscriptions"], [])

    # ---- B. operations -----------------------------------------------------------------------
    def test_multi_month_lifecycle_with_version_chaining(self):
        ids = {}
        for month in ("2027-01", "2027-02", "2027-03"):
            status, _, body, _ = self.create("TYO", month, 9000)
            self.assertEqual(status, 201)
            ids[month] = body
        listed = self.http.call("GET", "/subscriptions")[2]
        self.assertEqual([s["id"] for s in listed["subscriptions"]],
                         ["TPE-TYO_2027-01", "TPE-TYO_2027-02", "TPE-TYO_2027-03"])
        self.assertEqual(listed["limits"], {"max": 10, "used": 3})
        feb = ids["2027-02"]
        status, _, feb2, _ = self.http.call("PATCH", f"/subscriptions/{feb['id']}",
                                            {"target_price": 8000, "if_version": feb["version"]})
        self.assertEqual((status, feb2["target_price"]), (200, 8000))
        self.assertNotEqual(feb2["version"], feb["version"])
        mar = ids["2027-03"]
        status, _, paused, _ = self.http.call("PATCH", f"/subscriptions/{mar['id']}",
                                              {"alerts_paused": True, "if_version": mar["version"]})
        self.assertEqual((status, paused["status"]), (200, "paused"))
        status, _, resumed, _ = self.http.call("PATCH", f"/subscriptions/{mar['id']}",
                                               {"alerts_paused": False, "if_version": paused["version"]})
        self.assertEqual((status, resumed["status"]), (200, "active"))
        jan = ids["2027-01"]
        status, _, moved, _ = self.http.call("PATCH", f"/subscriptions/{jan['id']}",
                                             {"departure_month": "2027-04", "if_version": jan["version"]})
        self.assertEqual((status, moved["id"]), (200, "TPE-TYO_2027-04"))
        status, _, dup, _ = self.http.call("PATCH", f"/subscriptions/{feb2['id']}",
                                           {"departure_month": "2027-03", "if_version": feb2["version"]})
        self.assertEqual((status, dup["error"]), (409, "duplicate_subscription"))
        status, _, _, raw = self.http.call("DELETE", f"/subscriptions/{feb2['id']}?if_version={feb2['version']}")
        self.assertEqual((status, raw), (204, b""))
        stored = self.rows()
        self.assertEqual(sorted(stored), ["TPE-TYO#2027-03", "TPE-TYO#2027-04"])
        self.assertEqual(stored["TPE-TYO#2027-03"]["version_token"], resumed["version"])
        self.assertEqual(stored["TPE-TYO#2027-04"]["version_token"], moved["version"])
        self.assertEqual(self.state()["quota"]["user@example.com"]["count"], 2)

    def test_legacy_paused_and_expired_rows(self):
        r = VECTORS["rows"]
        self.reset(now=VECTORS["now"], rows={"user@example.com": [r["legacy_rolling_tokyo"], r["paused_feb"],
                                                                  r["expired_oct"]]})
        subs = {s["id"]: s for s in self.http.call("GET", "/subscriptions")[2]["subscriptions"]}
        self.assertEqual(subs["TPE-TYO"]["month_mode"], "rolling")
        self.assertTrue(subs["TPE-TYO"]["version"].startswith("legacy-"))
        self.assertEqual(subs["KHH-OKA_2027-02"]["status"], "paused")
        self.assertEqual(subs["RMQ-SEL_2026-10"]["status"], "expired")
        status, _, moved, _ = self.http.call("PATCH", "/subscriptions/TPE-TYO",
                                             {"departure_month": "2027-05", "if_version": subs["TPE-TYO"]["version"]})
        self.assertEqual((status, moved["id"], moved["month_mode"]), (200, "TPE-TYO_2027-05", "fixed"))
        self.assertFalse(moved["version"].startswith("legacy-"))

    def test_ten_subscription_limit(self):
        for i, month in enumerate(["2026-12", "2027-01", "2027-02", "2027-03", "2027-04", "2027-05", "2027-06",
                                   "2027-07", "2027-08", "2027-09"]):
            self.assertEqual(self.create("BKK", month)[0], 201, month)
        status, _, body, _ = self.create("BKK", "2027-10")
        self.assertEqual((status, body["error"], body["details"]), (409, "limit_reached", {"max": 10, "used": 10}))
        listed = self.http.call("GET", "/subscriptions")[2]
        self.assertEqual(listed["limits"], {"max": 10, "used": 10})
        first = listed["subscriptions"][0]
        self.assertEqual(self.http.call("DELETE", f"/subscriptions/{first['id']}?if_version={first['version']}")[0], 204)
        self.assertEqual(self.create("BKK", "2027-10")[0], 201)
        self.assertEqual(len(self.rows()), 10)
        self.assertEqual(self.state()["quota"]["user@example.com"]["count"], 10)

    # ---- C. concurrency ----------------------------------------------------------------------
    def burst(self, bodies, token="harness-token-user"):
        with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
            results = list(pool.map(lambda b: self.http.call("POST", "/subscriptions", b, token=token), bodies))
        return [(s, (b or {}).get("error")) for s, _, b, _ in results]

    def assert_consistent(self, email="user@example.com"):
        st = self.state()
        rows = st["rows"].get(email, [])
        self.assertLessEqual(len(rows), 10)
        keys = [(r["origin"], r["destination"], r.get("departure_month")) for r in rows]
        self.assertEqual(len(keys), len(set(keys)), "duplicate route/month")
        quota = st["quota"].get(email)
        if rows or quota:
            self.assertEqual(quota["count"], len(rows), "quota count must equal the stored rows")
        return rows

    def test_concurrent_creates_from_empty(self):
        dests = ["OSA", "NGO", "FUK", "OKA", "PUS", "HKG", "BKK", "SIN", "KUL", "MNL"]
        months = ["2027-01", "2027-02", "2027-03", "2027-04", "2027-05"]
        bodies = [{"origin": "TPE", "destination": dests[i % 10], "departure_month": months[i // 10],
                   "target_price": 9000} for i in range(50)]
        out = self.burst(bodies)
        created = sum(1 for s, _ in out if s == 201)
        self.assertFalse([o for o in out if o[0] >= 500], out)
        self.assertTrue({o for o in out if o[0] != 201} <= {(409, "limit_reached"), (409, "conflict")}, out)
        rows = self.assert_consistent()
        self.assertLessEqual(created, 10)
        self.assertGreaterEqual(created, 1)
        self.assertEqual(len(rows), created)
        print(f"[burst from empty: {created} created, "
              f"{sum(1 for o in out if o == (409, 'limit_reached'))} limit_reached, "
              f"{sum(1 for o in out if o == (409, 'conflict'))} conflict]", file=sys.stderr)

    def test_concurrent_creates_at_nine(self):
        nine = [{**VECTORS["rows"][f"limit_{i:02d}"]} for i in range(1, 10)]
        self.reset(rows={"user@example.com": nine})
        bodies = [{"origin": "TPE", "destination": d, "departure_month": "2027-06", "target_price": 1}
                  for d in ["OSA", "NGO", "FUK", "OKA", "PUS"] * 10]
        out = self.burst(bodies)
        self.assertFalse([o for o in out if o[0] >= 500], out)
        created = sum(1 for s, _ in out if s == 201)
        self.assertLessEqual(created, 1)
        rows = self.assert_consistent()
        self.assertEqual(len(rows), 9 + created)

    def test_concurrent_identical_creates(self):
        body = {"origin": "TPE", "destination": "LON", "departure_month": "2027-03", "target_price": 1}
        out = self.burst([body] * 50, token="harness-token-third")
        self.assertFalse([o for o in out if o[0] >= 500], out)
        self.assertTrue({o for o in out if o[0] != 201} <= {(409, "duplicate_subscription"), (409, "conflict")}, out)
        created = sum(1 for s, _ in out if s == 201)
        self.assertLessEqual(created, 1)
        rows = self.assert_consistent("third@example.com")
        self.assertEqual(len(rows), created)

    # ---- D. version protection --------------------------------------------------------------
    def test_version_protection_without_retry(self):
        row = VECTORS["rows"]["fixed_jan"]
        stale = "staleVersion0000000000"
        cases = [
            ("PATCH", "/subscriptions/TPE-OSA_2027-01", {"target_price": 1}, (428, "version_required")),
            ("PATCH", "/subscriptions/TPE-OSA_2027-01", {"target_price": 1, "if_version": stale}, (409, "conflict")),
            ("PATCH", "/subscriptions/TPE-OSA_2027-01", {"target_price": 1, "if_version": "bad version"},
             (400, "invalid_version")),
            ("DELETE", "/subscriptions/TPE-OSA_2027-01", None, (428, "version_required")),
            ("DELETE", f"/subscriptions/TPE-OSA_2027-01?if_version={stale}", None, (409, "conflict")),
            ("DELETE", "/subscriptions/TPE-OSA_2027-01?if_version=bad%20version", None, (400, "invalid_version")),
        ]
        for method, path, body, expected in cases:
            with self.subTest(method=method, path=path, body=body):
                self.reset(rows={"user@example.com": [row]})
                status, _, out, _ = self.http.call(method, path, body)
                self.assertEqual((status, out["error"]), expected)
                st = self.state()
                self.assertEqual(st["rows"]["user@example.com"], [{**row, "email": "user@example.com"}])
                self.assertEqual(sum(st["requests"].values()), 1, "one request; nothing retried")

    # ---- E. gateway behaviour --------------------------------------------------------------
    def test_gateway_404_for_unknown_routes(self):
        for method, path in [("GET", "/nope"), ("PUT", "/subscriptions"), ("GET", "/subscriptions/"),
                             ("GET", "/subscriptions/TPE-OSA_2027-01"), ("PATCH", "/subscriptions/a/b"),
                             ("DELETE", "/subscriptions")]:
            with self.subTest(method=method, path=path):
                status, headers, body, _ = self.http.call(method, path, headers={"Origin": ORIGIN})
                self.assertEqual((status, body), (404, {"message": "Not Found"}))
                self.assertEqual(headers.get("access-control-allow-origin"), ORIGIN)

    def test_injected_gateway_faults(self):
        for fault, message in [(429, "Too Many Requests"), (502, "Internal Server Error"),
                               (503, "Service Unavailable"), (504, "Endpoint request timed out")]:
            with self.subTest(fault):
                self.reset()
                self.assertEqual(self.control("POST", "/fault", {"status": fault})[0], 200)
                status, headers, body, _ = self.http.call(
                    "POST", "/subscriptions", {"origin": "TPE", "destination": "LON",
                                               "departure_month": "2027-03", "target_price": 1},
                    headers={"Origin": ORIGIN})
                self.assertEqual((status, body), (fault, {"message": message}))
                self.assertEqual(headers.get("access-control-allow-origin"), ORIGIN)
                self.assertEqual(self.state()["rows"], {}, "the function was never reached")
                self.assertEqual(self.create("LON", "2027-03")[0], 201, "the fault applied once")

    def test_malformed_empty_and_oversized_bodies(self):
        status, _, body, _ = self.http.call("POST", "/subscriptions", raw=b"{not json")
        self.assertEqual((status, body["error"]), (400, "invalid_json"))
        status, _, body, _ = self.http.call("POST", "/subscriptions", raw=b"")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "origin_required")
        big = json.dumps({"origin": "TPE", "destination": "LON", "departure_month": "2027-03",
                          "target_price": 1, "pad": "x" * 5000}).encode()
        status, _, body, _ = self.http.call("POST", "/subscriptions", raw=big)
        self.assertEqual((status, body["error"]), (400, "invalid_json"))  # the handler's 4 KiB limit
        status, _, body, _ = self.http.call("POST", "/subscriptions", raw=b"x" * (1024 * 1024 + 1))
        self.assertEqual((status, body), (413, {"message": "Request Entity Too Large"}))
        self.assertEqual(self.state()["rows"], {})

    def test_empty_204_response(self):
        status, _, created, _ = self.create("LON", "2027-03")
        self.assertEqual(status, 201)
        status, headers, body, raw = self.http.call(
            "DELETE", f"/subscriptions/{created['id']}?if_version={created['version']}")
        self.assertEqual((status, body, raw, headers.get("content-length")), (204, None, b"", "0"))

    # ---- F. CORS ----------------------------------------------------------------------------
    def preflight(self, origin, method, headers="authorization,content-type", path="/subscriptions/TPE-OSA_2027-01"):
        h = {"Origin": origin, "Access-Control-Request-Method": method}
        if headers:
            h["Access-Control-Request-Headers"] = headers
        status, hdrs, body, raw = self.http.call("OPTIONS", path, token=None, headers=h)
        self.assertEqual((status, raw), (204, b""))
        return hdrs

    def test_preflights(self):
        for method in ("PATCH", "DELETE", "GET", "POST"):
            with self.subTest(method):
                h = self.preflight(ORIGIN, method, path="/subscriptions" if method in ("GET", "POST")
                                   else "/subscriptions/TPE-OSA_2027-01")
                self.assertEqual(h["access-control-allow-origin"], ORIGIN)
                self.assertEqual(h["access-control-allow-methods"], "DELETE,GET,OPTIONS,PATCH,POST")
                self.assertEqual(h["access-control-allow-headers"], "authorization,content-type")
                self.assertEqual(h["access-control-max-age"], "600")
                self.assertNotIn("access-control-allow-credentials", h)
        for origin, method, headers in [(EVIL, "PATCH", "authorization"), (ORIGIN, "PUT", "authorization"),
                                        (ORIGIN, "PATCH", "authorization,x-evil"), ("null", "DELETE", None)]:
            with self.subTest(origin=origin, method=method, headers=headers):
                h = self.preflight(origin, method, headers)
                self.assertFalse([k for k in h if k.startswith("access-control-")], h)
        self.assertEqual(self.state()["rows"], {})

    def test_cors_on_actual_responses(self):
        status, h, _, _ = self.http.call("GET", "/subscriptions", headers={"Origin": ORIGIN})
        self.assertEqual((status, h.get("access-control-allow-origin")), (200, ORIGIN))
        status, h, _, _ = self.http.call("GET", "/subscriptions", headers={"Origin": EVIL})
        self.assertEqual(status, 200)  # served (the browser, not the server, blocks it) ...
        self.assertNotIn("access-control-allow-origin", h)  # ... because no CORS header is given
        status, h, _, _ = self.http.call("GET", "/subscriptions")
        self.assertNotIn("access-control-allow-origin", h)
        for method, path, body, token, expected in [
            ("GET", "/subscriptions", None, None, 401),
            ("PATCH", "/subscriptions/TPE-XXX_2027-01", {"target_price": 1, "if_version": "Z" * 22},
             "harness-token-user", 404),
            ("POST", "/subscriptions", {"origin": "TPE"}, "harness-token-user", 400),
        ]:
            with self.subTest(expected):
                status, h, _, _ = self.http.call(method, path, body, token=token, headers={"Origin": ORIGIN})
                self.assertEqual(status, expected)
                self.assertEqual(h.get("access-control-allow-origin"), ORIGIN)
                self.assertNotEqual(h.get("access-control-allow-origin"), "*")


if __name__ == "__main__":
    unittest.main()
