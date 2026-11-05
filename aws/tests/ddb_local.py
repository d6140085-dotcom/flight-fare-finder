"""Local DynamoDB endpoint for the M1.4 API tests. Never talks to AWS.

* ``DDB_ENDPOINT=http://127.0.0.1:8000`` → use that endpoint (DynamoDB Local; see
  aws/scripts/test_dynamodb_local.sh). Only localhost endpoints are accepted.
* otherwise → start moto's DynamoDB-compatible server in-process on 127.0.0.1, **single-threaded**
  so that each request (including each TransactWriteItems) is applied atomically, as DynamoDB
  does; concurrent clients still interleave between requests, which is what the quota protocol
  must survive.
* neither available → ``None`` (the integration tests skip).

Credentials are fixed fake values, so even a mistaken endpoint could not authenticate to AWS.
"""
import os
import sys
import threading
import uuid
from urllib.parse import urlparse

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
FAKE_ENV = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
            "AWS_DEFAULT_REGION": "ap-southeast-2", "AWS_EC2_METADATA_DISABLED": "true"}

# Earlier test modules replace boto3 with stubs in sys.modules; drop them before importing.
if "boto3" in sys.modules and not getattr(sys.modules["boto3"], "__file__", None):
    for name in [m for m in sys.modules if m == "boto3" or m.startswith("boto3.")]:
        del sys.modules[name]

try:
    import boto3
    from botocore.config import Config
except ImportError:  # the stdlib-only test run
    boto3 = None

_server = None
_endpoint = None


def endpoint():
    global _server, _endpoint
    if _endpoint:
        return _endpoint
    url = os.environ.get("DDB_ENDPOINT")
    if not url:
        try:
            from moto.moto_server.werkzeug_app import DomainDispatcherApplication, create_backend_app
            from werkzeug.serving import make_server
        except ImportError:
            return None
        from werkzeug.serving import WSGIRequestHandler

        class Quiet(WSGIRequestHandler):
            def log_request(self, *a, **kw):
                pass

        _server = make_server("127.0.0.1", 0, DomainDispatcherApplication(create_backend_app), threaded=False,
                              request_handler=Quiet)
        threading.Thread(target=_server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{_server.server_port}"
    if urlparse(url).hostname not in LOCAL_HOSTS:
        raise RuntimeError(f"refusing non-local DynamoDB endpoint {url!r}")
    _endpoint = url
    return url


def available():
    return boto3 is not None and endpoint() is not None


def client():
    os.environ.update(FAKE_ENV)
    session = boto3.session.Session(aws_access_key_id="testing", aws_secret_access_key="testing",
                                    aws_session_token="testing", region_name="ap-southeast-2")
    c = session.client("dynamodb", endpoint_url=endpoint(),
                       config=Config(max_pool_connections=128, retries={"mode": "standard", "max_attempts": 8}))
    assert urlparse(c.meta.endpoint_url).hostname in LOCAL_HOSTS
    return c


def create_tables(c):
    """Fresh, uniquely named tables for one test: (subscriptions, subscription_quota)."""
    suffix = uuid.uuid4().hex[:8]
    subs, quota = f"test-subscriptions-{suffix}", f"test-subscription-quota-{suffix}"
    c.create_table(TableName=subs, BillingMode="PAY_PER_REQUEST",
                   AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"},
                                         {"AttributeName": "route", "AttributeType": "S"}],
                   KeySchema=[{"AttributeName": "email", "KeyType": "HASH"},
                              {"AttributeName": "route", "KeyType": "RANGE"}])
    c.create_table(TableName=quota, BillingMode="PAY_PER_REQUEST",
                   AttributeDefinitions=[{"AttributeName": "email", "AttributeType": "S"}],
                   KeySchema=[{"AttributeName": "email", "KeyType": "HASH"}])
    return subs, quota


def drop_tables(c, *names):
    for n in names:
        c.delete_table(TableName=n)
