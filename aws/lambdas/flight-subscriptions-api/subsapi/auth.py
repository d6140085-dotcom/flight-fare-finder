"""Caller identity from the Supabase session only (contract §2). Any email in the request body
or query is ignored."""
import json
import urllib.request


def _norm(email):
    return email.strip().lower() if isinstance(email, str) and email.strip() else None


def resolve_email(event, env, opener=urllib.request.urlopen):
    if "requestContext" not in event:
        # Direct IAM-authorized invocation (never reachable through API Gateway). Disabled
        # unless the function is explicitly configured for testing.
        if env.get("ALLOW_DIRECT_TEST_INVOKE") == "true":
            return _norm(event.get("test_email"))
        return None
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization") or ""
    if not auth.lower().startswith("bearer ") or not auth[7:].strip():
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
