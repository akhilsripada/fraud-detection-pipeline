"""Re-drive records whose processing failed after all Lambda retries.

For a Kinesis event source, the SQS on-failure destination receives only
*metadata* (shard id + sequence-number range), not the records themselves.
This script reads that metadata, fetches the original records from the
stream (they are retained for 24h by default), and puts them back on the
stream once the underlying bug is fixed. Re-driving is safe: the pipeline
skips txn_ids it has already applied, and alerts are written with a
conditional put.

    python scripts/replay_dlq.py --queue-url <url> --dry-run
    python scripts/replay_dlq.py --queue-url <url>
"""
from __future__ import annotations

import argparse
import json


def fetch_range(kinesis, stream_arn: str, shard_id: str, start_seq: str, end_seq: str):
    it = kinesis.get_shard_iterator(StreamARN=stream_arn, ShardId=shard_id,
                                    ShardIteratorType="AT_SEQUENCE_NUMBER",
                                    StartingSequenceNumber=start_seq)["ShardIterator"]
    out = []
    while it:
        resp = kinesis.get_records(ShardIterator=it, Limit=1000, StreamARN=stream_arn)
        for r in resp["Records"]:
            if int(r["SequenceNumber"]) > int(end_seq):
                return out
            out.append(r)
        if not resp["Records"] and resp.get("MillisBehindLatest", 0) == 0:
            return out
        it = resp.get("NextShardIterator")
    return out


def main():
    import boto3
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue-url", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-messages", type=int, default=100)
    args = ap.parse_args()

    sqs, kinesis = boto3.client("sqs"), boto3.client("kinesis")
    handled = 0
    while handled < args.max_messages:
        msgs = sqs.receive_message(QueueUrl=args.queue_url, MaxNumberOfMessages=10,
                                   WaitTimeSeconds=2).get("Messages", [])
        if not msgs:
            break
        for m in msgs:
            info = json.loads(m["Body"])["KinesisBatchInfo"]
            records = fetch_range(kinesis, info["streamArn"], info["shardId"],
                                  info["startSequenceNumber"], info["endSequenceNumber"])
            print(f"{info['shardId']} {info['startSequenceNumber']}..{info['endSequenceNumber']}"
                  f" -> {len(records)} records")
            if not args.dry_run:
                for i in range(0, len(records), 500):
                    kinesis.put_records(StreamARN=info["streamArn"], Records=[
                        {"Data": r["Data"], "PartitionKey": r["PartitionKey"]}
                        for r in records[i:i + 500]])
                sqs.delete_message(QueueUrl=args.queue_url, ReceiptHandle=m["ReceiptHandle"])
            handled += 1
    print(f"handled {handled} DLQ messages{' (dry run)' if args.dry_run else ''}")


if __name__ == "__main__":
    main()
