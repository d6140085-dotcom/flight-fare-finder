"""DynamoDB access for the subscriptions API (low-level client, so transactions and their
cancellation reasons are explicit). All reads are strongly consistent.

Tables (names from the environment):
  subscriptions       PK email, SK route  — existing M1 table, unchanged schema. The SK value is
                      "ORIG-DEST" (legacy rolling) or "ORIG-DEST#YYYY-MM" (fixed month, R-MM-1);
                      see subkeys.py
  subscription_quota  PK email; count, version — new in M1.4 (contract §5.1)
"""
import json
import uuid
from decimal import Decimal

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

_ser, _de = TypeSerializer(), TypeDeserializer()


class TxCancelled(Exception):
    """TransactWriteItems was cancelled; ``reasons`` holds one code per item, in item order
    ("None", "ConditionalCheckFailed", "TransactionConflict", ...)."""

    def __init__(self, reasons):
        super().__init__(",".join(reasons))
        self.reasons = reasons

    def failed(self, index):
        return index < len(self.reasons) and self.reasons[index] == "ConditionalCheckFailed"

    @property
    def conflict(self):
        return "TransactionConflict" in self.reasons


class ConditionFailed(Exception):
    """A single-item write's condition was false (row missing or changed). Nothing written."""


class WriteConflict(Exception):
    """DynamoDB refused a write because of a concurrent transaction; nothing was written by
    this request, and the caller decides whether a fresh read-and-retry is safe.

    ``code`` is DynamoDB's error code:
      * ``TransactionConflictException`` — a single-item write hit an item that an in-flight
        TransactWriteItems is changing (seen on real DynamoDB in B4 Stage C, scenario s10;
        the emulators never produce it). Safe to re-read and retry.
      * ``TransactionInProgressException`` — the SDK re-sent our own TransactWriteItems while the
        first attempt is still running; its outcome is unknown, so callers must not retry it.
    """

    def __init__(self, code):
        super().__init__(code)
        self.code = code

    @property
    def retryable(self):
        return self.code == "TransactionConflictException"


def _error_code(e):
    return e.response.get("Error", {}).get("Code")


def _plain(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def to_item(d):
    return {k: _ser.serialize(v) for k, v in d.items() if v is not None}


def from_item(item):
    return {k: _plain(_de.deserialize(v)) for k, v in item.items()}


def json_key(key):
    return tuple(sorted((k, json.dumps(v, sort_keys=True)) for k, v in key.items()))


def _vals(**kw):
    return {f":{k}": _ser.serialize(v) for k, v in kw.items()}


class Store:
    def __init__(self, client, subscriptions_table, quota_table):
        self.c = client
        self.subs = subscriptions_table
        self.quota = quota_table

    # ---- reads -------------------------------------------------------------------------
    def get_quota(self, email):
        r = self.c.get_item(TableName=self.quota, Key=to_item({"email": email}), ConsistentRead=True)
        return from_item(r["Item"]) if "Item" in r else None

    def list_rows(self, email):
        rows, kw = [], {}
        while True:
            r = self.c.query(
                TableName=self.subs,
                KeyConditionExpression="email = :e",
                ExpressionAttributeValues=_vals(e=email),
                ConsistentRead=True,
                **kw,
            )
            rows += [from_item(i) for i in r.get("Items", [])]
            if "LastEvaluatedKey" not in r:
                return rows
            kw = {"ExclusiveStartKey": r["LastEvaluatedKey"]}

    def get_row(self, email, route):
        r = self.c.get_item(TableName=self.subs, Key=to_item({"email": email, "route": route}), ConsistentRead=True)
        return from_item(r["Item"]) if "Item" in r else None

    # ---- writes ------------------------------------------------------------------------
    def _quota_update(self, email, quota, new_count, now):
        """SET count/version guarded by the version read before counting (contract §5.2)."""
        op = {
            "TableName": self.quota,
            "Key": to_item({"email": email}),
            "UpdateExpression": "SET #c = :c, #v = :nv, updated_at = :t",
            "ExpressionAttributeNames": {"#c": "count", "#v": "version"},
        }
        if quota is None:
            op["ConditionExpression"] = "attribute_not_exists(email)"
            op["ExpressionAttributeValues"] = _vals(c=new_count, nv=1, t=now)
        else:
            op["ConditionExpression"] = "#v = :v"
            op["ExpressionAttributeValues"] = _vals(c=new_count, nv=quota["version"] + 1, v=quota["version"], t=now)
        return {"Update": op}

    @staticmethod
    def _target(item):
        (op, body), = item.items()
        key = body["Key"] if "Key" in body else {k: body["Item"][k] for k in ("email", "route") if k in body["Item"]}
        return body["TableName"], json_key(key)

    def _transact(self, items):
        """One TransactWriteItems. Never two operations on the same item (DynamoDB rejects that
        with ValidationException; we refuse it before sending). The ClientRequestToken makes an
        SDK-level retry of the same call idempotent (a lost response can't turn our own commit
        into a 'duplicate'); each application-level attempt uses a fresh token."""
        targets = [self._target(i) for i in items]
        if len(set(targets)) != len(targets):
            raise ValueError("transaction targets the same item twice")
        try:
            self.c.transact_write_items(TransactItems=items, ClientRequestToken=str(uuid.uuid4()))
        except ClientError as e:
            code = _error_code(e)
            if code == "TransactionCanceledException":
                reasons = [r.get("Code", "None") for r in e.response.get("CancellationReasons", [])]
                raise TxCancelled(reasons) from None
            if code == "TransactionConflictException":  # defensive: normally a cancellation reason
                raise TxCancelled(["TransactionConflict"]) from None
            if code == "TransactionInProgressException":
                raise WriteConflict(code) from None
            raise  # anything else is unexpected: surfaces as a 500 with its code logged

    def create(self, email, row, quota, new_count, now):
        self._transact([
            {"Put": {"TableName": self.subs, "Item": to_item({**row, "email": email}),
                     "ConditionExpression": "attribute_not_exists(email)"}},
            self._quota_update(email, quota, new_count, now),
        ])

    def delete(self, email, route, quota, new_count, now, if_updated_at=None):
        cond, vals = "attribute_exists(email)", None
        if if_updated_at is not None:
            cond, vals = cond + " AND updated_at = :u", _vals(u=if_updated_at)
        op = {"TableName": self.subs, "Key": to_item({"email": email, "route": route}), "ConditionExpression": cond}
        if vals:
            op["ExpressionAttributeValues"] = vals
        self._transact([{"Delete": op}, self._quota_update(email, quota, new_count, now)])

    def change_route(self, email, old_route, expected_updated_at, new_row):
        """Delete old + Put new in one transaction; the record count is unchanged (§6). Used for
        every sort-key change: route changes and, since R-MM-1, month changes."""
        self._transact([
            {"Delete": {"TableName": self.subs, "Key": to_item({"email": email, "route": old_route}),
                        "ConditionExpression": "attribute_exists(email) AND updated_at = :u",
                        "ExpressionAttributeValues": _vals(u=expected_updated_at)}},
            {"Put": {"TableName": self.subs, "Item": to_item({**new_row, "email": email}),
                     "ConditionExpression": "attribute_not_exists(email)"}},
        ])

    def update(self, email, route, set_fields, remove_fields, expected_updated_at=None):
        """UpdateItem on an existing row; with ``expected_updated_at``, only if unchanged."""
        names, values, sets = {}, {}, []
        for i, (k, v) in enumerate(sorted(set_fields.items())):
            names[f"#s{i}"] = k
            values[f":s{i}"] = _ser.serialize(v)
            sets.append(f"#s{i} = :s{i}")
        removes = []
        for i, k in enumerate(sorted(remove_fields)):
            names[f"#r{i}"] = k
            removes.append(f"#r{i}")
        expr = "SET " + ", ".join(sets)
        if removes:
            expr += " REMOVE " + ", ".join(removes)
        cond = "attribute_exists(email)"
        if expected_updated_at is not None:
            cond += " AND updated_at = :expected"
            values[":expected"] = _ser.serialize(expected_updated_at)
        try:
            r = self.c.update_item(
                TableName=self.subs, Key=to_item({"email": email, "route": route}),
                UpdateExpression=expr, ConditionExpression=cond,
                ExpressionAttributeNames=names, ExpressionAttributeValues=values, ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            code = _error_code(e)
            if code == "ConditionalCheckFailedException":
                raise ConditionFailed() from None
            if code == "TransactionConflictException":
                raise WriteConflict(code) from None
            raise  # anything else is unexpected: surfaces as a 500 with its code logged
        return from_item(r["Attributes"])
