"""notification_history access for v2. Every write is conditional, so concurrent workers and
repeated deliveries can't both send for one stream.

Protocol per stream (state item = (pk, "STATE")):
  1. read     strongly consistent GetItem of the state item
  2. claim    UpdateItem  SET claim_event, claim_expires, version+1
              IF (no state item) OR (version = read version AND (no claim OR claim expired))
  3. send     (outside DynamoDB; Resend Idempotency-Key = the event id)
  4. commit   TransactWriteItems:
                Update state  SET last_*, version+1 REMOVE claim  IF claim_event = this event
                Put log item  (pk, sent_at=<now>)                 IF attribute_not_exists(pk)
                [Put M1 row   (email#route, sent_at=<now>)        IF attribute_not_exists(pk)]
     release  UpdateItem REMOVE claim IF claim_event = this event   (on any non-send outcome)
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from botocore.exceptions import ClientError

from . import keys

TABLE = "notification_history"


class ClaimConflict(Exception):
    """Another worker holds the stream or changed it since we read it."""


def _code(e):
    return e.response.get("Error", {}).get("Code") if isinstance(e, ClientError) else None


def iso_ms(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


class History:
    def __init__(self, table, client, table_name=TABLE):
        """`table`: boto3 Table resource; `client`: boto3 DynamoDB client (TransactWriteItems)."""
        self.table, self.client, self.name = table, client, table_name

    # ---- reads -------------------------------------------------------------------------------
    def state(self, pk):
        return self.table.get_item(Key={"pk": pk, "sent_at": keys.STATE_SK}, ConsistentRead=True).get("Item")

    def legacy_last(self, email, route, since):
        """Newest genuine M1 row for (email, route) sent at or after `since`, as
        {"sent_at", "price" or None}, or None.

        M1 rows (pk "{email}#{route}") hold only sent_at, email, route and price: no departure
        month, no fare dates. Copies that v2 dual-writes for rollback safety carry source="v2" and
        are skipped (v2's own state covers them). Rows whose sent_at isn't a valid ISO timestamp
        can't be produced by M1 and are skipped. Rows older than the M1 floor can't suppress
        anything under the M1 rule, so only that window is read."""
        kw = {"KeyConditionExpression": "pk = :p AND sent_at >= :s", "ScanIndexForward": False, "ConsistentRead": True,
              "ExpressionAttributeValues": {":p": keys.legacy_pk(email, route), ":s": iso_ms(since)}}
        while True:
            r = self.table.query(**kw)
            for item in r.get("Items") or []:
                if item.get("source") == "v2":
                    continue
                try:
                    sent = datetime.fromisoformat(str(item.get("sent_at")))
                except ValueError:
                    continue
                if sent.tzinfo is None or sent < since:
                    continue
                try:
                    price = Decimal(str(item["price"]))
                    price = price if price.is_finite() and price > 0 else None
                except (KeyError, ArithmeticError, ValueError):
                    price = None
                return {"sent_at": sent.isoformat(), "price": price}
            if "LastEvaluatedKey" not in r:
                return None
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]

    # ---- claim / release ---------------------------------------------------------------------
    def claim(self, pk, state, eid, now, claim_seconds):
        values = {":e": eid, ":x": int((now + timedelta(seconds=claim_seconds)).timestamp()), ":one": 1, ":zero": 0}
        if state is None:
            cond = "attribute_not_exists(pk)"
        else:
            # a claim may be taken if free, expired, or already ours (the redelivery of the same
            # fare event after a crash re-enters its own claim)
            cond = "version = :v AND (attribute_not_exists(claim_event) OR claim_expires < :now OR claim_event = :e)"
            values.update({":v": state["version"], ":now": int(now.timestamp())})
        try:
            self.table.update_item(
                Key={"pk": pk, "sent_at": keys.STATE_SK},
                UpdateExpression="SET claim_event = :e, claim_expires = :x, version = if_not_exists(version, :zero) + :one",
                ConditionExpression=cond, ExpressionAttributeValues=values)
        except ClientError as e:
            if _code(e) == "ConditionalCheckFailedException":
                raise ClaimConflict(pk) from None
            raise
        return (state or {}).get("version", 0) + 1

    def release(self, pk, eid):
        try:
            self.table.update_item(Key={"pk": pk, "sent_at": keys.STATE_SK}, UpdateExpression="REMOVE claim_event, claim_expires",
                                   ConditionExpression="claim_event = :e", ExpressionAttributeValues={":e": eid})
        except ClientError as e:
            if _code(e) != "ConditionalCheckFailedException":
                raise

    # ---- commit ------------------------------------------------------------------------------
    def commit(self, *, pk, eid, now, price, route, month, sid, email=None, dual_write=False):
        """Record a send. Returns "committed", or "claim_lost" when the claim expired and another
        worker took the stream (the send already happened; history is still written)."""
        t = iso_ms(now)
        state_update = {"Update": {
            "TableName": self.name, "Key": {"pk": {"S": pk}, "sent_at": {"S": keys.STATE_SK}},
            "UpdateExpression": "SET last_sent_at = :t, last_price = :p, last_event_id = :e, version = version + :one, "
                                "sends = if_not_exists(sends, :zero) + :one, route = :r, #m = :m, subscriber_id = :s "
                                "REMOVE claim_event, claim_expires",
            "ConditionExpression": "claim_event = :e",
            "ExpressionAttributeNames": {"#m": "month"},
            "ExpressionAttributeValues": {":t": {"S": t}, ":p": {"N": str(price)}, ":e": {"S": eid}, ":one": {"N": "1"},
                                          ":zero": {"N": "0"}, ":r": {"S": route}, ":m": {"S": month}, ":s": {"S": sid}}}}
        puts = [{"Put": {"TableName": self.name, "ConditionExpression": "attribute_not_exists(pk)",
                         "Item": {"pk": {"S": pk}, "sent_at": {"S": t}, "route": {"S": route}, "month": {"S": month},
                                  "price": {"N": str(price)}, "event_id": {"S": eid}, "subscriber_id": {"S": sid}}}}]
        if dual_write and email:
            # The M1 row shape (pk, sent_at, email, route, price), so rolling back to the M1 Lambda
            # doesn't resend within 24 h; M1 reads only sent_at and price. source/month mark it as a
            # v2 copy. Unconditional: two months of one route may send in the same millisecond.
            puts.append({"Put": {"TableName": self.name,
                                 "Item": {"pk": {"S": keys.legacy_pk(email, route)}, "sent_at": {"S": t},
                                          "email": {"S": email}, "route": {"S": route}, "price": {"N": str(price)},
                                          "source": {"S": "v2"}, "month": {"S": month}}}})
        try:
            self.client.transact_write_items(TransactItems=[state_update, *puts])
            return "committed"
        except ClientError as e:
            if _code(e) != "TransactionCanceledException":
                raise
        # The claim was lost (expired and taken). The email went out, so record it anyway:
        # monotonic state update (never moves last_sent_at backwards) plus the log rows.
        try:
            self.table.update_item(
                Key={"pk": pk, "sent_at": keys.STATE_SK},
                UpdateExpression="SET last_sent_at = :t, last_price = :p, last_event_id = :e, "
                                 "version = if_not_exists(version, :zero) + :one",
                ConditionExpression="attribute_not_exists(last_sent_at) OR last_sent_at < :t",
                ExpressionAttributeValues={":t": t, ":p": price, ":e": eid, ":one": 1, ":zero": 0})
        except ClientError as e:
            if _code(e) != "ConditionalCheckFailedException":
                raise
        for p in puts:
            item = {k: (v.get("S") if "S" in v else v["N"]) for k, v in p["Put"]["Item"].items()}
            if "price" in item:
                item["price"] = Decimal(item["price"])
            extra = {"ConditionExpression": p["Put"]["ConditionExpression"]} if "ConditionExpression" in p["Put"] else {}
            try:
                self.table.put_item(Item=item, **extra)
            except ClientError as e:
                if _code(e) != "ConditionalCheckFailedException":
                    raise
        return "claim_lost"
