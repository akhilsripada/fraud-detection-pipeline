import unittest

from helpers import LONDON, txn
from fraud_pipeline.features import (MODEL_FEATURES, UserState, compute_features,
                                     model_vector, update_state)


def history(n=10, start=1_000_000.0, step=3600.0, amount=25.0):
    s = UserState(user_id="u1")
    for i in range(n):
        update_state(s, txn(ts=start + i * step, amount=amount))
    return s, start + n * step


class FeatureTests(unittest.TestCase):
    def test_new_user_has_neutral_features(self):
        f = compute_features(txn(), UserState(user_id="u1"))
        self.assertEqual(f["velocity_1m"], 1)
        self.assertEqual(f["is_new_device"], 0.0)  # nothing to compare against yet
        self.assertEqual(f["amount_zscore"], 0.0)
        self.assertEqual(f["has_history"], 0.0)

    def test_velocity_windows(self):
        s = UserState(user_id="u1")
        for i in range(4):
            update_state(s, txn(ts=1000.0 + i * 10))
        f = compute_features(txn(ts=1045.0), s)
        self.assertEqual(f["velocity_1m"], 5)
        f = compute_features(txn(ts=1000.0 + 30 + 400), s)  # 1m window has slid past
        self.assertEqual(f["velocity_1m"], 1)
        self.assertEqual(f["velocity_10m"], 5)

    def test_old_timestamps_are_pruned(self):
        s = UserState(user_id="u1")
        update_state(s, txn(ts=0.0))
        update_state(s, txn(ts=10_000.0))
        self.assertEqual(s.recent_ts, [10_000.0])

    def test_impossible_travel_speed(self):
        s, t = history()
        last = t - 3600.0  # history() returns the time one step after the last txn
        f = compute_features(txn(ts=last + 1800, lat=LONDON[0], lon=LONDON[1], country="GB"), s)
        self.assertGreater(f["km_from_last"], 6500)
        self.assertGreater(f["speed_kmh"], 10_000)
        self.assertEqual(f["is_new_country"], 1.0)
        self.assertEqual(f["is_foreign"], 1.0)

    def test_amount_spike_zscore_and_ratio(self):
        s, t = history(amount=25.0)
        f = compute_features(txn(ts=t, amount=1000.0), s)
        self.assertAlmostEqual(f["amount_ratio"], 40.0)
        self.assertGreater(f["amount_zscore"], 10)

    def test_welford_matches_direct_computation(self):
        s = UserState(user_id="u1")
        amounts = [10.0, 20.0, 35.5, 7.25, 99.0]
        for i, a in enumerate(amounts):
            update_state(s, txn(ts=float(i), amount=a))
        mean = sum(amounts) / len(amounts)
        var = sum((a - mean) ** 2 for a in amounts) / (len(amounts) - 1)
        self.assertAlmostEqual(s.mean, mean)
        self.assertAlmostEqual(s.std ** 2, var)

    def test_untrusted_txn_does_not_teach_profile(self):
        s, t = history()
        update_state(s, txn(ts=t, device="evil", country="RO", amount=5000), trusted=False)
        self.assertNotIn("evil", s.devices)
        self.assertNotIn("RO", s.countries)
        self.assertEqual(s.n, 10)                  # spending profile untouched
        self.assertEqual(len(s.recent_ts), 2)      # ...but velocity still counts it

    def test_state_round_trips(self):
        s, _ = history(3)
        self.assertEqual(UserState.from_dict(s.to_dict()), s)

    def test_model_vector_order(self):
        s, t = history()
        f = compute_features(txn(ts=t), s)
        self.assertEqual(len(model_vector(f)), len(MODEL_FEATURES))
        self.assertEqual(model_vector(f)[0], f[MODEL_FEATURES[0]])


if __name__ == "__main__":
    unittest.main()
