"""Train the fraud model on simulated data and write an evaluation report.

    python ml/train.py                 # ~5 min on a laptop
    python ml/train.py --events 200000 # quicker, noisier

Steps
  1. Simulate a transaction stream with ground-truth labels.
  2. Compute features with the SAME code the Lambda uses (no train/serve skew).
  3. Time-based split: train on the first 70% of the stream, evaluate on the
     last 30%. A random split would leak future user behaviour into training.
  4. Fit a logistic-regression baseline and a gradient-boosted tree model.
     Export the GBM as plain JSON and verify it matches sklearn's output.
  5. Pick decision thresholds on the held-out PR curve, measure every rule's
     precision, and report the whole system (model + rule overrides).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, roc_auc_score
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fraud_pipeline.features import (MODEL_FEATURES, compute_features,  # noqa: E402
                                     model_vector, update_state)
from fraud_pipeline.model import GradientBoostedModel, LogisticModel  # noqa: E402
from fraud_pipeline.pipeline import (ALLOW, BLOCK, REVIEW,  # noqa: E402
                                     final_decision)
from fraud_pipeline.rules import RulesEngine  # noqa: E402
from fraud_pipeline.simulator import TransactionSimulator  # noqa: E402
from fraud_pipeline.state import InMemoryStateStore  # noqa: E402

CACHE = ROOT / "ml" / ".cache"


def build_dataset(n_events: int, seed: int):
    cache = CACHE / f"dataset-{n_events}-{seed}.npz"
    if cache.exists():
        d = np.load(cache, allow_pickle=True)
        return d["X"], d["y"], d["types"], list(d["fired"])
    sim = TransactionSimulator(n_users=20000, tps=200, fraud_rate=0.004, seed=seed,
                               start_ts=1_760_000_000)
    store, rules = InMemoryStateStore(), RulesEngine.from_file()
    X, y, types, fired_all = [], [], [], []
    for txn in sim.stream(n_events):
        state = store.get(txn["user_id"])
        f = compute_features(txn, state)
        _, fired, _ = rules.evaluate(f)
        X.append(model_vector(f)); y.append(txn["label"])
        types.append(txn["fraud_type"] or "legit"); fired_all.append(fired)
        # Assume caught fraud is excluded from the user's profile (as BLOCK does in prod).
        update_state(state, txn, trusted=txn["label"] == 0)
        store.put(state)
    X, y, types = np.array(X), np.array(y), np.array(types)
    CACHE.mkdir(exist_ok=True)
    np.savez_compressed(cache, X=X, y=y, types=types, fired=np.array(fired_all, dtype=object))
    return X, y, types, fired_all


def prf(y_true, y_pred):
    tp = int(((y_pred == 1) & (y_true == 1)).sum())
    fp = int(((y_pred == 1) & (y_true == 0)).sum())
    fn = int(((y_pred == 0) & (y_true == 1)).sum())
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f1, 4),
            "tp": tp, "fp": fp, "fn": fn}


def pick_thresholds(y, scores):
    """review = best-F1 threshold.
    block  = lowest threshold above review with >= 95% precision, because
             auto-declining a real customer's card is expensive."""
    grid = np.round(np.arange(0.05, 0.995, 0.01), 2)
    stats = {float(t): prf(y, (scores >= t).astype(int)) for t in grid}
    review = max(stats, key=lambda t: stats[t]["f1"])
    block = next((t for t in sorted(stats) if t > review and stats[t]["precision"] >= 0.95
                  and stats[t]["tp"] > 0), 0.95)
    return {"review": review, "block": max(block, review + 0.05)}


def export_gbm(gbm, prior, version):
    trees = []
    for est in gbm.estimators_[:, 0]:
        tr = est.tree_
        trees.append({"left": tr.children_left.tolist(), "right": tr.children_right.tolist(),
                      "feature": tr.feature.tolist(),
                      "threshold": [round(float(v), 6) for v in tr.threshold],
                      "value": [round(float(v) * gbm.learning_rate, 6)
                                for v in tr.value[:, 0, 0]]})
    return GradientBoostedModel(MODEL_FEATURES, float(np.log(prior / (1 - prior))), trees,
                                version)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=600_000)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--trees", type=int, default=150)
    ap.add_argument("--out", default=str(ROOT / "src/fraud_pipeline/config/model.json"))
    args = ap.parse_args()

    t0 = time.time()
    X, y, types, fired = build_dataset(args.events, args.seed)
    split = int(len(y) * 0.7)
    print(f"dataset: {len(y):,} txns ({y.mean():.2%} fraud) in {time.time() - t0:.1f}s")
    Xtr, ytr, Xt, yt = X[:split], y[:split], X[split:], y[split:]
    tt, fired_t = types[split:], fired[split:]
    names, version = MODEL_FEATURES, time.strftime("%Y%m%d-%H%M%S")

    # --- baseline: logistic regression -------------------------------------
    scaler = StandardScaler().fit(Xtr)
    lr = LogisticRegression(class_weight="balanced", C=0.5, max_iter=5000)
    lr.fit(scaler.transform(Xtr), ytr)
    baseline = LogisticModel(names, scaler.mean_.tolist(), scaler.scale_.tolist(),
                             lr.coef_[0].tolist(), float(lr.intercept_[0]), "lr-" + version)

    # --- production model: gradient-boosted trees ---------------------------
    t1 = time.time()
    gbm = GradientBoostingClassifier(n_estimators=args.trees, max_depth=4, learning_rate=0.12,
                                     subsample=0.8, random_state=args.seed)
    gbm.fit(Xtr, ytr)
    model = export_gbm(gbm, float(ytr.mean()), "gbm-" + version)
    print(f"trained GBM ({args.trees} trees) in {time.time() - t1:.1f}s")

    # Parity: the pure-Python export must reproduce sklearn exactly.
    sample = Xt[:3000]
    diff = float(np.abs(np.array([model.raw_score(list(r)) for r in sample])
                        - gbm.decision_function(sample)).max())
    assert diff < 1e-3, f"exported GBM disagrees with sklearn (max diff {diff})"
    print(f"export parity OK (max abs diff {diff:.1e})")

    # Score the held-out set through the exported models, exactly as production does.
    gbm_p = np.array([model.predict_proba(dict(zip(names, r))) for r in Xt])
    lr_p = np.array([baseline.predict_proba(dict(zip(names, r))) for r in Xt])
    thresholds = pick_thresholds(yt, gbm_p)
    model.thresholds = thresholds

    # Per-rule precision on held-out data decides which rules may override.
    rules = RulesEngine.from_file()
    rule_stats = {}
    for r in rules.rules:
        hit = np.array([r["name"] in f for f in fired_t], dtype=int)
        rule_stats[r["name"]] = {**prf(yt, hit), "action": r.get("action", "tag")}

    action_of = {r["name"]: r.get("action", "tag") for r in rules.rules}
    rank = {"tag": 0, "review": 1, "block": 2}

    def decide(p, f):
        md = BLOCK if p >= thresholds["block"] else REVIEW if p >= thresholds["review"] else ALLOW
        ra = max((action_of[n] for n in f), key=rank.__getitem__, default="tag")
        return final_decision(md, ra)

    decisions = np.array([decide(p, f) for p, f in zip(gbm_p, fired_t)])
    flagged = (decisions != ALLOW).astype(int)
    blocked = (decisions == BLOCK).astype(int)
    any_rule = np.array([1 if f else 0 for f in fired_t])

    report = {
        "model_version": model.version,
        "events": len(y), "train_events": split, "test_events": len(yt),
        "fraud_rate": round(float(y.mean()), 5),
        "thresholds": thresholds,
        "test_metrics": {
            "gbm_roc_auc": round(float(roc_auc_score(yt, gbm_p)), 4),
            "gbm_pr_auc": round(float(average_precision_score(yt, gbm_p)), 4),
            "baseline_lr_pr_auc": round(float(average_precision_score(yt, lr_p)), 4),
            "any_rule_fired": prf(yt, any_rule),
            "baseline_lr@0.5": prf(yt, (lr_p >= 0.5).astype(int)),
            "gbm@review_threshold": prf(yt, (gbm_p >= thresholds["review"]).astype(int)),
            "system_flagged": prf(yt, flagged),
            "system_blocked": prf(yt, blocked),
        },
        "rules": rule_stats,
        "flagged_recall_by_fraud_type": {},
        "false_positive_rate": round(float(((flagged == 1) & (yt == 0)).sum() / (yt == 0).sum()), 6),
        "feature_importance": dict(sorted(
            zip(names, (round(float(v), 4) for v in gbm.feature_importances_)),
            key=lambda kv: -kv[1])),
    }
    by_type = defaultdict(lambda: [0, 0])
    for t, f in zip(tt, flagged):
        if t != "legit":
            by_type[t][0] += int(f); by_type[t][1] += 1
    report["flagged_recall_by_fraud_type"] = {
        t: {"caught": c, "total": n, "recall": round(c / n, 4)} for t, (c, n) in sorted(by_type.items())}

    Path(args.out).write_text(json.dumps(model.to_dict(), separators=(",", ":")))
    (ROOT / "ml/report.json").write_text(json.dumps(report, indent=2))
    write_markdown(report)
    print(json.dumps({k: v for k, v in report.items() if k not in ("feature_importance",)},
                     indent=1))
    print(f"wrote {args.out}")


def write_markdown(rep):
    m, th = rep["test_metrics"], rep["thresholds"]
    rows = [("Any rule fires", m["any_rule_fired"]),
            ("Logistic regression baseline (p >= 0.5)", m["baseline_lr@0.5"]),
            (f"Gradient-boosted trees (p >= {th['review']})", m["gbm@review_threshold"]),
            ("**System: flagged (REVIEW or BLOCK)**", m["system_flagged"]),
            ("**System: auto-blocked**", m["system_blocked"])]
    t = lambda r: (f"{r['precision']:.3f} | {r['recall']:.3f} | {r['f1']:.3f} | {r['tp']} | "
                   f"{r['fp']} | {r['fn']}")
    lines = [
        "# Model evaluation report", "",
        f"Model `{rep['model_version']}`: trained on the first {rep['train_events']:,} "
        f"simulated transactions, evaluated on the next {rep['test_events']:,} "
        f"(time-based split, {rep['fraud_rate']:.2%} fraud).", "",
        "PR-AUC is the headline metric. With ~1% fraud, ROC-AUC looks excellent for almost any "
        "model and hides false positives.", "",
        f"* PR-AUC, logistic regression baseline: **{m['baseline_lr_pr_auc']}**",
        f"* PR-AUC, gradient-boosted trees: **{m['gbm_pr_auc']}** (ROC-AUC {m['gbm_roc_auc']})",
        f"* Decision thresholds: REVIEW at p >= {th['review']}, BLOCK at p >= {th['block']}",
        f"* False-positive rate (legit transactions flagged): "
        f"**{rep['false_positive_rate']:.4%}**", "",
        "| Detector | Precision | Recall | F1 | TP | FP | FN |", "|---|---|---|---|---|---|---|",
        *[f"| {n} | {t(r)} |" for n, r in rows],
        "", "## Rules measured on held-out data", "",
        "Only rules with high measured precision are allowed to escalate a decision "
        "(`action`); the rest are reason codes (`tag`).", "",
        "| Rule | Action | Precision | Recall | Fired on fraud | Fired on legit |",
        "|---|---|---|---|---|---|",
        *[f"| {n} | {r['action']} | {r['precision']:.3f} | {r['recall']:.3f} | {r['tp']} | {r['fp']} |"
          for n, r in rep["rules"].items()],
        "", "## Recall by fraud pattern (flagged = REVIEW or BLOCK)", "",
        "| Pattern | Caught | Total | Recall |", "|---|---|---|---|",
        *[f"| {k} | {v['caught']} | {v['total']} | {v['recall']:.3f} |"
          for k, v in rep["flagged_recall_by_fraud_type"].items()],
        "", "## Top features (GBM importance)", "",
        "| Feature | Importance |", "|---|---|",
        *[f"| {k} | {v} |" for k, v in list(rep["feature_importance"].items())[:8]], "",
    ]
    (ROOT / "ml/report.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
