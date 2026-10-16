import json
import math
import unittest
from pathlib import Path

from helpers import LONDON, txn
from fraud_pipeline.features import MODEL_FEATURES, UserState, compute_features, update_state
from fraud_pipeline.model import DEFAULT_MODEL_PATH, GradientBoostedModel, LogisticModel, load_model
from fraud_pipeline.pipeline import (ALLOW, BLOCK, REVIEW, FraudScorer, Pipeline,
                                     final_decision)
from fraud_pipeline.rules import RulesEngine
from fraud_pipeline.sinks import InMemoryAlertSink
from fraud_pipeline.state import InMemoryStateStore


class StubModel:
    version = "stub"

    def __init__(self, p):
        self.p = p

    def predict_proba(self, features):
        return self.p


class RulesTests(unittest.TestCase):
    def test_rules_fire_and_escalate(self):
        engine = RulesEngine([
            {"name": "big", "when": [{"feature": "amount", "op": ">", "value": 100}],
             "score": 30, "action": "review"},
            {"name": "far", "when": [{"feature": "km_from_last", "op": ">", "value": 500},
                                     {"feature": "speed_kmh", "op": ">", "value": 900}],
             "score": 90, "action": "block"},
        ])
        self.assertEqual(engine.evaluate({"amount": 50}), (0.0, [], "tag"))
        self.assertEqual(engine.evaluate({"amount": 500}), (0.3, ["big"], "review"))
        score, fired, action = engine.evaluate(
            {"amount": 500, "km_from_last": 900, "speed_kmh": 5000})
        self.assertEqual((score, fired, action), (1.0, ["big", "far"], "block"))

    def test_invalid_rule_rejected(self):
        with self.assertRaises(ValueError):
            RulesEngine([{"name": "x", "when": [{"feature": "a", "op": "~", "value": 1}]}])

    def test_shipped_rules_file_is_valid(self):
        self.assertGreater(len(RulesEngine.from_file().rules), 3)


class ModelTests(unittest.TestCase):
    def test_shipped_model_loads_and_scores(self):
        model = load_model()
        self.assertIsInstance(model, GradientBoostedModel)
        self.assertIn("review", model.thresholds)
        f = compute_features(txn(), UserState(user_id="u1"))
        self.assertTrue(0.0 <= model.predict_proba(f) <= 1.0)

    def test_logistic_math(self):
        n = len(MODEL_FEATURES)
        m = LogisticModel(MODEL_FEATURES, [0.0] * n, [1.0] * n, [0.0] * n, 0.0)
        self.assertAlmostEqual(m.predict_proba({k: 1.0 for k in MODEL_FEATURES}), 0.5)
        m = LogisticModel(MODEL_FEATURES, [0.0] * n, [1.0] * n, [1.0] + [0.0] * (n - 1), 0.0)
        self.assertAlmostEqual(m.predict_proba({k: 2.0 for k in MODEL_FEATURES}),
                               1 / (1 + math.exp(-2)))

    def test_tree_walk(self):
        # one stump: feature 0 <= 1.0 -> -2, else +2
        tree = {"left": [1, -1, -1], "right": [2, -1, -1], "feature": [0, -2, -2],
                "threshold": [1.0, -2.0, -2.0], "value": [0.0, -2.0, 2.0]}
        m = GradientBoostedModel(MODEL_FEATURES, 0.0, [tree])
        self.assertEqual(m.raw_score([0.5] + [0] * 14), -2.0)
        self.assertEqual(m.raw_score([3.0] + [0] * 14), 2.0)

    def test_feature_list_mismatch_is_caught(self):
        with self.assertRaises(ValueError):
            GradientBoostedModel(["a", "b"], 0.0, [])

    def test_model_file_round_trips(self):
        d = json.loads(Path(DEFAULT_MODEL_PATH).read_text())
        self.assertEqual(GradientBoostedModel.from_dict(d).to_dict()["features"], d["features"])


class DecisionTests(unittest.TestCase):
    def test_rules_only_escalate(self):
        self.assertEqual(final_decision(ALLOW, "block"), BLOCK)
        self.assertEqual(final_decision(BLOCK, "tag"), BLOCK)
        self.assertEqual(final_decision(REVIEW, "tag"), REVIEW)
        self.assertEqual(final_decision(ALLOW, "review"), REVIEW)

    def test_thresholds(self):
        scorer = FraudScorer(StubModel(0.0), RulesEngine([]), {"review": 0.5, "block": 0.9})
        self.assertEqual(scorer.decide(0.49), ALLOW)
        self.assertEqual(scorer.decide(0.5), REVIEW)
        self.assertEqual(scorer.decide(0.95), BLOCK)


class PipelineTests(unittest.TestCase):
    def make(self, p=0.0):
        alerts = InMemoryAlertSink()
        scorer = FraudScorer(StubModel(p), RulesEngine.from_file(), {"review": 0.5, "block": 0.9})
        return Pipeline(scorer, InMemoryStateStore(), [alerts]), alerts

    def test_redelivered_record_is_skipped(self):
        pipe, alerts = self.make(p=0.95)
        t = txn(txn_id="same")
        self.assertIsNotNone(pipe.process(t))
        self.assertIsNone(pipe.process(dict(t)))
        self.assertEqual(pipe.duplicates_skipped, 1)
        self.assertEqual(len(alerts.recent), 1)

    def test_state_not_committed_when_sink_fails(self):
        class Boom:
            def emit(self, d):
                raise RuntimeError("downstream outage")

        pipe, _ = self.make()
        pipe.sinks.append(Boom())
        with self.assertRaises(RuntimeError):
            pipe.process(txn(txn_id="t1"))
        self.assertEqual(pipe.store.get("u1").n, 0)  # retry will recompute identically

    def test_impossible_travel_rule_blocks_even_if_model_unsure(self):
        pipe, alerts = self.make(p=0.01)
        for i in range(6):
            pipe.process(txn(ts=1_000_000.0 + i * 3600))
        d = pipe.process(txn(ts=1_000_000.0 + 6 * 3600 + 900, lat=LONDON[0], lon=LONDON[1],
                             country="GB"))
        self.assertIn("impossible_travel", d.rules_fired)
        self.assertEqual(d.decision, BLOCK)
        self.assertEqual(alerts.recent[-1]["txn_id"], d.txn_id)

    def test_blocked_txn_does_not_update_profile(self):
        pipe, _ = self.make(p=0.99)
        pipe.process(txn(ts=1.0, device="attacker"))
        self.assertNotIn("attacker", pipe.store.get("u1").devices)


if __name__ == "__main__":
    unittest.main()
