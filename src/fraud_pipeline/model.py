"""Dependency-free model inference.

Models are trained with scikit-learn (ml/train.py) but exported as plain JSON.
Inference is pure Python -- a dot product for the logistic baseline, a walk
down shallow trees for the gradient-boosted model -- so the Lambda package
has zero third-party dependencies: tiny deployment, fast cold starts,
nothing to patch.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from .features import MODEL_FEATURES, model_vector

DEFAULT_MODEL_PATH = Path(__file__).parent / "config" / "model.json"


def _sigmoid(z: float) -> float:
    z = max(min(z, 40.0), -40.0)
    return 1.0 / (1.0 + math.exp(-z))


def _check_features(features):
    if list(features) != MODEL_FEATURES:
        raise ValueError("model was trained on a different feature list; retrain it")


class LogisticModel:
    kind = "logistic"

    def __init__(self, features, mean, scale, coef, intercept, version="dev", thresholds=None):
        _check_features(features)
        self.mean, self.scale = list(mean), [s or 1.0 for s in scale]
        self.coef, self.intercept, self.version = list(coef), float(intercept), version
        self.thresholds = thresholds  # chosen at training time from the evaluation PR curve

    @classmethod
    def from_dict(cls, d: dict) -> "LogisticModel":
        return cls(d["features"], d["mean"], d["scale"], d["coef"], d["intercept"],
                   d.get("version", "dev"), d.get("thresholds"))

    def predict_proba(self, features: dict) -> float:
        x = model_vector(features)
        return _sigmoid(self.intercept + sum(
            c * (v - m) / s for c, v, m, s in zip(self.coef, x, self.mean, self.scale)))

    def to_dict(self) -> dict:
        return {"kind": self.kind, "features": MODEL_FEATURES, "mean": self.mean,
                "scale": self.scale, "coef": self.coef, "intercept": self.intercept,
                "version": self.version, "thresholds": self.thresholds}


class GradientBoostedModel:
    """Sum of shallow regression trees in log-odds space.

    Each tree is stored as parallel arrays (sklearn's layout):
    left[i], right[i] child indexes (-1 = leaf), feature[i], threshold[i], value[i].
    """
    kind = "gbm"

    def __init__(self, features, init_score, trees, version="dev", thresholds=None):
        _check_features(features)
        self.init_score, self.trees = float(init_score), trees
        self.version, self.thresholds = version, thresholds

    @classmethod
    def from_dict(cls, d: dict) -> "GradientBoostedModel":
        return cls(d["features"], d["init_score"], d["trees"], d.get("version", "dev"),
                   d.get("thresholds"))

    def raw_score(self, x: list[float]) -> float:
        z = self.init_score
        for t in self.trees:
            left, right, feat, thr = t["left"], t["right"], t["feature"], t["threshold"]
            i = 0
            while left[i] != -1:
                i = left[i] if x[feat[i]] <= thr[i] else right[i]
            z += t["value"][i]
        return z

    def predict_proba(self, features: dict) -> float:
        return _sigmoid(self.raw_score(model_vector(features)))

    def to_dict(self) -> dict:
        return {"kind": self.kind, "features": MODEL_FEATURES, "init_score": self.init_score,
                "trees": self.trees, "version": self.version, "thresholds": self.thresholds}


def load_model(path: str | Path = DEFAULT_MODEL_PATH):
    d = json.loads(Path(path).read_text())
    return {"logistic": LogisticModel, "gbm": GradientBoostedModel}[d.get("kind", "logistic")] \
        .from_dict(d)
