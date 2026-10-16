"""The scoring pipeline: state -> features -> rules + model -> decision -> sinks.

Processing order (and why):
  1. load user state; if this txn_id was already applied, skip (redelivery)
  2. compute features from PRIOR state, score, decide
  3. run sinks (alerts table, notifications, dashboard, data lake)
  4. commit the new state LAST

If anything in 3 fails, state is untouched, so a retry recomputes exactly the
same features and decision. The alerts sink uses a conditional write on
txn_id, so a retried alert is detected as a duplicate instead of doubled.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict

from .features import compute_features, update_state
from .model import load_model
from .rules import RulesEngine

ALLOW, REVIEW, BLOCK = "ALLOW", "REVIEW", "BLOCK"
DEFAULT_THRESHOLDS = {"review": 0.5, "block": 0.85}


@dataclass
class Decision:
    txn_id: str
    user_id: str
    ts: float
    amount: float
    city: str
    country: str
    lat: float
    lon: float
    merchant_category: str
    risk_score: float
    model_score: float
    rule_score: float
    rules_fired: list
    decision: str
    model_version: str
    latency_ms: float
    features: dict = field(default_factory=dict)
    label: int | None = None          # ground truth when available (simulation only)
    fraud_type: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


RANK = {ALLOW: 0, REVIEW: 1, BLOCK: 2}
RULE_ACTION_TO_DECISION = {"tag": ALLOW, "review": REVIEW, "block": BLOCK}


def final_decision(model_decision: str, rule_action: str) -> str:
    """The model decides; a precise rule can only escalate, never downgrade.

    (An earlier version blended rule scores into the risk score. Measured on
    held-out data that *lowered* PR-AUC from 0.94 to 0.90 because the broad
    rules are noisy, so rules became reason codes + targeted overrides.)"""
    rule_decision = RULE_ACTION_TO_DECISION[rule_action]
    return max(model_decision, rule_decision, key=RANK.__getitem__)


class FraudScorer:
    def __init__(self, model, rules: RulesEngine,
                 thresholds: dict | None = None):
        self.model, self.rules = model, rules
        self.thresholds = thresholds or DEFAULT_THRESHOLDS

    @classmethod
    def default(cls) -> "FraudScorer":
        model = load_model()
        return cls(model, RulesEngine.from_file(), model.thresholds)

    def decide(self, risk: float) -> str:
        if risk >= self.thresholds["block"]:
            return BLOCK
        if risk >= self.thresholds["review"]:
            return REVIEW
        return ALLOW

    def score(self, txn: dict, state) -> Decision:
        start = time.perf_counter()
        f = compute_features(txn, state)
        m = self.model.predict_proba(f)
        r, fired, action = self.rules.evaluate(f)
        return Decision(
            txn_id=txn["txn_id"], user_id=txn["user_id"], ts=float(txn["ts"]),
            amount=float(txn["amount"]), city=txn.get("city", ""), country=txn.get("country", ""),
            lat=float(txn["lat"]), lon=float(txn["lon"]),
            merchant_category=txn.get("merchant_category", ""),
            risk_score=round(m, 4), model_score=round(m, 4), rule_score=round(r, 4),
            rules_fired=fired, decision=final_decision(self.decide(m), action),
            model_version=self.model.version,
            latency_ms=round((time.perf_counter() - start) * 1000, 3),
            features={k: round(v, 4) for k, v in f.items()},
            label=txn.get("label"), fraud_type=txn.get("fraud_type"),
        )


class Pipeline:
    def __init__(self, scorer: FraudScorer, state_store, sinks: list | None = None):
        self.scorer, self.store, self.sinks = scorer, state_store, sinks or []
        self.duplicates_skipped = 0

    def process(self, txn: dict) -> Decision | None:
        state = self.store.get(txn["user_id"])
        if state.has_seen(txn["txn_id"]):
            self.duplicates_skipped += 1
            return None
        decision = self.scorer.score(txn, state)
        for sink in self.sinks:
            sink.emit(decision)
        update_state(state, txn, trusted=decision.decision != BLOCK)
        self.store.put(state)
        return decision

    def flush(self):
        for sink in self.sinks:
            if hasattr(sink, "flush"):
                sink.flush()
