# Benchmark results

Single CPU core, CPython 3.13.16, x86_64, `python loadtest/bench.py --events 200000`.

| Path | Throughput | Latency |
|---|---|---|
| Core pipeline (state, features, GBM, rules, decision) | 6,643 txns/s | p50 0.138 ms, p99 0.273 ms per txn |
| Lambda handler, batches of 100 (AWS calls faked) | 4,826 txns/s | p50 19.06 ms, p99 29.49 ms per batch |

These numbers isolate compute. In AWS, end-to-end latency adds Kinesis propagation and DynamoDB round trips; measure it with `scripts/producer.py` and the `EndToEndLatencyMs` metric on the CloudWatch dashboard.
