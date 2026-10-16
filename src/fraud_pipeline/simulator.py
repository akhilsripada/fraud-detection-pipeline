"""Synthetic card-transaction stream with labeled fraud patterns.

Public fraud datasets use anonymized (PCA) features, which makes it impossible
to demo real-time feature engineering. Instead we simulate users with stable
habits and inject known attack patterns, so every transaction carries a
ground-truth label we can measure precision and recall against.

Fraud patterns
--------------
card_testing       a burst of tiny charges in under a minute (checking a stolen card)
impossible_travel  a purchase thousands of km away minutes after a normal one
amount_spike       one purchase 15-60x the user's normal spend
account_takeover   new device + new country + elevated amount

Legitimate "noise" is injected too (real trips, occasional big purchases,
new phones) so the detector has to separate fraud from unusual-but-genuine
behaviour rather than just flagging anything odd.
"""
from __future__ import annotations

import heapq
import itertools
import math
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Iterator

# name, country, lat, lon
CITIES = [
    ("Tampa", "US", 27.95, -82.46), ("New York", "US", 40.71, -74.01),
    ("Chicago", "US", 41.88, -87.63), ("Seattle", "US", 47.61, -122.33),
    ("Austin", "US", 30.27, -97.74), ("San Francisco", "US", 37.77, -122.42),
    ("Atlanta", "US", 33.75, -84.39), ("Denver", "US", 39.74, -104.99),
    ("Toronto", "CA", 43.65, -79.38), ("Mexico City", "MX", 19.43, -99.13),
    ("London", "GB", 51.51, -0.13), ("Paris", "FR", 48.86, 2.35),
    ("Berlin", "DE", 52.52, 13.40), ("Lagos", "NG", 6.52, 3.38),
    ("Mumbai", "IN", 19.08, 72.88), ("Hyderabad", "IN", 17.39, 78.49),
    ("Singapore", "SG", 1.35, 103.82), ("Tokyo", "JP", 35.68, 139.69),
    ("Sydney", "AU", -33.87, 151.21), ("Sao Paulo", "BR", -23.55, -46.63),
    ("Moscow", "RU", 55.76, 37.62), ("Bucharest", "RO", 44.43, 26.10),
]
HOME_CITIES = [c for c in CITIES if c[1] == "US"]

CATEGORIES = ["grocery", "restaurant", "gas", "pharmacy", "retail", "travel",
              "electronics", "jewelry", "digital_goods", "gift_cards"]
EVERYDAY = ["grocery", "restaurant", "gas", "pharmacy", "retail"]
ATTACKS = ["card_testing", "impossible_travel", "amount_spike", "account_takeover"]


@dataclass
class User:
    user_id: str
    home: tuple
    mean_amount: float
    devices: list
    categories: list
    location: tuple = None  # current city (changes during legit trips)
    busy_until: float = 0.0  # in transit: no purchases until we "land"

    def __post_init__(self):
        self.location = self.home


@dataclass(order=True)
class _Scheduled:
    ts: float
    seq: int
    txn: dict = field(compare=False)


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class TransactionSimulator:
    def __init__(self, n_users: int = 20000, tps: float = 200.0,
                 fraud_rate: float = 0.004, seed: int | None = 42,
                 start_ts: float | None = None):
        self.rng = random.Random(seed)
        self.tps = tps
        self.fraud_rate = fraud_rate  # probability an event starts an attack
        self.now = start_ts if start_ts is not None else time.time()
        self._seq = itertools.count()
        self._queue: list[_Scheduled] = []
        self.users = [self._make_user(i) for i in range(n_users)]

    # ---------- users ----------
    def _make_user(self, i: int) -> User:
        rng = self.rng
        return User(
            user_id=f"u{i:05d}",
            home=rng.choice(HOME_CITIES),
            mean_amount=round(math.exp(rng.gauss(3.4, 0.6)), 2),  # ~$30 median
            devices=[self._device()],
            categories=rng.sample(EVERYDAY, 3) + [rng.choice(CATEGORIES)],
        )

    def _device(self) -> str:
        return "d-" + uuid.UUID(int=self.rng.getrandbits(128)).hex[:10]

    # ---------- txn helpers ----------
    def _txn(self, user: User, ts: float, *, city=None, amount=None, device=None,
             category=None, label=0, fraud_type=None) -> dict:
        rng = self.rng
        name, country, lat, lon = city or user.location
        if amount is None:
            amount = max(0.5, rng.lognormvariate(math.log(user.mean_amount), 0.5))
        return {
            "txn_id": uuid.UUID(int=rng.getrandbits(128)).hex,
            "user_id": user.user_id,
            "ts": round(ts, 3),
            "amount": round(amount, 2),
            "currency": "USD",
            "merchant_category": category or rng.choice(user.categories),
            "merchant_id": f"m{rng.randrange(5000):04d}",
            "city": name,
            "country": country,
            "lat": round(lat + rng.uniform(-0.08, 0.08), 4),
            "lon": round(lon + rng.uniform(-0.08, 0.08), 4),
            "device_id": device or rng.choice(user.devices),
            # ground truth -- never used by the scorer, only for evaluation
            "label": label,
            "fraud_type": fraud_type,
        }

    def _schedule(self, txn: dict):
        heapq.heappush(self._queue, _Scheduled(txn["ts"], next(self._seq), txn))

    def _far_city(self, user: User):
        options = [c for c in CITIES
                   if haversine_km(c[2], c[3], user.location[2], user.location[3]) > 3000]
        return self.rng.choice(options)

    # ---------- event generation ----------
    def _spawn(self, ts: float):
        rng, user = self.rng, self.rng.choice(self.users)
        r = rng.random()

        if r < self.fraud_rate:
            self._spawn_attack(user, ts)
            return

        # legitimate noise (unusual but genuine)
        if ts < user.busy_until:                   # user is on a plane
            return
        if r < self.fraud_rate + 0.0015:           # start or end a real trip
            src = user.location
            dst = (rng.choice([c for c in CITIES if c != user.home])
                   if user.location == user.home else user.home)
            # flight time at ~800 km/h plus airport time: genuine travel is never "impossible"
            hours = haversine_km(src[2], src[3], dst[2], dst[3]) / 800 + rng.uniform(1.5, 4)
            user.location, user.busy_until = dst, ts + hours * 3600
            self._schedule(self._txn(user, user.busy_until, category="travel"))
            return
        if r < self.fraud_rate + 0.0040:           # occasional big legit buy
            self._schedule(self._txn(user, ts, amount=user.mean_amount * rng.uniform(3, 18),
                                     category=rng.choice(["electronics", "travel", "jewelry",
                                                          "retail"])))
            return
        if r < self.fraud_rate + 0.0050:           # new phone, same place
            user.devices.append(self._device())
            self._schedule(self._txn(user, ts, device=user.devices[-1]))
            return
        if r < self.fraud_rate + 0.0065:           # a few quick legit buys (mall trip)
            for k in range(rng.randint(2, 5)):
                self._schedule(self._txn(user, ts + k * rng.uniform(10, 90)))
            return
        if r < self.fraud_rate + 0.0080:           # app-store / transit micro-purchases
            for k in range(rng.randint(2, 6)):
                self._schedule(self._txn(user, ts + k * rng.uniform(5, 120),
                                         amount=rng.uniform(0.99, 6), category="digital_goods"))
            return
        if r < self.fraud_rate + 0.0085:           # gift for family abroad, bought online
            self._schedule(self._txn(user, ts, amount=user.mean_amount * rng.uniform(2, 8),
                                     category="gift_cards"))
            return
        if r < self.fraud_rate + 0.0088 and user.location != user.home:  # local SIM / borrowed phone abroad
            user.devices.append(self._device())
            self._schedule(self._txn(user, ts, device=user.devices[-1]))
            return

        self._schedule(self._txn(user, ts))

    def inject_attack(self, kind: str | None = None) -> str:
        """Start an attack on a random user right now (used by the demo's buttons)."""
        user = self.rng.choice(self.users)
        self._spawn_attack(user, self.now, kind)
        return user.user_id

    def _spawn_attack(self, user: User, ts: float, kind: str | None = None):
        """Attacks vary in how blatant they are, so detection is not trivial."""
        rng = self.rng
        kind = kind or rng.choice(ATTACKS)
        if kind not in ATTACKS:
            raise ValueError(f"unknown attack {kind!r}")
        if kind == "card_testing":
            dev = self._device() if rng.random() < 0.7 else rng.choice(user.devices)
            t = ts
            for _ in range(rng.randint(3, 10)):
                t += rng.uniform(3, 75)
                self._schedule(self._txn(user, t, amount=rng.uniform(0.5, 9.99), device=dev,
                                         category=rng.choice(["digital_goods", "gift_cards",
                                                              "retail"]),
                                         label=1, fraud_type=kind))
        elif kind == "impossible_travel":
            self._schedule(self._txn(user, ts))  # genuine purchase at home
            far = self._far_city(user) if rng.random() < 0.6 else rng.choice(
                [c for c in HOME_CITIES if haversine_km(c[2], c[3], user.location[2],
                                                        user.location[3]) > 1200])
            self._schedule(self._txn(user, ts + rng.uniform(300, 5400), city=far,
                                     amount=user.mean_amount * rng.uniform(0.6, 5),
                                     device=rng.choice(user.devices),  # cloned card, POS
                                     label=1, fraud_type=kind))
        elif kind == "amount_spike":
            self._schedule(self._txn(user, ts, amount=user.mean_amount * rng.uniform(5, 40),
                                     category=rng.choice(["electronics", "jewelry", "gift_cards",
                                                          "retail"]),
                                     label=1, fraud_type=kind))
        else:  # account_takeover: sometimes from abroad, sometimes a domestic proxy
            city = self._far_city(user) if rng.random() < 0.5 else rng.choice(HOME_CITIES)
            dev = self._device()
            t = ts
            for _ in range(rng.randint(1, 3)):
                t += rng.uniform(30, 900)
                self._schedule(self._txn(user, t, city=city, device=dev,
                                         amount=user.mean_amount * rng.uniform(1.5, 12),
                                         category=rng.choice(["gift_cards", "electronics",
                                                              "retail"]),
                                         label=1, fraud_type=kind))

    # ---------- public API ----------
    def stream(self, max_events: int | None = None) -> Iterator[dict]:
        """Yield transactions in timestamp order on a simulated clock (fast)."""
        emitted = 0
        while max_events is None or emitted < max_events:
            self.now += self.rng.expovariate(self.tps)
            self._spawn(self.now)
            while self._queue and self._queue[0].ts <= self.now:
                yield heapq.heappop(self._queue).txn
                emitted += 1
                if max_events is not None and emitted >= max_events:
                    return

    def realtime(self, max_events: int | None = None) -> Iterator[dict]:
        """Same stream, paced to wall-clock time and stamped with real timestamps."""
        offset = time.time() - self.now
        for txn in self.stream(max_events):
            wait = txn["ts"] + offset - time.time()
            if wait > 0:
                time.sleep(wait)
            txn["ts"] = round(time.time(), 3)
            yield txn


if __name__ == "__main__":
    import json, sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    for t in TransactionSimulator(tps=50, fraud_rate=0.05).stream(n):
        print(json.dumps(t))
