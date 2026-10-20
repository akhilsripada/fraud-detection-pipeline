import io
import json
import unittest

from helpers import txn  # noqa: F401  (sets up the import path)
from fraud_pipeline.assistant import Assistant, GeminiRouter, explain, parse_intent
from fraud_pipeline.store import AlertStore


def alert(**kw):
    a = {"txn_id": "t1", "user_id": "u01037", "ts": 1000.0, "amount": 412.80, "city": "Tokyo", "country": "JP",
         "lat": 35.7, "lon": 139.7, "merchant_category": "electronics", "decision": "BLOCK", "risk_score": 0.97,
         "rules_fired": ["impossible_travel", "new_device_new_country"], "label": 1, "fraud_type": "account_takeover",
         "features": {"amount_ratio": 6.2, "km_from_last": 11900, "speed_kmh": 39700, "seconds_since_last": 1080,
                      "is_new_device": 1, "is_new_country": 1, "velocity_10m": 1}}
    a.update(kw)
    return a


class ParseTests(unittest.TestCase):
    def test_intents(self):
        cases = {
            "Why was the last transaction blocked?": {"intent": "explain", "user": None},
            "explain u00042": {"intent": "explain", "user": "u00042"},
            "Show me fraud in Europe": {"intent": "region", "region": "europe"},
            "Simulate an account takeover": {"intent": "attack", "kind": "account_takeover"},
            "run a card testing attack": {"intent": "attack", "kind": "card_testing"},
            "give me a summary": {"intent": "report"},
            "show all": {"intent": "clear"},
            "what's the weather": {"intent": "unknown"},
        }
        for text, want in cases.items():
            self.assertEqual(parse_intent(text), want, text)


class ExplainTests(unittest.TestCase):
    def test_uses_real_numbers_only(self):
        e = explain(alert())
        self.assertIn("Blocked: $412.80", e["headline"])
        joined = " ".join(e["reasons"])
        self.assertIn("11,900 km", joined)
        self.assertIn("39,700 km/h", joined)
        self.assertIn("6.2×", joined)
        self.assertIn("Reply YES", e["next_step"])

    def test_model_only_decision_still_explained(self):
        e = explain(alert(decision="REVIEW", rules_fired=[], features={"amount_ratio": 1.2}))
        self.assertIn("fraud model scored it", e["reasons"][0])
        self.assertIn("analyst", e["next_step"])


class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.store = AlertStore()
        self.store.save(alert())
        self.store.save(alert(txn_id="t2", user_id="u02000", ts=2000.0, decision="REVIEW", country="US",
                              city="Tampa", rules_fired=["card_testing"], label=0))
        self.store.bump(processed=1000, flagged=2, blocked=1)
        self.injected = []
        self.a = Assistant(self.store.recent, self.store.stats, lambda k: self.injected.append(k) or "u09999")

    def test_explain_latest_block(self):
        out = self.a.ask("why was the last transaction blocked?")
        self.assertEqual(out["alert"]["txn_id"], "t1")
        self.assertEqual(out["routed_by"], "rules")

    def test_explain_named_user_and_missing_user(self):
        self.assertEqual(self.a.ask("why was u02000 flagged")["alert"]["txn_id"], "t2")
        self.assertIn("no flagged transactions", self.a.ask("explain u77777")["text"])

    def test_attack_region_report(self):
        out = self.a.ask("simulate impossible travel")
        self.assertEqual(self.injected, ["impossible_travel"])
        self.assertEqual(out["attack"]["user"], "u09999")
        self.assertIn("1 of the last 2 alerts were in Asia", self.a.ask("fraud in asia?")["text"])
        self.assertIn("Scored 1,000", self.a.ask("summary")["text"])

    def test_unknown_suggests_examples(self):
        self.assertIn("I didn't catch that", self.a.ask("hello there")["text"])


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RouterTests(unittest.TestCase):
    def router(self, reply_text=None, fail=False):
        calls = []

        def opener(req, timeout):
            calls.append(json.loads(req.data))
            if fail:
                raise OSError("network down")
            return FakeResponse(json.dumps({"candidates": [{"content": {"parts": [{"text": reply_text}]}}]}).encode())
        return GeminiRouter("key", opener=opener), calls

    def test_llm_used_only_for_unknown_and_cached(self):
        r, calls = self.router('{"intent":"region","region":"asia"}')
        store = AlertStore()
        a = Assistant(store.recent, store.stats, router=r)
        self.assertEqual(a.ask("anything weird happening over in the far east?")["routed_by"], "llm")
        a.ask("anything weird happening over in the far east?")
        a.ask("give me a summary")                       # rules handle this: no LLM call
        self.assertEqual(len(calls), 1)

    def test_rejects_invented_values(self):
        r, _ = self.router('{"intent":"region","region":"atlantis"}')
        self.assertEqual(r.route("q"), {"intent": "unknown"})
        r, _ = self.router('{"intent":"explain","user":"DROP TABLE"}')
        self.assertEqual(r.route("q2"), {"intent": "explain", "user": None})

    def test_network_failure_falls_back(self):
        r, _ = self.router(fail=True)
        self.assertEqual(r.route("q"), {"intent": "unknown"})


class StoreTests(unittest.TestCase):
    def test_idempotent_and_ordered(self):
        s = AlertStore()
        self.assertTrue(s.save(alert()))
        self.assertFalse(s.save(alert()))                 # redelivery
        s.save(alert(txn_id="t2", ts=5000.0))
        self.assertEqual([a["txn_id"] for a in s.recent()], ["t2", "t1"])
        self.assertEqual(s.get("t1")["features"]["km_from_last"], 11900)
        self.assertIsNone(s.get("nope"))

    def test_counters_and_persistence(self):
        import tempfile, os
        path = os.path.join(tempfile.mkdtemp(), "x.db")
        s = AlertStore(path)
        s.save(alert())
        s.bump(processed=5, flagged=1)
        s.bump(processed=3)
        s2 = AlertStore(path)                             # survives a restart
        self.assertEqual(s2.stats()["processed"], 8)
        self.assertEqual(len(s2.recent()), 1)


if __name__ == "__main__":
    unittest.main()
