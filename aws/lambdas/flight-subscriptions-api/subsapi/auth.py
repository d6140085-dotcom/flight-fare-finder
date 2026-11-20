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

How Supabase's answer is classified (Phase 3B-E). The caller is never identified unless
Supabase answered 200 with a usable email, so every other path fails closed:
  * ``None`` -> 401 ``unauthorized``: the session is definitely not valid for this API. No
    bearer token (no request is made), Supabase 400/401/403, or a 200 without an email.
  * ``AuthUnavailable`` -> 503 ``auth_unavailable``: Supabase could not say who the caller is
    right now. Supabase 408/429/5xx, a timeout, a DNS, TLS or connection failure (refused,
    reset, closed early), or a 200 whose body cannot be decoded as a JSON object.
  * Any other status (404, 405, 409, 422, a non-200 2xx, ...) is unexpected. It is treated as
    unavailable too: it does not show that the session is invalid, so the user is not told to
    sign in again, and no data is read. Its status code is logged so it can be reclassified.
  * Missing or unsafe configuration raises ``RuntimeError`` (500), as before.
There is deliberately no catch-all: an error that is none of the above is a programming error
and propagates (500).
"""
import http.client
import json
import re
import socket
import ssl
import urllib.error
import urllib.request

PRODUCTION_FUNCTION = "flight-subscriptions-api"
TEST_DOMAINS = ("example.com", "example.org", "example.net")
TEST_TLDS = (".test", ".example", ".invalid")

INVALID_SESSION_STATUSES = frozenset({400, 401, 403})


class AuthUnavailable(Exception):
    """Supabase could not confirm who the caller is right now; the API answers 503.

    ``reason`` is a short fixed label for logs (``http_503``, ``timeout``, ``dns``, ``tls``,
    ``connection``, ``invalid_response``). It never contains the token, an email or any text
    from Supabase's response."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _status_outcome(status):
    """None for a definitely invalid session; otherwise AuthUnavailable (temporary or unexpected)."""
    if status in INVALID_SESSION_STATUSES:
        return None
    if status in (408, 429) or 500 <= status <= 599:
        raise AuthUnavailable(f"http_{status}")
    raise AuthUnavailable(f"unexpected_http_{status}" if isinstance(status, int) else "unexpected_http")


def _transport_reason(exc):
    """The label for a failure to get any HTTP answer from Supabase."""
    if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
        exc = exc.reason if isinstance(exc.reason, BaseException) else exc
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, socket.gaierror):
        return "dns"
    if isinstance(exc, (ssl.SSLError, ssl.CertificateError)):
        return "tls"
    return "connection"


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
            status = getattr(r, "status", 200)
            if status != 200:
                return _status_outcome(status)
            raw = r.read()
    except urllib.error.HTTPError as e:  # urlopen raises this for every 4xx/5xx answer
        status = e.code
        e.close()  # its body (Supabase's error text) is never read, logged or returned
        return _status_outcome(status)
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        # No usable HTTP answer: DNS, TLS, refused, reset, timeout, or a connection closed early.
        raise AuthUnavailable(_transport_reason(e)) from None
    try:
        user = json.loads(raw)
    except ValueError:  # includes undecodable bytes
        raise AuthUnavailable("invalid_response") from None
    if not isinstance(user, dict):
        raise AuthUnavailable("invalid_response")
    return _norm(user.get("email"))
