"""Every side effect of the fare check goes through these classes, so the safety rules are
enforced in one place:

- AWS clients come only from `guarded_factory`, which allows an explicit set of services.
  Dry run allows `ssm` (the Travelpayouts token) and nothing else: no Lambda, SQS or SES client
  can be created, so no parser, queue, notification Lambda or email can be reached.
- `subscriptions` is only ever seen through `ReadOnlyTable`, which exposes `scan` and refuses
  every write method, in both modes. The fare check never writes DynamoDB.
- `LambdaInvoker` (the live fan-out) refuses to exist in dry run.
"""
import hashlib
import json


class DryRunViolation(RuntimeError):
    """A live side effect was attempted while in dry run."""


class ReadOnlyViolation(RuntimeError):
    """A DynamoDB write was attempted; the fare check is read-only."""


def subscriber_tag(email):
    return "sub:" + hashlib.sha256(email.encode()).hexdigest()[:8]


def guarded_factory(factory, allowed):
    """Wrap boto3.client so only the named services can be created."""
    allowed = frozenset(allowed)

    def create(service, *args, **kwargs):
        if service not in allowed:
            raise DryRunViolation(f"AWS client {service!r} is not allowed here (allowed: {sorted(allowed)})")
        return factory(service, *args, **kwargs)
    return create


class ReadOnlyTable:
    """Proxy for a DynamoDB Table resource that only allows scan."""
    def __init__(self, table):
        self._table = table

    def scan(self, **kwargs):
        return self._table.scan(**kwargs)

    def __getattr__(self, name):
        raise ReadOnlyViolation(f"subscriptions.{name} is not allowed: the fare check is read-only")


class SqsPublisher:
    def __init__(self, queue_url, client_factory, dry_run):
        if dry_run:
            raise DryRunViolation("SQS publisher requested in dry run")
        if not queue_url:
            raise ValueError("FARE_QUEUE_URL is not set")
        self.queue_url, self._client, self._factory = queue_url, None, client_factory

    def send(self, body):
        if self._client is None:
            self._client = self._factory("sqs")
        self._client.send_message(QueueUrl=self.queue_url, MessageBody=json.dumps(body))


class DryRunPublisher:
    """Keeps would-be messages in memory and logs them without addresses."""
    def __init__(self, log=print):
        self.messages, self.log = [], log

    def send(self, body):
        self.messages.append(body)
        self.log(f"DRY_RUN would enqueue {body['route']} {subscriber_tag(body['email'])} "
                 f"target {body['target_price']} fare {body['cheapest']['price']}")


class LambdaInvoker:
    """Live fan-out to the parser. Cannot be constructed in dry run."""
    def __init__(self, function_name, client_factory, dry_run):
        if dry_run:
            raise DryRunViolation("parser fan-out requested in dry run")
        self.function_name, self._factory, self._client = function_name, client_factory, None

    def invoke(self, payload):
        if payload.get("dry_run"):
            raise DryRunViolation("dry-run payload on the live fan-out")
        if self._client is None:
            self._client = self._factory("lambda")
        self._client.invoke(FunctionName=self.function_name, InvocationType="Event",
                            Payload=json.dumps(payload).encode())


class RecordingInvoker:
    """Offline: collects payloads instead of invoking anything."""
    def __init__(self):
        self.payloads = []

    def invoke(self, payload):
        self.payloads.append(payload)


class TableReader:
    """Read-only scans of `subscriptions`. The wrapper's scan never includes `email`."""
    WRAPPER_FIELDS = ("route", "origin", "destination", "departure_month", "alerts_paused", "target_price")

    def __init__(self, table):
        self.table = table if isinstance(table, ReadOnlyTable) else ReadOnlyTable(table)

    def _scan(self, **kw):
        while True:
            page = self.table.scan(**kw)
            yield from page.get("Items", [])
            if "LastEvaluatedKey" not in page:
                return
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def rows_without_email(self):
        names = {f"#f{i}": f for i, f in enumerate(self.WRAPPER_FIELDS)}
        return self._scan(ProjectionExpression=", ".join(names), ExpressionAttributeNames=names)

    def rows_for_route(self, route):
        return self._scan(FilterExpression="#r = :r", ExpressionAttributeNames={"#r": "route"},
                          ExpressionAttributeValues={":r": route})


class ListReader:
    """Offline fixture reader with the same interface."""
    def __init__(self, rows):
        self.rows = list(rows)

    def rows_without_email(self):
        return ({k: v for k, v in r.items() if k in TableReader.WRAPPER_FIELDS} for r in self.rows)

    def rows_for_route(self, route):
        return (r for r in self.rows if r.get("route") == route)


def get_token(name, client_factory):
    """SSM value: a bare token or {"token": ...}. Never logged or returned to callers' output."""
    raw = client_factory("ssm").get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"].strip()
    try:
        v = json.loads(raw)
    except ValueError:
        v = raw
    token = v.get("token") if isinstance(v, dict) else str(v)
    if not token:
        raise RuntimeError("Travelpayouts parameter has no token")
    return token
