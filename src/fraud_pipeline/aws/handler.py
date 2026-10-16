"""Lambda consumer for the Kinesis transaction stream.

Failure handling
----------------
* Records are processed in shard order. On the first failure we stop and
  report that record's sequence number via ReportBatchItemFailures, so Lambda
  retries from exactly that record (earlier ones are not reprocessed, later
  ones keep their order).
* After the configured retries, Lambda bisects the batch and finally sends
  the failing range's metadata to the SQS dead-letter queue
  (scripts/replay_dlq.py re-drives it).
* Malformed JSON can never succeed, so it is logged, counted and skipped
  instead of blocking the shard forever (a "poison pill").

Metrics are written as CloudWatch Embedded Metric Format (EMF) log lines:
no PutMetricData API calls, no extra latency, no extra cost per call.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time

from ..pipeline import FraudScorer, Pipeline, BLOCK, ALLOW
from ..sinks import (DynamoAlertSink, FirehoseSink, SnsBlockSink, WebSocketBroadcastSink)
from ..state import DynamoDBStateStore

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_pipeline: Pipeline | None = None
_broadcast: WebSocketBroadcastSink | None = None


def build_pipeline(clients: dict | None = None) -> tuple[Pipeline, WebSocketBroadcastSink | None]:
    """Wire the pipeline from environment variables. `clients` lets tests inject fakes."""
    env = os.environ
    if clients is None:
        import boto3  # imported lazily: unit tests run without boto3 installed
        ddb = boto3.resource("dynamodb")
        clients = {
            "state_table": ddb.Table(env["STATE_TABLE"]),
            "alerts_table": ddb.Table(env["ALERTS_TABLE"]),
            "connections_table": ddb.Table(env["CONNECTIONS_TABLE"])
            if env.get("CONNECTIONS_TABLE") else None,
            "sns": boto3.client("sns") if env.get("ALERT_TOPIC_ARN") else None,
            "firehose": boto3.client("firehose") if env.get("FIREHOSE_STREAM") else None,
            "apigw": boto3.client("apigatewaymanagementapi", endpoint_url=env["WS_CALLBACK_URL"])
            if env.get("WS_CALLBACK_URL") else None,
        }

    broadcast = None
    downstream = []
    if clients.get("sns"):
        downstream.append(SnsBlockSink(clients["sns"], env["ALERT_TOPIC_ARN"]))
    if clients.get("apigw") and clients.get("connections_table"):
        broadcast = WebSocketBroadcastSink(clients["apigw"], clients["connections_table"])
        downstream.append(broadcast)

    sinks = [DynamoAlertSink(clients["alerts_table"], downstream)]
    if clients.get("firehose"):
        sinks.append(FirehoseSink(clients["firehose"], env["FIREHOSE_STREAM"]))

    pipeline = Pipeline(FraudScorer.default(), DynamoDBStateStore(clients["state_table"]), sinks)
    return pipeline, broadcast


def _emf(metrics: dict, values: dict):
    """Print one CloudWatch Embedded Metric Format record."""
    print(json.dumps({
        "_aws": {"Timestamp": int(time.time() * 1000), "CloudWatchMetrics": [{
            "Namespace": "FraudPipeline", "Dimensions": [[]],
            "Metrics": [{"Name": k, "Unit": u} for k, u in metrics.items()]}]},
        **values,
    }))


def _percentile(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def handler(event, context=None, pipeline: Pipeline | None = None,
            broadcast: WebSocketBroadcastSink | None = None):
    global _pipeline, _broadcast
    if pipeline is None:
        if _pipeline is None:  # built once per container, reused while warm
            _pipeline, _broadcast = build_pipeline()
        pipeline, broadcast = _pipeline, _broadcast

    failures, decision_ms, e2e_ms = [], [], []
    counts = {"processed": 0, "flagged": 0, "blocked": 0, "malformed": 0}
    dupes_before = pipeline.duplicates_skipped

    for rec in event.get("Records", []):
        seq = rec["kinesis"]["sequenceNumber"]
        try:
            txn = json.loads(base64.b64decode(rec["kinesis"]["data"]))
            txn["txn_id"], txn["user_id"]  # required keys
        except (ValueError, KeyError, TypeError):
            counts["malformed"] += 1
            log.error("malformed record skipped seq=%s", seq)
            continue
        try:
            d = pipeline.process(txn)
        except Exception:
            log.exception("failed on txn %s (seq %s); retrying from here", txn.get("txn_id"), seq)
            failures.append({"itemIdentifier": seq})
            break  # keep shard order: everything after this is retried too
        if d is None:
            continue
        counts["processed"] += 1
        counts["flagged"] += d.decision != ALLOW
        counts["blocked"] += d.decision == BLOCK
        decision_ms.append(d.latency_ms)
        e2e_ms.append(max(0.0, (time.time() - d.ts) * 1000))

    pipeline.flush()  # firehose + websocket buffers, for everything that succeeded

    if broadcast is not None and counts["processed"]:
        broadcast.send({"type": "stats", **counts,
                        "p50_ms": round(_percentile(e2e_ms, 50), 1),
                        "p99_ms": round(_percentile(e2e_ms, 99), 1)})
        broadcast.flush()

    duplicates = pipeline.duplicates_skipped - dupes_before
    _emf({"Processed": "Count", "Flagged": "Count", "Blocked": "Count", "Duplicates": "Count",
          "Malformed": "Count", "DecisionLatencyMs": "Milliseconds",
          "EndToEndLatencyMs": "Milliseconds"},
         {"Processed": counts["processed"], "Flagged": counts["flagged"],
          "Blocked": counts["blocked"], "Duplicates": duplicates,
          "Malformed": counts["malformed"],
          "DecisionLatencyMs": decision_ms or [0], "EndToEndLatencyMs": e2e_ms or [0]})

    return {"batchItemFailures": failures}
