"""The cardholder side: merchant names, local time, purchase timeline, answers, card freeze, push alerts."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import LONDON, txn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "local"))

import demo_server  # noqa: E402
from fraud_pipeline.customer import CustomerDesk, PushNotifier, local_time, merchant  # noqa: E402
from fraud_pipeline.store import AlertStore  # noqa: E402

TS = 1_792_600_000.0  # 2026-10-21 16:26:40 UTC


def alert_for(t, decision="BLOCK", **extra):
    return {"type": "alert", "txn_id": t["txn_id"], "user_id": t["user_id"], "ts": t["ts"], "amount": t["amount"],
            "city": t["city"], "country": t["country"], "lat": t["lat"], "lon": t["lon"],
            "merchant_category": t["merchant_category"], "decision": decision, "risk_score": 0.97,
            "rules_fired": ["impossible_travel"], **extra}


class MerchantAndTimeTests(unittest.TestCase):
    def test_merchant_name_is_stable_and_names_the_city(self):
        a = merchant("m0042", "electronics", "London")
        self.assertEqual(a, merchant("m0042", "electronics", "London"))
        self.assertTrue(a.endswith(", London"))

    def test_local_time_uses_the_merchants_time_zone(self):
        london, tampa = local_time(TS, "London"), local_time(TS, "Tampa")
        self.assertEqual(london["utc"], "2026-10-21 16:26:40 UTC")
        self.assertIn("5:26 PM", london["local"])   # BST in October
        self.assertIn("12:26 PM", tampa["local"])   # EDT
        self.assertEqual(local_time(TS, "Atlantis")["tz"], "UTC")


class DeskTests(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "c.db")
        self.store = AlertStore(self.path)
        self.desk = CustomerDesk(self.store, clock=lambda: TS + 600)

    def _impossible_trip(self):
        home = txn(user="u7", ts=TS - 300)
        away = txn(user="u7", ts=TS, lat=LONDON[0], lon=LONDON[1], country="GB", amount=900)
        self.desk.record(home, "ALLOW")
        self.desk.record(away, "BLOCK")
        a = alert_for(away)
        self.store.save(a)
        return home, a

    def test_context_has_where_when_and_the_trip_that_gave_it_away(self):
        home, a = self._impossible_trip()
        c = self.desk.context(a)
        self.assertEqual((c["merchant"]["lat"], c["merchant"]["lon"]), LONDON)
        self.assertEqual(c["when"]["tz"], "BST")
        self.assertEqual([t["txn_id"] for t in c["timeline"]], [home["txn_id"], a["txn_id"]])
        gap = c["timeline"][-1]["gap"]
        self.assertGreater(gap["km"], 6900)          # Tampa to London
        self.assertEqual(gap["minutes"], 5.0)
        self.assertGreater(gap["kmh"], 80_000)       # nobody flies that fast
        self.assertTrue(c["timeline"][-1]["this"])
        self.assertIsNone(c["answer"])

    def test_not_mine_freezes_the_card_and_is_idempotent(self):
        _, a = self._impossible_trip()
        out = self.desk.answer(a, "not_mine")
        self.assertEqual((out["status"], out["label"], out["frozen"]), ("recorded", 1, True))
        again = self.desk.answer(a, "mine")         # a second tap can't flip the answer
        self.assertEqual((again["status"], again["verdict"]), ("already_answered", "not_mine"))
        self.assertTrue(self.desk.is_frozen("u7"))
        self.assertEqual([f["label"] for f in self.desk.feedback()], [1])

    def test_mine_is_a_false_alarm_and_keeps_the_card_open(self):
        _, a = self._impossible_trip()
        out = self.desk.answer(a, "mine")
        self.assertEqual((out["label"], out["frozen"]), (0, False))
        self.assertFalse(self.desk.is_frozen("u7"))

    def test_frozen_cards_and_answers_survive_a_restart(self):
        _, a = self._impossible_trip()
        self.desk.answer(a, "not_mine")
        reopened = CustomerDesk(AlertStore(self.path))
        self.assertTrue(reopened.is_frozen("u7"))
        self.assertEqual(reopened.context(a)["answer"]["verdict"], "not_mine")
        reopened.unfreeze("u7")
        self.assertFalse(CustomerDesk(AlertStore(self.path)).is_frozen("u7"))

    def test_bad_verdict_is_rejected(self):
        _, a = self._impossible_trip()
        with self.assertRaises(ValueError):
            self.desk.answer(a, "maybe")


class FrozenCardEngineTests(unittest.TestCase):
    def test_every_purchase_on_a_frozen_card_is_blocked(self):
        engine = demo_server.Engine(demo_server.Hub(), tps=100, users=50, fraud_rate=0, warmup=0,
                                    store=AlertStore(os.path.join(tempfile.mkdtemp(), "e.db")))
        engine._on_txn(txn(user="u9", ts=TS, amount=12))
        self.assertEqual(engine.totals["blocked"], 0)
        engine.desk.frozen.add("u9")
        engine._on_txn(txn(user="u9", ts=TS + 60, amount=12))
        alert = engine.store.recent(1, "u9")[0]
        self.assertEqual(alert["decision"], "BLOCK")
        self.assertIn("card_frozen", alert["rules_fired"])
        self.assertTrue(alert["merchant"].endswith(", Tampa"))
        self.assertEqual(engine.totals["fp"], 0)    # the freeze isn't counted against the model
        self.assertEqual(len(engine.desk.history["u9"]), 2)


class PushTests(unittest.TestCase):
    def test_push_only_for_blocks_and_rate_limited(self):
        sent, now = [], [0.0]

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def opener(req, timeout):
            sent.append(req)
            return Resp()

        p = PushNotifier("my-topic", "http://192.168.1.20:8000/", opener=opener, clock=lambda: now[0], min_gap=20)
        a = alert_for(txn(ts=TS, amount=1234.5))
        self.assertFalse(p.notify({**a, "decision": "REVIEW"}))
        self.assertTrue(p.notify(a))
        self.assertFalse(p.notify(a))                # within 20 s
        now[0] = 25
        self.assertTrue(p.notify(a))
        req = sent[0]
        self.assertEqual(req.full_url, "https://ntfy.sh/my-topic")
        self.assertIn(b"$1,234.50", req.data)
        self.assertEqual(req.get_header("Click"), f"http://192.168.1.20:8000/customer/{a['txn_id']}")

    def test_push_failure_never_raises(self):
        def broken(req, timeout):
            raise OSError("offline")
        p = PushNotifier("t", "http://x", opener=broken)
        self.assertFalse(p.notify(alert_for(txn(ts=TS))))


if __name__ == "__main__":
    unittest.main()
