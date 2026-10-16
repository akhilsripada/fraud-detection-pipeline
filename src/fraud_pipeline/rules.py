"""Config-driven rules engine. Rules live in config/rules.json, not in code.

Rules do two jobs:
  * reason codes -- every rule that fires is attached to the decision, so an
    analyst can see *why* a transaction was flagged (the model alone can't say)
  * overrides    -- a rule with "action": "review" or "block" forces at least
    that decision. Only rules measured to be precise get an action (see
    ml/report.md); ops can also add one during an incident without retraining.
"""
from __future__ import annotations

import json
import operator
from pathlib import Path

DEFAULT_RULES_PATH = Path(__file__).parent / "config" / "rules.json"

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt,
       "<=": operator.le, "==": operator.eq, "!=": operator.ne}
ACTIONS = {"tag": 0, "review": 1, "block": 2}


class RulesEngine:
    def __init__(self, rules: list[dict]):
        for r in rules:
            for c in r["when"]:
                if c["op"] not in OPS:
                    raise ValueError(f"rule {r['name']}: unknown op {c['op']!r}")
            if r.get("action", "tag") not in ACTIONS:
                raise ValueError(f"rule {r['name']}: unknown action {r['action']!r}")
        self.rules = rules

    @classmethod
    def from_file(cls, path: str | Path = DEFAULT_RULES_PATH) -> "RulesEngine":
        return cls(json.loads(Path(path).read_text())["rules"])

    def evaluate(self, features: dict) -> tuple[float, list[str], str]:
        """Return (score in [0,1], names of rules that fired, strongest action)."""
        fired, total, action = [], 0, "tag"
        for r in self.rules:
            if all(OPS[c["op"]](features.get(c["feature"], 0), c["value"]) for c in r["when"]):
                fired.append(r["name"])
                total += r.get("score", 0)
                a = r.get("action", "tag")
                if ACTIONS[a] > ACTIONS[action]:
                    action = a
        return min(total, 100) / 100.0, fired, action
