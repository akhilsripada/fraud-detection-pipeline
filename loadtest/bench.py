"""Benchmark the scoring path on one CPU core (no network).

    python loadtest/bench.py --events 200000

Measures (1) the core pipeline per transaction -- state lookup, features,
model, rules, decision, state commit -- and (2) the full Lambda handler on
Kinesis-style batches of 100 with in-memory fakes for DynamoDB/SNS/Firehose,
which isolates our code's cost from AWS network latency. In AWS, per-record
latency is dominated by the DynamoDB round trips (~5-10 ms), which is why the
handler processes a whole Kinesis batch per invocation.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fraud_pipeline.pipeline import FraudScorer, Pipeline  # noqa: E402
from fraud_pipeline.simulator import TransactionSimulator  # noqa: E402
from fraud_pipeline.state import InMemoryStateStore  # noqa: E402


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * (len(xs) - 1)))]


def bench_pipeline(txns):
    pipe = Pipeline(FraudScorer.default(), InMemoryStateStore())
    lat = []
    t0 = time.perf_counter()
    for t in txns:
        s = time.perf_counter()
        pipe.process(t)
        lat.append((time.perf_counter() - s) * 1000)
    total = time.perf_counter() - t0
    return {"txns": len(txns), "throughput_tps": round(len(txns) / total),
            "p50_ms": round(pct(lat, 50), 3), "p99_ms": round(pct(lat, 99), 3),
            "max_ms": round(max(lat), 3)}


def bench_handler(txns, batch=100):
    from helpers import FakeApiGw, FakeFirehose, FakeSns, FakeTable
    from fraud_pipeline.aws import handler as h
    os.environ.update(ALERT_TOPIC_ARN="arn:x", FIREHOSE_STREAM="s", WS_CALLBACK_URL="https://x")
    clients = {"state_table": FakeTable("user_id"), "alerts_table": FakeTable("txn_id"),
               "connections_table": FakeTable("connection_id"), "sns": FakeSns(),
               "firehose": FakeFirehose(), "apigw": FakeApiGw()}
    pipe, bc = h.build_pipeline(clients)
    events = []
    for i in range(0, len(txns), batch):
        events.append({"Records": [{"kinesis": {"sequenceNumber": str(i + j), "data":
                       base64.b64encode(json.dumps(t).encode()).decode()}}
                       for j, t in enumerate(txns[i:i + batch])]})
    lat = []
    devnull = open(os.devnull, "w")
    stdout, sys.stdout = sys.stdout, devnull  # silence EMF log lines
    try:
        t0 = time.perf_counter()
        for ev in events:
            s = time.perf_counter()
            h.handler(ev, None, pipe, bc)
            lat.append((time.perf_counter() - s) * 1000)
        total = time.perf_counter() - t0
    finally:
        sys.stdout = stdout
    return {"batches": len(events), "batch_size": batch,
            "throughput_tps": round(len(txns) / total),
            "batch_p50_ms": round(pct(lat, 50), 2), "batch_p99_ms": round(pct(lat, 99), 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=200_000)
    args = ap.parse_args()
    print(f"generating {args.events:,} transactions...")
    txns = list(TransactionSimulator(seed=11, start_ts=1_760_000_000).stream(args.events))
    env = f"{platform.python_implementation()} {platform.python_version()}, {platform.machine()}"
    p = bench_pipeline(txns)
    hnd = bench_handler(txns[: min(len(txns), 50_000)])
    print(json.dumps({"env": env, "pipeline": p, "lambda_handler_with_fakes": hnd}, indent=2))
    (ROOT / "loadtest" / "results.md").write_text(
        "# Benchmark results\n\n"
        f"Single CPU core, {env}, `python loadtest/bench.py --events {args.events}`.\n\n"
        "| Path | Throughput | Latency |\n|---|---|---|\n"
        f"| Core pipeline (state, features, GBM, rules, decision) | "
        f"{p['throughput_tps']:,} txns/s | p50 {p['p50_ms']} ms, p99 {p['p99_ms']} ms per txn |\n"
        f"| Lambda handler, batches of {hnd['batch_size']} (AWS calls faked) | "
        f"{hnd['throughput_tps']:,} txns/s | p50 {hnd['batch_p50_ms']} ms, p99 "
        f"{hnd['batch_p99_ms']} ms per batch |\n\n"
        "These numbers isolate compute. In AWS, end-to-end latency adds Kinesis "
        "propagation and DynamoDB round trips; measure it with `scripts/producer.py` "
        "and the `EndToEndLatencyMs` metric on the CloudWatch dashboard.\n")


if __name__ == "__main__":
    main()
