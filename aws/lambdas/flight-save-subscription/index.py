import json, os, base64, urllib.request
def resp(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"}, "body": json.dumps(body)}
def user_email(e):
    # Direct IAM-authorized invokes (no API Gateway context) may pass test_email for testing.
    if "requestContext" not in e:
        return (e.get("test_email") or "").strip().lower() or None
    h = {k.lower(): v for k, v in (e.get("headers") or {}).items()}
    auth = h.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    req = urllib.request.Request(os.environ["SUPABASE_URL"] + "/auth/v1/user", headers={
        "apikey": os.environ["SUPABASE_PUBLISHABLE_KEY"], "Authorization": auth, "User-Agent": "flight-notifier/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            u = json.loads(r.read())
    except Exception:
        return None
    return (u.get("email") or "").strip().lower() or None
def body_of(e):
    if "requestContext" not in e:
        return e
    b = e.get("body") or "{}"
    if e.get("isBase64Encoded"):
        b = base64.b64decode(b).decode()
    try:
        return json.loads(b)
    except Exception:
        return None

import boto3
from datetime import datetime, timezone
from decimal import Decimal
PLANS = {"tokyo": ("TPE", "TYO"), "seoul": ("TPE", "SEL")}
T = boto3.resource("dynamodb").Table("subscriptions")
def handler(e, c):
    email = user_email(e)
    if not email:
        return resp(401, {"error": "unauthorized"})
    b = body_of(e)
    if not isinstance(b, dict):
        return resp(400, {"error": "invalid_json"})
    plan = b.get("plan_name")
    if plan not in PLANS:
        return resp(400, {"error": "invalid_plan"})
    tp = b.get("target_price")
    if isinstance(tp, bool) or not isinstance(tp, (int, float)) or tp != int(tp) or not 1 <= tp <= 1000000:
        return resp(400, {"error": "invalid_target_price"})
    tp = int(tp)
    o, d = PLANS[plan]
    route = o + "-" + d
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    T.update_item(Key={"email": email, "route": route},
        UpdateExpression="SET plan_name=:p, origin=:o, destination=:d, target_price=:t, currency=:c, updated_at=:n, created_at=if_not_exists(created_at,:n)",
        ExpressionAttributeValues={":p": plan, ":o": o, ":d": d, ":t": Decimal(tp), ":c": "TWD", ":n": now})
    return resp(200, {"route": route, "plan_name": plan, "target_price": tp, "currency": "TWD", "updated_at": now})
