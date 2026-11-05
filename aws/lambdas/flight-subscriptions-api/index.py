"""flight-subscriptions-api — M1.4 subscriptions API (docs/api/m1.4-subscriptions-api.md).

API Gateway HTTP API (payload 2.0) routes handled:
  GET /subscriptions, POST /subscriptions, PATCH /subscriptions/{route},
  DELETE /subscriptions/{route}, and the legacy POST /subscribe (after cutover only).
Environment: SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, SUBSCRIPTIONS_TABLE, QUOTA_TABLE,
optional MAX_SUBSCRIPTIONS and ALLOW_DIRECT_TEST_INVOKE ("true" only on a test function).
Logs never contain email addresses, tokens or request bodies.
"""
import base64
import hashlib
import json
import os

from subsapi import config as config_mod
from subsapi.auth import resolve_email
from subsapi.errors import ApiError
from subsapi.messages import message

MAX_BODY_BYTES = 4096
_service = None


def configure(service):
    """Inject a Service (tests). In Lambda the first request builds one from the environment."""
    global _service
    _service = service


def _default_service():
    import boto3
    from botocore.config import Config as BotoConfig

    from subsapi.service import Service
    from subsapi.store import Store

    client = boto3.client("dynamodb", config=BotoConfig(retries={"mode": "standard", "max_attempts": 4}))
    return Service(
        Store(client, os.environ.get("SUBSCRIPTIONS_TABLE", "subscriptions"),
              os.environ.get("QUOTA_TABLE", "subscription_quota")),
        config_mod.load(),
    )


def _resp(status, body):
    out = {"statusCode": status, "headers": {"Content-Type": "application/json; charset=utf-8"}}
    if body is not None:
        out["body"] = json.dumps(body, ensure_ascii=False)
    return out


def _body(event):
    if "requestContext" not in event:
        return event.get("body", {})
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except Exception:
            raise ApiError(400, "invalid_json") from None
    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        raise ApiError(400, "invalid_json")
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        raise ApiError(400, "invalid_json") from None


def _log(event, user, status, code=None):
    who = hashlib.sha256(user.encode()).hexdigest()[:12] if user else "-"
    print(json.dumps({"route": event.get("routeKey"), "user": who, "status": status, "error": code}))


def handler(event, context):
    global _service
    svc = _service or _default_service()
    _service = svc
    email = None
    try:
        email = resolve_email(event, os.environ)
        if not email:
            raise ApiError(401, "unauthorized")
        key = event.get("routeKey", "")
        params = event.get("pathParameters") or {}
        query = event.get("queryStringParameters") or {}
        if key == "GET /subscriptions":
            status, body = svc.list(email)
        elif key == "POST /subscriptions":
            status, body = svc.create(email, _body(event))
        elif key == "PATCH /subscriptions/{route}":
            status, body = svc.patch(email, params.get("route"), _body(event))
        elif key == "DELETE /subscriptions/{route}":
            status, body = svc.delete(email, params.get("route"), query.get("if_updated_at"))
        elif key == "POST /subscribe":
            status, body = svc.legacy_subscribe(email, _body(event))
        else:
            raise ApiError(404, "not_found_route")
        _log(event, email, status)
        return _resp(status, body)
    except ApiError as e:
        _log(event, email, e.status, e.code)
        out = {"error": e.code, "message": message(e.code, svc.cfg, svc.clock(), e.details)}
        if e.details:
            out["details"] = e.details
        return _resp(e.status, out)
    except Exception as e:  # never leak internals; nothing is partially written (transactions)
        print(json.dumps({"route": event.get("routeKey"), "status": 500, "exception": type(e).__name__}))
        return _resp(500, {"error": "internal_error", "message": message("internal_error", svc.cfg, svc.clock())})
