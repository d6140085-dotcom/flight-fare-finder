"""Every side effect of the fare check goes through these classes, so DRY_RUN is enforced in
one place: in dry-run mode no SQS client, Lambda client or DynamoDB write is ever created.

Reads: the wrapper scans `subscriptions` without `email`; the parser reads one route's rows.
Writes: only SQS `send_message` (live mode). The fare check never writes DynamoDB.
"""
import hashlib
import json


class DryRunViolation(RuntimeError):
    """A live side effect was attempted while DRY_RUN is on."""


def subscriber_tag(email):
    return "sub:" + hashlib.sha256(email.encode()).hexdigest()[:8]


class SqsPublisher:
    def __init__(self, queue_url, client_factory, dry_run):
        if dry_run:
            raise DryRunViolation("SQS publisher requested in DRY_RUN")
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
    def __init__(self, function_name, client_factory, dry_run_payloads):
        self.function_name, self._factory, self._client = function_name, client_factory, None
        self.dry_run_payloads = dry_run_payloads

    def invoke(self, payload):
        if self.dry_run_payloads and payload.get("dry_run") is not True:
            raise DryRunViolation("live parser payload in DRY_RUN")
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
    """Read-only scans of `subscriptions` (resource injected so tests never touch AWS)."""
    WRAPPER_FIELDS = ("route", "origin", "destination", "departure_month", "alerts_paused")

    def __init__(self, table):
        self.table = table

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
