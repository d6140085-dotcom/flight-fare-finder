"""DynamoDB access for the subscriptions API (low-level client, so transactions and their
cancellation reasons are explicit). All reads are strongly consistent.

Tables (names from the environment):
  subscriptions       PK email, SK route  — existing M1 table, unchanged schema. The SK value is
                      "ORIG-DEST" (legacy rolling) or "ORIG-DEST#YYYY-MM" (fixed month, R-MM-1);
                      see subkeys.py
  subscription_quota  PK email; count, version — new in M1.4 (contract §5.1)

Row identity (2026-11-13): every row written by M1.4 carries ``version_token``, a random opaque
token that changes on every mutation. Writes to an existing row are guarded by ``guard(row)``:
the stored item must still be exactly the row that was read (token, plus updated_at so a write
by the still-live M1 save Lambda, which never touches the token, is detected). M1 rows without a
token are guarded by every stored attribute (contract §4.5).
"""
import json
import re
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


TOKEN_ATTR = "version_token"
_M14_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


def token_current(row):
    """True when the row's version_token describes its current content: M1.4 wrote it last.
    Every M1.4 write sets a new token and a millisecond "...Z" updated_at; the deployed M1 save
    Lambda writes second-precision "+00:00" stamps and never touches the token, so a row it
    changed (or created) is treated as legacy until M1.4 writes it again."""
    return bool(row.get(TOKEN_ATTR)) and bool(_M14_STAMP.match(str(row.get("updated_at") or "")))


def _dyn(value):
    """A read value back in DynamoDB form (floats as exact Decimals)."""
    if isinstance(value, float):
        value = Decimal(repr(value))
    elif isinstance(value, dict):
        return {"M": {k: _dyn(v) for k, v in value.items()}}
    elif isinstance(value, list):
        return {"L": [_dyn(v) for v in value]}
    return _ser.serialize(value)


def guard(row):
    """(condition, names, values): the stored item at the row's key is still exactly ``row``.

    Token-current row: version_token and updated_at unchanged. Any other row (M1 row without a
    token, or a token row the M1 Lambda changed since): every attribute that was read is unchanged
    and, without a token, none has been added (contract §4.5). Names/values use a ``g`` prefix so
    they can be combined with an update's own placeholders."""
    names, values = {"#gtok": TOKEN_ATTR, "#gupd": "updated_at"}, {}
    parts = ["attribute_exists(email)"]
    if token_current(row):
        parts.append("#gtok = :gtok")
        values[":gtok"] = _ser.serialize(row[TOKEN_ATTR])
        if row.get("updated_at") is None:
            parts.append("attribute_not_exists(#gupd)")
        else:
            parts.append("#gupd = :gupd")
            values[":gupd"] = _ser.serialize(row["updated_at"])
        return " AND ".join(parts), names, values
    del names["#gupd"]
    if not row.get(TOKEN_ATTR):
        parts.append("attribute_not_exists(#gtok)")
    else:
        del names["#gtok"]  # a token row last written by M1: the token is compared below like any attribute
    for i, (k, v) in enumerate(sorted(row.items())):
        if k in ("email", "route") or v is None:
            continue
        names[f"#ga{i}"] = k
        values[f":ga{i}"] = _dyn(v)
        parts.append(f"#ga{i} = :ga{i}")
    return " AND ".join(parts), names, values


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

    def _guarded_delete(self, email, row):
        cond, names, values = guard(row)
        op = {"TableName": self.subs, "Key": to_item({"email": email, "route": row["route"]}),
              "ConditionExpression": cond, "ExpressionAttributeNames": names}
        if values:
            op["ExpressionAttributeValues"] = values
        return {"Delete": op}

    def delete(self, email, row, quota, new_count, now):
        """Quota-checked delete of exactly ``row`` as read (guard) — never whatever happens to be
        at the key."""
        self._transact([self._guarded_delete(email, row), self._quota_update(email, quota, new_count, now)])

    def change_route(self, email, old_row, new_row):
        """Delete old (exactly as read) + Put new in one transaction; the record count is
        unchanged (§6). Used for every sort-key change: route changes and, since R-MM-1, month
        changes."""
        self._transact([
            self._guarded_delete(email, old_row),
            {"Put": {"TableName": self.subs, "Item": to_item({**new_row, "email": email}),
                     "ConditionExpression": "attribute_not_exists(email)"}},
        ])

    def update(self, email, row, set_fields, remove_fields):
        """UpdateItem on ``row``'s key, only if the stored item is still exactly ``row`` (guard)."""
        route = row["route"]
        cond, names, values = guard(row)
        sets = []
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
