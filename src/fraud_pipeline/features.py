"""Real-time, per-user feature computation.

Features are computed from the user's *prior* state, then the state is
updated with the current transaction. The same code runs in training
(ml/train.py) and serving (Lambda / local demo), so there is no
train/serve skew.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

from .simulator import haversine_km

WINDOW_SECONDS = 3600          # keep 1h of timestamps for velocity windows
MAX_RECENT_IDS = 64            # dedupe window for redelivered records
MAX_DEVICES = 20
MAX_COUNTRIES = 20
MIN_HISTORY_FOR_STATS = 5      # need some history before z-scores mean anything

CATEGORY_RISK = {
    "grocery": 0.05, "pharmacy": 0.05, "gas": 0.1, "restaurant": 0.1,
    "retail": 0.3, "travel": 0.4, "electronics": 0.7, "jewelry": 0.8,
    "digital_goods": 0.8, "gift_cards": 0.9,
}

# Order matters: the model's coefficients are aligned to this list.
MODEL_FEATURES = [
    "log_amount", "amount_zscore", "log_amount_ratio",
    "velocity_1m", "velocity_10m", "log_velocity_1h",
    "log_speed_kmh", "log_km_from_last", "is_new_device", "is_new_country",
    "is_foreign", "category_risk", "is_micro_amount", "log_seconds_since_last",
    "has_history",
]


@dataclass
class UserState:
    user_id: str
    recent_ts: list = field(default_factory=list)      # timestamps within WINDOW_SECONDS
    recent_ids: list = field(default_factory=list)     # last N txn_ids (idempotency)
    n: int = 0                                          # Welford running stats on amount
    mean: float = 0.0
    m2: float = 0.0
    last_ts: float | None = None
    last_lat: float | None = None
    last_lon: float | None = None
    home_country: str | None = None
    devices: list = field(default_factory=list)
    countries: list = field(default_factory=list)
    version: int = 0                                    # optimistic locking in DynamoDB

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "UserState":
        return cls(**d)

    @property
    def std(self) -> float:
        return math.sqrt(self.m2 / (self.n - 1)) if self.n > 1 else 0.0

    def has_seen(self, txn_id: str) -> bool:
        return txn_id in self.recent_ids


def compute_features(txn: dict, state: UserState) -> dict:
    ts, amount = float(txn["ts"]), float(txn["amount"])
    window = [t for t in state.recent_ts if ts - t <= WINDOW_SECONDS]

    f = {
        "amount": amount,
        "log_amount": math.log1p(amount),
        # +1 counts the current transaction
        "velocity_1m": 1 + sum(1 for t in window if ts - t <= 60),
        "velocity_10m": 1 + sum(1 for t in window if ts - t <= 600),
        "velocity_1h": 1 + len(window),
        "category_risk": CATEGORY_RISK.get(txn.get("merchant_category"), 0.5),
        "is_micro_amount": 1.0 if amount < 5 else 0.0,
        "has_history": 1.0 if state.n >= MIN_HISTORY_FOR_STATS else 0.0,
    }

    if state.n >= MIN_HISTORY_FOR_STATS:
        std = max(state.std, 0.25 * state.mean, 1.0)
        f["amount_zscore"] = (amount - state.mean) / std
        f["amount_ratio"] = amount / max(state.mean, 1.0)
    else:
        f["amount_zscore"], f["amount_ratio"] = 0.0, 1.0

    if state.last_ts is not None:
        km = haversine_km(state.last_lat, state.last_lon, float(txn["lat"]), float(txn["lon"]))
        dt = max(ts - state.last_ts, 30.0)  # floor avoids divide-by-~0 on bursts
        f["km_from_last"] = km
        f["speed_kmh"] = km / (dt / 3600)
        f["seconds_since_last"] = ts - state.last_ts
    else:
        f["km_from_last"] = f["speed_kmh"] = 0.0
        f["seconds_since_last"] = 86400.0

    seen = state.n > 0
    f["is_new_device"] = 1.0 if seen and txn.get("device_id") not in state.devices else 0.0
    f["is_new_country"] = 1.0 if seen and txn.get("country") not in state.countries else 0.0
    f["is_foreign"] = 1.0 if seen and txn.get("country") != state.home_country else 0.0

    # log transforms tame heavy tails for the linear model
    f["log_amount_ratio"] = math.log(max(f["amount_ratio"], 1e-3))
    f["log_velocity_1h"] = math.log1p(f["velocity_1h"])
    f["log_speed_kmh"] = math.log1p(f["speed_kmh"])
    f["log_km_from_last"] = math.log1p(f["km_from_last"])
    f["log_seconds_since_last"] = math.log1p(max(f["seconds_since_last"], 0.0))
    return f


def update_state(state: UserState, txn: dict, trusted: bool = True) -> UserState:
    """Apply txn to the user's state (mutates and returns `state`).

    Velocity always counts every attempt. Blocked (untrusted) transactions do
    NOT update the spending profile, devices, countries or last location --
    otherwise a fraudster's first blocked charge would "teach" the system that
    their device and country are normal.
    """
    ts, amount = float(txn["ts"]), float(txn["amount"])
    state.recent_ts = [t for t in state.recent_ts if ts - t <= WINDOW_SECONDS] + [ts]
    state.recent_ids = (state.recent_ids + [txn["txn_id"]])[-MAX_RECENT_IDS:]
    if not trusted:
        return state

    # Welford's online mean/variance -- O(1) memory per user
    state.n += 1
    delta = amount - state.mean
    state.mean += delta / state.n
    state.m2 += delta * (amount - state.mean)

    state.last_ts, state.last_lat, state.last_lon = ts, float(txn["lat"]), float(txn["lon"])
    if state.home_country is None:
        state.home_country = txn.get("country")
    if txn.get("device_id") not in state.devices:
        state.devices = (state.devices + [txn.get("device_id")])[-MAX_DEVICES:]
    if txn.get("country") not in state.countries:
        state.countries = (state.countries + [txn.get("country")])[-MAX_COUNTRIES:]
    return state


def model_vector(features: dict) -> list[float]:
    return [float(features[name]) for name in MODEL_FEATURES]
