import unittest
from collections import Counter

from helpers import txn  # noqa: F401  (sets up import path)
from fraud_pipeline.simulator import ATTACKS, TransactionSimulator, haversine_km


class SimulatorTests(unittest.TestCase):
    def test_deterministic_with_seed(self):
        a = list(TransactionSimulator(n_users=200, seed=1, start_ts=0).stream(300))
        b = list(TransactionSimulator(n_users=200, seed=1, start_ts=0).stream(300))
        self.assertEqual(a, b)

    def test_stream_is_time_ordered(self):
        ts = [t["ts"] for t in TransactionSimulator(n_users=500, seed=2, start_ts=0).stream(5000)]
        self.assertEqual(ts, sorted(ts))

    def test_labels_and_fraud_mix(self):
        # low tps = long simulated window, so slow attacks (travel) have time to land
        sim = TransactionSimulator(n_users=2000, tps=5, fraud_rate=0.02, seed=3, start_ts=0)
        txns = list(sim.stream(20_000))
        types = Counter(t["fraud_type"] for t in txns if t["label"] == 1)
        self.assertEqual(set(types), set(ATTACKS))
        self.assertTrue(all(t["fraud_type"] is None for t in txns if t["label"] == 0))

    def test_inject_attack(self):
        sim = TransactionSimulator(n_users=50, fraud_rate=0.0, seed=4, start_ts=0)
        sim.inject_attack("amount_spike")
        fraud = [t for t in sim.stream(200) if t["label"] == 1]
        self.assertEqual([t["fraud_type"] for t in fraud], ["amount_spike"])
        with self.assertRaises(ValueError):
            sim.inject_attack("nope")

    def test_haversine(self):
        self.assertAlmostEqual(haversine_km(40.71, -74.01, 51.51, -0.13), 5570, delta=20)


if __name__ == "__main__":
    unittest.main()
