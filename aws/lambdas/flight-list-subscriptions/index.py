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
from boto3.dynamodb.conditions import Key
T = boto3.resource("dynamodb").Table("subscriptions")
def handler(e, c):
    email = user_email(e)
    if not email:
        return resp(401, {"error": "unauthorized"})
    items = T.query(KeyConditionExpression=Key("email").eq(email)).get("Items", [])
    subs = [{"route": i["route"], "plan_name": i.get("plan_name"), "target_price": int(i["target_price"]),
             "currency": i.get("currency", "TWD"), "updated_at": i.get("updated_at")} for i in items]
    return resp(200, {"subscriptions": subs})
