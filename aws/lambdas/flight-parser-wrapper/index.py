"""flight-parser-wrapper: EventBridge target. Reads flight-routes.json from S3 and fans out
one async flight-parser invocation per route.

Env: CONFIG_BUCKET, ROUTES_KEY (default flight-routes.json), PARSER_FUNCTION (default flight-parser)
"""
import json
import os

import boto3

_s3 = boto3.client("s3")
_lambda = boto3.client("lambda")


def handler(event, context):
    bucket = os.environ["CONFIG_BUCKET"]
    key = os.environ.get("ROUTES_KEY", "flight-routes.json")
    parser = os.environ.get("PARSER_FUNCTION", "flight-parser")

    routes = json.loads(_s3.get_object(Bucket=bucket, Key=key)["Body"].read())
    invoked = []
    for r in routes:
        payload = {
            "origin": r["origin"],
            "destination": r["destination"],
            "route": f"{r['origin']}-{r['destination']}",
            "plan": r.get("plan"),
        }
        _lambda.invoke(FunctionName=parser, InvocationType="Event", Payload=json.dumps(payload).encode())
        invoked.append(payload["route"])

    print(f"fanned out {len(invoked)} route(s): {', '.join(invoked)}")
    return {"ok": True, "routes": invoked}
