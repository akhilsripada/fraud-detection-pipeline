"""Where decisions go. Every sink has emit(decision) and optional flush().

AWS sinks take boto3 clients/resources as constructor arguments so tests can
pass in fakes -- no network, no mocking library needed.
"""
from __future__ import annotations

import json
import logging
from collections import deque
from decimal import Decimal

from .pipeline import ALLOW, BLOCK, Decision

log = logging.getLogger(__name__)


def alert_payload(d: Decision) -> dict:
    """The compact message the dashboard and notifications receive."""
    return {
        "type": "alert", "txn_id": d.txn_id, "user_id": d.user_id, "ts": d.ts,
        "amount": d.amount, "city": d.city, "country": d.country, "lat": d.lat, "lon": d.lon,
        "merchant_category": d.merchant_category, "decision": d.decision,
        "risk_score": d.risk_score, "rules_fired": d.rules_fired, "label": d.label,
        "fraud_type": d.fraud_type,
    }


# ----------------------------------------------------------------------------- local

class InMemoryAlertSink:
    """Idempotent alert store (mirrors the DynamoDB conditional write)."""

    def __init__(self, maxlen: int = 500):
        self._seen: set[str] = set()
        self.recent: deque = deque(maxlen=maxlen)
        self.duplicates = 0

    def emit(self, d: Decision):
        if d.decision == ALLOW:
            return
        if d.txn_id in self._seen:
            self.duplicates += 1
            return
        self._seen.add(d.txn_id)
        self.recent.append(alert_payload(d))


class JsonlSink:
    """Append every decision to a local file (the local stand-in for S3/Firehose)."""

    def __init__(self, path: str):
        self.f = open(path, "a", buffering=1)

    def emit(self, d: Decision):
        self.f.write(json.dumps(d.to_dict(), separators=(",", ":")) + "\n")

    def flush(self):
        self.f.flush()


# ----------------------------------------------------------------------------- AWS

class DynamoAlertSink:
    """Writes flagged transactions to the alerts table exactly once.

    The conditional put on txn_id is the idempotency key: Kinesis delivers
    at-least-once, so a retried batch must not create duplicate alerts or
    re-page anyone. Downstream sinks only fire for *new* alerts.
    """

    def __init__(self, table, downstream: list | None = None, ttl_days: int = 30):
        self.table, self.downstream, self.ttl_days = table, downstream or [], ttl_days

    def emit(self, d: Decision):
        if d.decision == ALLOW:
            return
        # boto3's DynamoDB resource rejects Python floats; Decimal(str(x)) keeps exact values
        item = {k: (Decimal(str(v)) if isinstance(v, float) else v)
                for k, v in alert_payload(d).items() if v is not None}
        item["rules_fired"] = d.rules_fired
        item["expires_at"] = int(d.ts) + self.ttl_days * 86400
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(txn_id)")
        except Exception as e:
            if getattr(e, "response", {}).get("Error", {}).get("Code") == \
                    "ConditionalCheckFailedException":
                log.info("duplicate alert %s skipped", d.txn_id)
                return
            raise
        for sink in self.downstream:
            sink.emit(d)


class SnsBlockSink:
    """Pages a human (email/SMS) for BLOCK decisions only."""

    def __init__(self, sns_client, topic_arn: str):
        self.sns, self.topic_arn = sns_client, topic_arn

    def emit(self, d: Decision):
        if d.decision != BLOCK:
            return
        self.sns.publish(
            TopicArn=self.topic_arn,
            Subject=f"[FRAUD BLOCK] ${d.amount:,.2f} user {d.user_id}"[:100],
            Message=json.dumps(alert_payload(d), indent=2),
        )


class WebSocketBroadcastSink:
    """Pushes alerts to every connected dashboard via API Gateway WebSockets.

    Messages are buffered and sent in flush() once per Lambda batch; stale
    connections (GoneException) are removed from the connections table.
    """

    def __init__(self, apigw_client, connections_table):
        self.api, self.table = apigw_client, connections_table
        self.buffer: list[dict] = []

    def emit(self, d: Decision):
        self.buffer.append(alert_payload(d))

    def send(self, message: dict):
        self.buffer.append(message)

    def flush(self):
        if not self.buffer:
            return
        data = json.dumps({"type": "batch", "items": self.buffer}).encode()
        self.buffer = []
        conns = self.table.scan(ProjectionExpression="connection_id").get("Items", [])
        for c in conns:
            cid = c["connection_id"]
            try:
                self.api.post_to_connection(ConnectionId=cid, Data=data)
            except Exception as e:
                code = getattr(e, "response", {}).get("Error", {}).get("Code")
                if code in ("GoneException", "410"):
                    self.table.delete_item(Key={"connection_id": cid})
                else:
                    log.warning("websocket push to %s failed: %s", cid, e)


class FirehoseSink:
    """Every decision (ALLOW too) goes to S3 via Firehose for Athena analytics."""

    MAX_BATCH = 500  # Firehose PutRecordBatch limit

    def __init__(self, firehose_client, stream_name: str):
        self.fh, self.stream = firehose_client, stream_name
        self.buffer: list[dict] = []

    def emit(self, d: Decision):
        row = d.to_dict()
        row.pop("features", None)
        row.update({f"f_{k}": v for k, v in d.features.items()})  # flat columns for Athena
        self.buffer.append({"Data": (json.dumps(row, separators=(",", ":")) + "\n").encode()})

    def flush(self):
        while self.buffer:
            chunk, self.buffer = self.buffer[:self.MAX_BATCH], self.buffer[self.MAX_BATCH:]
            resp = self.fh.put_record_batch(DeliveryStreamName=self.stream, Records=chunk)
            if resp.get("FailedPutCount"):
                # retry only the failed records once; analytics loss is logged, not fatal
                failed = [r for r, res in zip(chunk, resp["RequestResponses"])
                          if res.get("ErrorCode")]
                resp = self.fh.put_record_batch(DeliveryStreamName=self.stream, Records=failed)
                if resp.get("FailedPutCount"):
                    log.error("firehose dropped %d records", resp["FailedPutCount"])
