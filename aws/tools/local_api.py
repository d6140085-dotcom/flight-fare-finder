"""M1.4 Phase 3B-B: a localhost-only HTTP harness for the subscriptions API. DEV/TEST ONLY.

    HTTP client  ->  this server (127.0.0.1, random port)  ->  API Gateway HTTP API payload 2.0
    event  ->  the real index.handler (unchanged)  ->  local DynamoDB  ->  HTTP response

What is real and what is simulated:
* real: the Lambda handler, Service, Store, validation, quota protocol and Supabase auth code
  (``subsapi.auth.resolve_email`` runs unpatched and makes a real HTTPS request);
* simulated, locally: API Gateway's routing, event shape, CORS and gateway error responses; the
  Supabase ``/auth/v1/user`` endpoint (HTTPS on 127.0.0.1 with a throwaway certificate authority);
  DynamoDB (DynamoDB Local via ``DDB_ENDPOINT``, or moto in-process when it is unset).

Isolation (fail closed, see ``check_environment``): loopback only; fake AWS credentials only;
test-prefixed tables only; no production API URL, Supabase URL or Supabase key may be configured.
Proxy variables are removed and the process trusts only its own throwaway CA, so the auth code
cannot complete TLS with any real server. Nothing here is packaged into the Lambda ZIP or the
Vite build, and no workflow deploys it.

Usage (prints one JSON line with the URLs, then serves until killed):
    python3 aws/tools/local_api.py serve [--control] [--allow-origin ORIGIN ...]
With ``--control`` a second loopback port accepts test commands (reset/seed the tables, fix the
clock and version tokens, read the stored state, queue a 429/502/503/504 fault). Every control
request must carry the random secret printed at startup.
"""
import argparse
import base64
import datetime
import hmac
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import uuid
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlparse

ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "aws" / "lambdas" / "flight-subscriptions-api"
TESTS_DIR = ROOT / "aws" / "tests"
CORS_FILE = Path(__file__).resolve().parent / "cors.json"

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
PRODUCTION_TABLES = {"subscriptions", "subscription_quota", "notification_history"}
FAKE_AWS = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing"}
FAKE_SUPABASE_KEY = "sb_publishable_harness_local_only"
MAX_REQUEST_BYTES = 1024 * 1024  # larger bodies get the gateway's 413 (the handler itself allows 4 KiB)
FAULTS = {
    429: {"message": "Too Many Requests"},
    502: {"message": "Internal Server Error"},
    503: {"message": "Service Unavailable"},
    504: {"message": "Endpoint request timed out"},
}
# The planned M1.4 routes (contract §4, staged B3 cutover). {id} is one path segment.
ROUTES = {
    ("GET", "/subscriptions"),
    ("POST", "/subscriptions"),
    ("PATCH", "/subscriptions/{id}"),
    ("DELETE", "/subscriptions/{id}"),
    ("POST", "/subscribe"),
}

# Fake Supabase users: bearer token -> email (reserved test domains only), or None = expired.
FAKE_USERS = {
    "test-access-token": "user@example.com",  # the token of aws/tests/vectors/subscriptions-http.json
    "harness-token-user": "user@example.com",
    "harness-token-other": "other@example.com",
    "harness-token-third": "third@example.com",
    "harness-token-expired": None,
}


class LoopbackServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a backlog large enough for 50-request bursts (the default is 5)."""
    request_queue_size = 256
    daemon_threads = True


class Refused(RuntimeError):
    """The environment is not isolated; the harness does not start."""


# ---- isolation guards ----------------------------------------------------------------------------

def _is_loopback_url(url, schemes=("http", "https")):
    try:
        u = urlparse(url)
    except ValueError:
        return False
    return u.scheme in schemes and (u.hostname or "") in LOOPBACK_HOSTS


def environment_problems(env):
    """Every reason this environment is not isolated (empty list = safe to start)."""
    problems = []
    ddb = env.get("DDB_ENDPOINT")
    if ddb is not None and not _is_loopback_url(ddb):
        problems.append("DDB_ENDPOINT is not a localhost URL")
    sup = env.get("SUPABASE_URL")
    if sup is not None and not _is_loopback_url(sup, ("https",)):
        problems.append("SUPABASE_URL points outside localhost")
    key = env.get("SUPABASE_PUBLISHABLE_KEY")
    if key is not None and key != FAKE_SUPABASE_KEY:
        problems.append("a real-looking SUPABASE_PUBLISHABLE_KEY is configured")
    for name in ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_SECRET_KEY", "SUPABASE_JWT_SECRET",
                 "SUPABASE_ACCESS_TOKEN", "SUPABASE_DB_PASSWORD"):
        if env.get(name):
            problems.append(f"{name} is set")
    for name, fake in FAKE_AWS.items():
        if env.get(name) not in (None, fake):
            problems.append(f"{name} is set to a non-test value")
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ROLE_ARN", "AWS_WEB_IDENTITY_TOKEN_FILE",
                 "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
                 "AWS_LAMBDA_FUNCTION_NAME", "AWS_EXECUTION_ENV"):
        if env.get(name):
            problems.append(f"{name} is set (real AWS identity or runtime)")
    for name in ("SUBSCRIPTIONS_TABLE", "QUOTA_TABLE"):
        table = env.get(name)
        if table is not None and (table in PRODUCTION_TABLES or not table.startswith("test-")):
            problems.append(f"{name} is not a test- table")
    for name, value in env.items():
        if not isinstance(value, str):
            continue
        if re.search(r"execute-api\.[a-z0-9-]+\.amazonaws\.com", value, re.I):
            problems.append(f"{name} contains an API Gateway URL")
        if re.search(r"\bsb_secret_", value) or re.search(r"\bAKIA[0-9A-Z]{16}\b", value):
            problems.append(f"{name} contains a secret key")
        if re.search(r"https://[a-z0-9]+\.supabase\.co", value, re.I):
            problems.append(f"{name} contains a Supabase project URL")
    for name in ("VITE_FLIGHT_API_URL", "FLIGHT_API_URL", "M14_API_URL"):
        if env.get(name) and not _is_loopback_url(env[name]):
            problems.append(f"{name} is not a localhost URL")
    return sorted(set(problems))


def check_environment(env, host="127.0.0.1"):
    problems = environment_problems(env)
    if host not in ("127.0.0.1", "::1"):
        problems.append("the harness binds only to 127.0.0.1 or ::1")
    if problems:
        raise Refused("refusing to start the local harness: " + "; ".join(problems))


def isolate_process_environment(ca_file, ca_dir):
    """After the checks pass: fake AWS credentials, no proxies, trust only the throwaway CA."""
    for name in list(os.environ):
        if name.lower() in ("http_proxy", "https_proxy", "all_proxy") or name in ("AWS_CA_BUNDLE",
                                                                                  "REQUESTS_CA_BUNDLE"):
            del os.environ[name]
    os.environ.update(FAKE_AWS)
    os.environ.update({"AWS_DEFAULT_REGION": "ap-southeast-2", "AWS_EC2_METADATA_DISABLED": "true",
                       "NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1",
                       "SSL_CERT_FILE": str(ca_file), "SSL_CERT_DIR": str(ca_dir)})


# ---- API Gateway (HTTP API, payload 2.0) simulation ----------------------------------------------

def load_cors(path=CORS_FILE):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def match_route(method, raw_path):
    """(routeKey, pathParameters) for a request, or None (the gateway's 404)."""
    if (method, raw_path) in ROUTES:
        return f"{method} {raw_path}", None
    m = re.fullmatch(r"/subscriptions/([^/]+)", raw_path)
    if m and (method, "/subscriptions/{id}") in ROUTES:
        return f"{method} /subscriptions/{{id}}", {"id": unquote(m.group(1))}
    return None


def to_event(method, raw_path, raw_query, headers, body, route_key, path_params):
    """The payload-2.0 event API Gateway would send for this request."""
    lowered = {}
    for name, value in headers:
        k = name.lower()
        lowered[k] = f"{lowered[k]},{value}" if k in lowered else value
    query = {}
    for k, v in parse_qsl(raw_query, keep_blank_values=True):
        query[k] = f"{query[k]},{v}" if k in query else v
    now = datetime.datetime.now(datetime.timezone.utc)
    event = {
        "version": "2.0",
        "routeKey": route_key,
        "rawPath": raw_path,
        "rawQueryString": raw_query,
        "headers": lowered,
        "requestContext": {
            "accountId": "000000000000", "apiId": "local-harness", "domainName": "127.0.0.1",
            "http": {"method": method, "path": raw_path, "protocol": "HTTP/1.1", "sourceIp": "127.0.0.1",
                     "userAgent": lowered.get("user-agent", "")},
            "requestId": uuid.uuid4().hex, "routeKey": route_key, "stage": "$default",
            "time": now.strftime("%d/%b/%Y:%H:%M:%S +0000"), "timeEpoch": int(now.timestamp() * 1000),
        },
        "isBase64Encoded": False,
    }
    if query:
        event["queryStringParameters"] = query
    if path_params:
        event["pathParameters"] = path_params
    if body:
        try:
            event["body"] = body.decode("utf-8")
        except UnicodeDecodeError:
            event["body"], event["isBase64Encoded"] = base64.b64encode(body).decode(), True
    return event


def cors_headers(cors, origins, origin, preflight=None):
    """CORS response headers, or {} when the origin (or preflight request) is not allowed."""
    if not origin or origin not in origins:
        return {}
    out = {"Access-Control-Allow-Origin": origin}
    if preflight is not None:
        method, requested = preflight
        wanted = [h.strip().lower() for h in (requested or "").split(",") if h.strip()]
        if method not in cors["allow_methods"] or not set(wanted) <= set(cors["allow_headers"]):
            return {}
        out.update({"Access-Control-Allow-Methods": ",".join(cors["allow_methods"]),
                    "Access-Control-Allow-Headers": ",".join(cors["allow_headers"]),
                    "Access-Control-Max-Age": str(cors["max_age"])})
    elif cors["expose_headers"]:
        out["Access-Control-Expose-Headers"] = ",".join(cors["expose_headers"])
    if cors["allow_credentials"]:
        out["Access-Control-Allow-Credentials"] = "true"
    return out


# ---- fake Supabase (HTTPS on loopback, throwaway CA) ---------------------------------------------

def make_certificates(directory):
    """A throwaway CA and a 127.0.0.1 server certificate it signs (valid for one day)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "flight-fare-finder local harness CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
          .not_valid_after(now + datetime.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                       crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    d = Path(directory)
    (d / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (d / "server.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (d / "server.key").write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                                     serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()))
    empty = d / "no-other-cas"
    empty.mkdir()
    return d / "ca.pem", empty, d / "server.pem", d / "server.key"


class FakeSupabaseHandler(BaseHTTPRequestHandler):
    """GET /auth/v1/user as Supabase answers it, for the fixed fake tokens only."""
    users = FAKE_USERS

    def log_message(self, *args):
        pass

    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path != "/auth/v1/user":
            return self._send(404, {"message": "no route"})
        if self.headers.get("apikey") != FAKE_SUPABASE_KEY:
            return self._send(401, {"message": "No API key found in request"})
        auth = self.headers.get("Authorization") or ""
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if token not in self.users:
            return self._send(401, {"code": 401, "error_code": "bad_jwt",
                                    "msg": "invalid JWT: unable to parse or verify signature"})
        email = self.users[token]
        if email is None:
            return self._send(401, {"code": 401, "error_code": "bad_jwt",
                                    "msg": "invalid JWT: token has invalid claims: token is expired"})
        return self._send(200, {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, email)), "aud": "authenticated",
                                "role": "authenticated", "email": email})


# ---- the harness -----------------------------------------------------------------------------

def _plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    return value


class Harness:
    def __init__(self, allow_origins=None, control=False):
        self.cors = load_cors()
        self.origins = list(allow_origins) if allow_origins else list(self.cors["local_allow_origins"])
        if "*" in self.origins:
            raise Refused("refusing a wildcard CORS origin")
        self.control_enabled = control
        self.control_token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.faults = []
        self.requests = {}
        self.clock_value = None
        self.tokens = None
        self.tables = None

    # -- setup ------------------------------------------------------------------------------
    def start(self):
        check_environment(dict(os.environ))
        self.tmp = tempfile.mkdtemp(prefix="m14-harness-")
        ca_file, ca_dir, cert, key = make_certificates(self.tmp)
        isolate_process_environment(ca_file, ca_dir)

        import ssl

        self.supabase = LoopbackServer(("127.0.0.1", 0), FakeSupabaseHandler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self.supabase.socket = ctx.wrap_socket(self.supabase.socket, server_side=True)
        threading.Thread(target=self.supabase.serve_forever, daemon=True).start()
        os.environ["SUPABASE_URL"] = f"https://127.0.0.1:{self.supabase.server_address[1]}"
        os.environ["SUPABASE_PUBLISHABLE_KEY"] = FAKE_SUPABASE_KEY
        check_environment(dict(os.environ))  # again, with the harness's own settings in place

        sys.path.insert(0, str(TESTS_DIR))
        sys.path.insert(0, str(API_DIR))
        import ddb_local  # localhost-only endpoint guard and fake credentials

        self.ddb_local = ddb_local
        self.engine = "dynamodb-local" if os.environ.get("DDB_ENDPOINT") else "moto"
        if not ddb_local.available():
            raise Refused("no local DynamoDB: set DDB_ENDPOINT to DynamoDB Local or install moto")
        self.client = ddb_local.client()
        if (urlparse(self.client.meta.endpoint_url).hostname or "") not in LOOPBACK_HOSTS:
            raise Refused("DynamoDB endpoint is not localhost")
        spec = importlib.util.spec_from_file_location("flight_subscriptions_api_index_harness", API_DIR / "index.py")
        self.index = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.index)
        from subsapi import config as config_mod
        from subsapi import service as service_mod
        from subsapi import store as store_mod

        self.config_mod, self.service_mod, self.store_mod = config_mod, service_mod, store_mod
        self.reset()

        self.api = LoopbackServer(("127.0.0.1", 0), self._api_handler())
        threading.Thread(target=self.api.serve_forever, daemon=True).start()
        self.control = None
        if self.control_enabled:
            self.control = LoopbackServer(("127.0.0.1", 0), self._control_handler())
            threading.Thread(target=self.control.serve_forever, daemon=True).start()
        return self.info()

    def info(self):
        out = {"api_url": f"http://127.0.0.1:{self.api.server_address[1]}", "engine": self.engine,
               "allow_origins": self.origins, "tables": list(self.tables)}
        if self.control:
            out.update({"control_url": f"http://127.0.0.1:{self.control.server_address[1]}",
                        "control_token": self.control_token})
        return out

    def _clock(self):
        return self.clock_value or datetime.datetime.now(datetime.timezone.utc)

    def _new_token(self):
        with self.lock:
            if self.tokens is None:
                return secrets.token_urlsafe(16)
            if not self.tokens:
                raise RuntimeError("the harness ran out of the version tokens this test fixed")
            return self.tokens.pop(0)

    def reset(self, now=None, new_tokens=None, rows=None):
        """Fresh, empty test- tables (the old ones are dropped), then the given rows."""
        old = self.tables
        self.tables = self.ddb_local.create_tables(self.client)
        if any(t in PRODUCTION_TABLES or not t.startswith("test-") for t in self.tables):
            raise Refused("refusing a non-test table name")
        if old:
            self.ddb_local.drop_tables(self.client, *old)
        self.clock_value = (datetime.datetime.fromisoformat(now.replace("Z", "+00:00")) if now else None)
        self.tokens = list(new_tokens) if new_tokens is not None else None
        with self.lock:
            self.faults, self.requests = [], {}
        store = self.store_mod.Store(self.client, *self.tables)
        self.index.configure(self.service_mod.Service(store, self.config_mod.load({}), clock=self._clock,
                                                      new_token=self._new_token))
        for email, items in (rows or {}).items():
            if not re.fullmatch(r"[a-z0-9._%+-]+@example\.(com|org|net)", email):
                raise Refused("seed rows may only belong to @example.com/.org/.net users")
            for item in items:
                self.client.put_item(TableName=self.tables[0], Item=self.store_mod.to_item({**item, "email": email}))

    def state(self):
        subs, quota = self.tables
        rows, quotas = {}, {}
        for item in self.client.scan(TableName=subs, ConsistentRead=True).get("Items", []):
            row = _plain(self.store_mod.from_item(item))
            rows.setdefault(row["email"], []).append(row)
        for item in self.client.scan(TableName=quota, ConsistentRead=True).get("Items", []):
            q = _plain(self.store_mod.from_item(item))
            quotas[q["email"]] = q
        for v in rows.values():
            v.sort(key=lambda r: r["route"])
        with self.lock:
            counts = dict(self.requests)
        return {"rows": rows, "quota": quotas, "requests": counts}

    def queue_fault(self, status, count=1):
        if status not in FAULTS or not isinstance(count, int) or not 1 <= count <= 100:
            raise ValueError("fault must be 429, 502, 503 or 504, count 1-100")
        with self.lock:
            self.faults.extend([status] * count)

    def _take_fault(self):
        with self.lock:
            return self.faults.pop(0) if self.faults else None

    def _count(self, key):
        with self.lock:
            self.requests[key] = self.requests.get(key, 0) + 1

    # -- HTTP --------------------------------------------------------------------------------
    def _api_handler(self):
        harness = self

        class Api(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _reply(self, status, body=None, headers=None):
                data = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("apigw-requestid", uuid.uuid4().hex[:16])
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data and self.command != "HEAD":
                    self.wfile.write(data)

            def _handle(self):
                origin = self.headers.get("Origin")
                raw_path, _, raw_query = self.path.partition("?")
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_REQUEST_BYTES:
                    self.close_connection = True
                    cors = cors_headers(harness.cors, harness.origins, origin)
                    return self._reply(413, {"message": "Request Entity Too Large"},
                                       {"Content-Type": "application/json", **cors})
                body = self.rfile.read(length) if length else b""
                if self.command == "OPTIONS":
                    harness._count("OPTIONS")
                    pre = (self.headers.get("Access-Control-Request-Method"),
                           self.headers.get("Access-Control-Request-Headers"))
                    return self._reply(204, None, cors_headers(harness.cors, harness.origins, origin,
                                                               preflight=pre) if pre[0] else {})
                cors = cors_headers(harness.cors, harness.origins, origin)
                route = match_route(self.command, raw_path)
                harness._count(route[0] if route else "unmatched")
                fault = harness._take_fault()
                if fault:  # the gateway answers; the request never reaches the function
                    return self._reply(fault, FAULTS[fault], {"Content-Type": "application/json", **cors})
                if route is None:
                    return self._reply(404, {"message": "Not Found"}, {"Content-Type": "application/json", **cors})
                event = to_event(self.command, raw_path, raw_query, self.headers.items(), body, *route)
                try:
                    resp = harness.index.handler(event, None)
                    status = int(resp["statusCode"])
                    out = resp.get("body")
                    if out is not None and resp.get("isBase64Encoded"):
                        out = base64.b64decode(out)
                    elif out is not None:
                        out = out.encode("utf-8")
                    headers = {k: v for k, v in (resp.get("headers") or {}).items()
                               if not k.lower().startswith("access-control-")}
                except Exception:  # the gateway's answer when the integration fails
                    status, out, headers = 500, json.dumps({"message": "Internal Server Error"}).encode(), \
                        {"Content-Type": "application/json"}
                return self._reply(status, out if status != 204 else None, {**headers, **cors})

            do_GET = do_POST = do_PATCH = do_DELETE = do_PUT = do_OPTIONS = do_HEAD = _handle

        return Api

    def _control_handler(self):
        harness = self

        class Control(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorised(self):
                given = self.headers.get("X-Harness-Control") or ""
                return hmac.compare_digest(given.encode(), harness.control_token.encode())

            def do_GET(self):
                if not self._authorised():
                    return self._reply(403, {"error": "forbidden"})
                if self.path == "/state":
                    return self._reply(200, harness.state())
                return self._reply(404, {"error": "not_found"})

            def do_POST(self):
                if not self._authorised():
                    return self._reply(403, {"error": "forbidden"})
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                    if self.path == "/reset":
                        harness.reset(body.get("now"), body.get("new_tokens"), body.get("rows"))
                        return self._reply(200, {"tables": list(harness.tables)})
                    if self.path == "/fault":
                        harness.queue_fault(body.get("status"), body.get("count", 1))
                        return self._reply(200, {"queued": True})
                except (ValueError, Refused) as e:
                    return self._reply(400, {"error": str(e)})
                return self._reply(404, {"error": "not_found"})

        return Control


def main(argv=None):
    parser = argparse.ArgumentParser(description="M1.4 localhost-only HTTP harness (dev/test only).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--control", action="store_true", help="enable the secret-protected control port")
    serve.add_argument("--allow-origin", action="append", help="CORS origin to allow (repeatable)")
    args = parser.parse_args(argv)
    try:
        harness = Harness(args.allow_origin, args.control)
        info = harness.start()
    except Refused as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps(info), flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
