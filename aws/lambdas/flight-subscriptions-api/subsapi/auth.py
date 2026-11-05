"""Caller identity from the Supabase session only (contract §2). Any email in the request body
or query is ignored.

Direct test invocation (no API Gateway) is a narrow, fail-closed exception for smoke tests on a
*test* function. All of these must hold, otherwise the caller is anonymous (401):
  1. the event has no ``requestContext`` key at all — API Gateway always sends one, so no HTTP
     request can reach this path, whatever its headers or body contain;
  2. the environment sets ``ALLOW_DIRECT_TEST_INVOKE`` to exactly ``"true"``;
  3. the function is **not** the production function (``AWS_LAMBDA_FUNCTION_NAME`` differs from
     ``flight-subscriptions-api``; the Lambda runtime sets this variable itself);
  4. ``test_email`` is in a reserved test domain (RFC 2606/6761), so even a misconfigured test
     function can never act as a real user.
"""
import json
import re
import urllib.request

PRODUCTION_FUNCTION = "flight-subscriptions-api"
TEST_DOMAINS = ("example.com", "example.org", "example.net")
TEST_TLDS = (".test", ".example", ".invalid")


def _norm(email):
    return email.strip().lower() if isinstance(email, str) and email.strip() else None


_ADDRESS = re.compile(r"^[a-z0-9._%+-]+@([a-z0-9-]+\.)*[a-z0-9-]+$")


def is_test_address(email):
    if not isinstance(email, str) or not _ADDRESS.match(email):
        return False
    domain = email.rpartition("@")[2]
    return domain in TEST_DOMAINS or domain.endswith(TEST_TLDS)


def direct_test_identity(event, env):
    if "requestContext" in event:
        return None
    if env.get("ALLOW_DIRECT_TEST_INVOKE") != "true":
        return None
    if env.get("AWS_LAMBDA_FUNCTION_NAME") == PRODUCTION_FUNCTION:
        return None
    email = _norm(event.get("test_email"))
    return email if email and is_test_address(email) else None


def resolve_email(event, env, opener=urllib.request.urlopen):
    if not isinstance(event, dict):
        return None
    if "requestContext" not in event:
        return direct_test_identity(event, env)
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization") or ""
    if not isinstance(auth, str) or not auth.lower().startswith("bearer ") or not auth[7:].strip():
        return None
    url, key = env.get("SUPABASE_URL", ""), env.get("SUPABASE_PUBLISHABLE_KEY", "")
    if not url.startswith("https://") or not key.startswith("sb_publishable_"):
        raise RuntimeError("Supabase is not configured with an https URL and a publishable key")
    req = urllib.request.Request(
        url.rstrip("/") + "/auth/v1/user",
        headers={"apikey": key, "Authorization": auth, "User-Agent": "flight-notifier/1.0"},
    )
    try:
        with opener(req, timeout=5) as r:
            if getattr(r, "status", 200) != 200:
                return None
            user = json.loads(r.read())
    except Exception:
        return None
    return _norm(user.get("email")) if isinstance(user, dict) else None
