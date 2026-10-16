"""Lambda handler tests with in-memory fakes for DynamoDB, SNS, Firehose and API Gateway."""
import base64
import json
import os
import unittest

from helpers import (LONDON, ClientError, FakeApiGw, FakeFirehose, FakeSns, FakeTable, txn)
from fraud_pipeline.aws import handler as h
from fraud_pipeline.aws import ws
from fraud_pipeline.features import UserState
from fraud_pipeline.state import DynamoDBStateStore, StaleStateError


def kinesis_event(*payloads):
    recs = []
    for i, p in enumerate(payloads):
        data = p if isinstance(p, bytes) else json.dumps(p).encode()
        recs.append({"kinesis": {"sequenceNumber": str(1000 + i),
                                 "data": base64.b64encode(data).decode()}})
    return {"Records": recs}


class HandlerTests(unittest.TestCase):
    def setUp(self):
        os.environ.update(ALERT_TOPIC_ARN="arn:aws:sns:us-east-1:1:alerts",
                          FIREHOSE_STREAM="decisions", WS_CALLBACK_URL="https://x")
        self.c = {"state_table": FakeTable("user_id"), "alerts_table": FakeTable("txn_id"),
                  "connections_table": FakeTable("connection_id"), "sns": FakeSns(),
                  "firehose": FakeFirehose(), "apigw": FakeApiGw(gone={"stale"})}
        self.c["connections_table"].items = {"live": {"connection_id": "live"},
                                             "stale": {"connection_id": "stale"}}
        self.pipeline, self.broadcast = h.build_pipeline(self.c)

    def run_event(self, event):
        return h.handler(event, None, self.pipeline, self.broadcast)

    def attack_batch(self):
        normal = [txn(ts=1_000_000.0 + i * 3600, txn_id=f"n{i}") for i in range(6)]
        fraud = txn(ts=1_000_000.0 + 6 * 3600 + 600, lat=LONDON[0], lon=LONDON[1],
                    country="GB", device="evil", amount=900, category="gift_cards",
                    txn_id="fraud1", label=1)
        return normal + [fraud]

    def test_end_to_end_batch(self):
        out = self.run_event(kinesis_event(*self.attack_batch()))
        self.assertEqual(out, {"batchItemFailures": []})
        self.assertIn("fraud1", self.c["alerts_table"].items)            # alert stored
        self.assertEqual(len(self.c["sns"].published), 1)                 # human paged
        self.assertEqual(len(self.c["firehose"].records), 7)              # all decisions to lake
        self.assertIn("u1", self.c["state_table"].items)                  # state persisted
        sent = [json.loads(d) for cid, d in self.c["apigw"].sent if cid == "live"]
        types = [i["type"] for m in sent for i in m["items"]]
        self.assertIn("alert", types)
        self.assertIn("stats", types)
        self.assertNotIn("stale", self.c["connections_table"].items)     # gone conn removed

    def test_replayed_batch_is_idempotent(self):
        batch = self.attack_batch()
        self.run_event(kinesis_event(*batch))
        self.run_event(kinesis_event(*batch))                            # Kinesis redelivery
        self.assertEqual(len(self.c["sns"].published), 1)
        self.assertEqual(len(self.c["alerts_table"].items), 1)
        self.assertEqual(self.pipeline.duplicates_skipped, 7)

    def test_failure_reports_first_failed_record_and_stops(self):
        batch = self.attack_batch()
        self.c["alerts_table"].fail_next_put = ClientError("ProvisionedThroughputExceededException")
        out = self.run_event(kinesis_event(*batch))
        # the alert write for record 6 (fraud1) failed: retry starts exactly there
        self.assertEqual(out, {"batchItemFailures": [{"itemIdentifier": "1006"}]})
        self.assertEqual(json.loads(self.c["state_table"].items["u1"]["state"])["n"], 6)
        out = self.run_event(kinesis_event(batch[-1]))                    # the retry
        self.assertEqual(out, {"batchItemFailures": []})
        self.assertIn("fraud1", self.c["alerts_table"].items)

    def test_malformed_record_is_skipped_not_retried(self):
        out = self.run_event(kinesis_event(b"not json", {"no": "ids"}, txn(txn_id="ok")))
        self.assertEqual(out, {"batchItemFailures": []})
        self.assertEqual(len(self.c["firehose"].records), 1)


class StateStoreTests(unittest.TestCase):
    def test_optimistic_locking(self):
        table = FakeTable("user_id")
        a, b = DynamoDBStateStore(table), DynamoDBStateStore(table)
        s1 = a.get("u1"); s1.n = 1
        a.put(s1)
        stale, fresh = b.get("u1"), b.get("u1")
        fresh.n = 2
        b.put(fresh)
        stale.n = 99
        with self.assertRaises(StaleStateError):
            a.put(stale)
        self.assertEqual(json.loads(table.items["u1"]["state"])["n"], 2)

    def test_round_trip(self):
        store = DynamoDBStateStore(FakeTable("user_id"))
        s = UserState(user_id="u9", devices=["d1"], n=3, mean=12.5)
        store.put(s)
        self.assertEqual(store.get("u9"), s)


class WebSocketTests(unittest.TestCase):
    def test_connect_disconnect(self):
        t = FakeTable("connection_id")
        ev = {"requestContext": {"connectionId": "abc"}}
        self.assertEqual(ws.connect(ev, table=t)["statusCode"], 200)
        self.assertIn("abc", t.items)
        ws.disconnect(ev, table=t)
        self.assertNotIn("abc", t.items)


if __name__ == "__main__":
    unittest.main()
