"""Stream simulated transactions into Kinesis (or stdout).

    python scripts/producer.py --stream fraud-transactions --tps 100 --duration 300
    python scripts/producer.py --stdout --tps 5 --duration 10

Uses PutRecords in batches (up to 500 records / call) with the user_id as the
partition key, so each user's transactions stay ordered on one shard.
Records that Kinesis throttles (ProvisionedThroughputExceeded) are retried
with exponential backoff instead of being dropped.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fraud_pipeline.simulator import TransactionSimulator  # noqa: E402


def put_with_retry(kinesis, stream: str, records: list[dict], max_attempts: int = 6) -> int:
    """Returns the number of records that could not be written."""
    for attempt in range(max_attempts):
        resp = kinesis.put_records(StreamName=stream, Records=records)
        if not resp["FailedRecordCount"]:
            return 0
        records = [r for r, res in zip(records, resp["Records"]) if "ErrorCode" in res]
        time.sleep(min(2 ** attempt * 0.1, 3))
    return len(records)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", default="fraud-transactions")
    ap.add_argument("--region", default=None)
    ap.add_argument("--tps", type=float, default=50)
    ap.add_argument("--duration", type=float, default=60, help="seconds")
    ap.add_argument("--fraud-rate", type=float, default=0.02,
                    help="higher than training so the demo shows alerts quickly")
    ap.add_argument("--users", type=int, default=2000)
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--duplicate-rate", type=float, default=0.0,
                    help="re-send this fraction of records to prove idempotency")
    args = ap.parse_args()

    sim = TransactionSimulator(n_users=args.users, tps=args.tps, fraud_rate=args.fraud_rate,
                               seed=None)
    kinesis = None
    if not args.stdout:
        import boto3
        kinesis = boto3.client("kinesis", region_name=args.region)

    deadline, batch, sent, dropped, last_flush = time.time() + args.duration, [], 0, 0, time.time()
    for txn in sim.realtime():
        if time.time() > deadline:
            break
        records = [txn] + ([txn] if sim.rng.random() < args.duplicate_rate else [])
        for t in records:
            if args.stdout:
                print(json.dumps(t))
                continue
            batch.append({"Data": json.dumps(t).encode(), "PartitionKey": t["user_id"]})
        # flush small batches often: latency matters more than batching efficiency here
        if batch and (len(batch) >= 500 or time.time() - last_flush > 0.2):
            dropped += put_with_retry(kinesis, args.stream, batch)
            sent += len(batch)
            batch, last_flush = [], time.time()
    if batch:
        dropped += put_with_retry(kinesis, args.stream, batch)
        sent += len(batch)
    if not args.stdout:
        print(f"sent {sent} records, {dropped} dropped after retries", file=sys.stderr)


if __name__ == "__main__":
    main()
