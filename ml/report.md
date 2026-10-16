# Model evaluation report

Model `gbm-20261016-153647`: trained on the first 420,000 simulated transactions, evaluated on the next 180,000 (time-based split, 0.90% fraud).

PR-AUC is the headline metric. With ~1% fraud, ROC-AUC looks excellent for almost any model and hides false positives.

* PR-AUC, logistic regression baseline: **0.802**
* PR-AUC, gradient-boosted trees: **0.9376** (ROC-AUC 0.9984)
* Decision thresholds: REVIEW at p >= 0.26, BLOCK at p >= 0.31
* False-positive rate (legit transactions flagged): **0.0578%**

| Detector | Precision | Recall | F1 | TP | FP | FN |
|---|---|---|---|---|---|---|
| Any rule fires | 0.242 | 0.663 | 0.355 | 1217 | 3810 | 618 |
| Logistic regression baseline (p >= 0.5) | 0.323 | 0.955 | 0.483 | 1753 | 3671 | 82 |
| Gradient-boosted trees (p >= 0.26) | 0.942 | 0.873 | 0.906 | 1601 | 99 | 234 |
| **System: flagged (REVIEW or BLOCK)** | 0.940 | 0.875 | 0.906 | 1606 | 103 | 229 |
| **System: auto-blocked** | 0.950 | 0.864 | 0.905 | 1586 | 84 | 249 |

## Rules measured on held-out data

Only rules with high measured precision are allowed to escalate a decision (`action`); the rest are reason codes (`tag`).

| Rule | Action | Precision | Recall | Fired on fraud | Fired on legit |
|---|---|---|---|---|---|
| high_velocity_1m | tag | 0.080 | 0.034 | 62 | 715 |
| card_testing | tag | 0.169 | 0.307 | 564 | 2769 |
| impossible_travel | block | 0.987 | 0.245 | 450 | 6 |
| amount_spike | tag | 0.523 | 0.076 | 139 | 127 |
| new_device_new_country | block | 1.000 | 0.098 | 180 | 0 |
| risky_category_high_amount | tag | 0.507 | 0.164 | 300 | 292 |

## Recall by fraud pattern (flagged = REVIEW or BLOCK)

| Pattern | Caught | Total | Recall |
|---|---|---|---|
| account_takeover | 372 | 374 | 0.995 |
| amount_spike | 138 | 190 | 0.726 |
| card_testing | 1023 | 1198 | 0.854 |
| impossible_travel | 73 | 73 | 1.000 |

## Top features (GBM importance)

| Feature | Importance |
|---|---|
| is_new_device | 0.4671 |
| category_risk | 0.1951 |
| log_amount_ratio | 0.1159 |
| log_amount | 0.09 |
| amount_zscore | 0.0313 |
| log_km_from_last | 0.0234 |
| log_seconds_since_last | 0.0228 |
| log_speed_kmh | 0.0217 |
