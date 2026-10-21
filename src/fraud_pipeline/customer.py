"""The cardholder's side of an alert: "Was this you?"

When a transaction is blocked or held, the cardholder gets an alert with
exactly where and when it happened, the card's last few purchases leading up
to it, and two buttons:

* "No, freeze my card": every later purchase on the card is blocked, and
  the alert is recorded as confirmed fraud (label 1).
* "Yes, it was me": the hold is released and the alert is recorded as a
  false alarm (label 0).

Both answers become labelled training data (`/api/feedback`), which closes
the loop: the people who know best whether a charge was fraud are the ones
who made it, or didn't.
"""
from __future__ import annotations

import json
import random
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from .simulator import haversine_km

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

TIMEZONES = {
    "Tampa": "America/New_York", "New York": "America/New_York", "Atlanta": "America/New_York",
    "Chicago": "America/Chicago", "Austin": "America/Chicago", "Denver": "America/Denver",
    "Seattle": "America/Los_Angeles", "San Francisco": "America/Los_Angeles", "Toronto": "America/Toronto",
    "Mexico City": "America/Mexico_City", "London": "Europe/London", "Paris": "Europe/Paris",
    "Berlin": "Europe/Berlin", "Bucharest": "Europe/Bucharest", "Moscow": "Europe/Moscow",
    "Lagos": "Africa/Lagos", "Mumbai": "Asia/Kolkata", "Hyderabad": "Asia/Kolkata", "Singapore": "Asia/Singapore",
    "Tokyo": "Asia/Tokyo", "Sydney": "Australia/Sydney", "Sao Paulo": "America/Sao_Paulo",
    "São Paulo": "America/Sao_Paulo",
}

_NAMES = {
    "grocery": ["Fresh Market", "Corner Grocer", "Green Basket", "Daily Foods"],
    "restaurant": ["Harbor Grill", "Noodle House", "Blue Door Kitchen", "Taco Social"],
    "gas": ["Metro Fuel", "QuickStop Gas", "Highway 9 Fuel"],
    "pharmacy": ["CityCare Pharmacy", "Wellness Rx", "Main St Pharmacy"],
    "retail": ["Urban Outfitters Co.", "Northside Mall Store", "Style Depot", "Home Goods Plus"],
    "travel": ["SkyWay Travel", "Grand Central Hotel", "Airport Duty Free"],
    "electronics": ["TechHub Electronics", "Circuit Point", "Gadget Galaxy", "Pixel & Port"],
    "jewelry": ["Golden Hour Jewelers", "Facet Fine Jewelry", "Lumen Diamonds"],
    "digital_goods": ["AppVault", "PixelPlay Store", "StreamBox Credits"],
    "gift_cards": ["GiftCard Express", "Card Kiosk 24", "eGift Center"],
}


def merchant(merchant_id: str, category: str, city: str) -> str:
    """A stable, made-up merchant name for a simulated merchant id."""
    names = _NAMES.get(category, ["Store"])
    return f"{names[random.Random(merchant_id + category).randrange(len(names))]}, {city}"


def local_time(ts: float, city: str) -> dict:
    """The purchase time in the merchant's own time zone, and in UTC."""
    utc = datetime.fromtimestamp(ts, timezone.utc)
    tz_name = TIMEZONES.get(city)
    if ZoneInfo and tz_name:
        try:
            local = utc.astimezone(ZoneInfo(tz_name))
            return {"local": local.strftime("%a %d %b, %I:%M %p").replace(" 0", " "), "tz": local.tzname(),
                    "utc": utc.strftime("%Y-%m-%d %H:%M:%S UTC")}
        except Exception:
            pass
    return {"local": utc.strftime("%a %d %b, %I:%M %p UTC").replace(" 0", " "), "tz": "UTC",
            "utc": utc.strftime("%Y-%m-%d %H:%M:%S UTC")}


SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
  txn_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, verdict TEXT NOT NULL, label INTEGER NOT NULL,
  answered_at REAL NOT NULL, model_decision TEXT, risk_score REAL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS frozen_cards (user_id TEXT PRIMARY KEY, frozen_at REAL NOT NULL, txn_id TEXT);
"""


class CustomerDesk:
    """Card history, frozen cards and cardholder answers. Thread-safe; persists to the alert store's SQLite."""

    HISTORY = 8

    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock
        self.lock = threading.Lock()
        self.history: dict[str, deque] = defaultdict(lambda: deque(maxlen=self.HISTORY))
        with store.lock, store.db:
            store.db.executescript(SCHEMA)
        self.frozen = {r[0] for r in store.db.execute("SELECT user_id FROM frozen_cards")}

    # ------------------------------------------------------------------ stream side
    def record(self, txn: dict, decision: str):
        """Remember a transaction for the card's timeline (called for every scored transaction)."""
        with self.lock:
            self.history[txn["user_id"]].append({
                "txn_id": txn["txn_id"], "ts": float(txn["ts"]), "amount": float(txn["amount"]),
                "city": txn.get("city"), "country": txn.get("country"), "lat": float(txn["lat"]),
                "lon": float(txn["lon"]), "merchant": merchant(txn.get("merchant_id", ""), txn.get("merchant_category", ""),
                                                               txn.get("city", "")), "decision": decision})

    def is_frozen(self, user_id: str) -> bool:
        return user_id in self.frozen

    # ------------------------------------------------------------------ cardholder side
    def context(self, alert: dict) -> dict:
        """Everything the cardholder's alert screen needs: where, when, and what came before."""
        uid, ts = alert["user_id"], float(alert["ts"])
        with self.lock:
            before = [h for h in self.history.get(uid, []) if h["ts"] < ts and h["txn_id"] != alert["txn_id"]][-5:]
        timeline, prev = [], None
        for h in before + [{"txn_id": alert["txn_id"], "ts": ts, "amount": alert["amount"], "city": alert.get("city"),
                            "country": alert.get("country"), "lat": alert["lat"], "lon": alert["lon"],
                            "merchant": self._merchant(alert), "decision": alert["decision"], "this": True}]:
            row = {**h, "when": local_time(h["ts"], h["city"] or "")}
            if prev:
                km = haversine_km(prev["lat"], prev["lon"], h["lat"], h["lon"])
                minutes = max((h["ts"] - prev["ts"]) / 60, 0.0)
                row["gap"] = {"km": round(km), "minutes": round(minutes, 1),
                              "kmh": round(km / max(minutes / 60, 1 / 120)) if km > 1 else 0}
            timeline.append(row)
            prev = h
        answer = self._answer(alert["txn_id"])
        return {
            "alert": {k: alert.get(k) for k in ("txn_id", "user_id", "ts", "amount", "city", "country", "lat", "lon",
                                                 "merchant_category", "decision", "risk_score", "rules_fired")},
            "merchant": {"name": self._merchant(alert), "category": alert.get("merchant_category"),
                         "lat": alert["lat"], "lon": alert["lon"], "city": alert.get("city"), "country": alert.get("country")},
            "when": local_time(ts, alert.get("city") or ""),
            "timeline": timeline,
            "card": {"frozen": uid in self.frozen},
            "answer": answer,
        }

    def _merchant(self, alert: dict) -> str:
        return alert.get("merchant") or merchant(alert.get("merchant_id", alert["txn_id"][:6]),
                                                alert.get("merchant_category", ""), alert.get("city", ""))

    def _answer(self, txn_id: str) -> dict | None:
        with self.store.lock:
            row = self.store.db.execute("SELECT verdict, answered_at FROM feedback WHERE txn_id = ?", (txn_id,)).fetchone()
        return {"verdict": row[0], "answered_at": row[1]} if row else None

    def answer(self, alert: dict, verdict: str) -> dict:
        """Record the cardholder's answer. Idempotent: answering twice keeps the first answer."""
        if verdict not in ("mine", "not_mine"):
            raise ValueError("verdict must be 'mine' or 'not_mine'")
        existing = self._answer(alert["txn_id"])
        if existing:
            return {"status": "already_answered", **existing, "frozen": alert["user_id"] in self.frozen}
        now = self.clock()
        label = 1 if verdict == "not_mine" else 0
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?)",
                                  (alert["txn_id"], alert["user_id"], verdict, label, now, alert.get("decision"),
                                   alert.get("risk_score"), json.dumps(alert, separators=(",", ":"))))
            if verdict == "not_mine":
                self.store.db.execute("INSERT OR IGNORE INTO frozen_cards VALUES (?,?,?)",
                                      (alert["user_id"], now, alert["txn_id"]))
        if verdict == "not_mine":
            self.frozen.add(alert["user_id"])
        return {"status": "recorded", "verdict": verdict, "label": label, "frozen": alert["user_id"] in self.frozen}

    def unfreeze(self, user_id: str):
        with self.store.lock, self.store.db:
            self.store.db.execute("DELETE FROM frozen_cards WHERE user_id = ?", (user_id,))
        self.frozen.discard(user_id)

    def feedback(self, limit: int = 500) -> list[dict]:
        """Labelled examples from cardholders, newest first, for retraining and for measuring live precision."""
        with self.store.lock:
            rows = self.store.db.execute("SELECT txn_id, user_id, verdict, label, answered_at, model_decision, risk_score "
                                         "FROM feedback ORDER BY answered_at DESC LIMIT ?", (limit,)).fetchall()
        keys = ("txn_id", "user_id", "verdict", "label", "answered_at", "model_decision", "risk_score")
        return [dict(zip(keys, r)) for r in rows]


class PushNotifier:
    """Optional phone push for blocked transactions via ntfy (free, no account; install the ntfy app).

        python run.py --notify my-secret-topic --public-url http://192.168.1.20:8000

    Each BLOCK alert sends "Was this you?" with a tap-through link to the cardholder's alert page.
    At most one push every `min_gap` seconds, so a demo stream doesn't flood the phone.
    """

    def __init__(self, topic: str, public_url: str, server: str = "https://ntfy.sh", min_gap: float = 20.0,
                 opener=None, clock=time.monotonic):
        import urllib.request
        self.topic, self.public_url, self.server, self.min_gap = topic, public_url.rstrip("/"), server.rstrip("/"), min_gap
        self.opener, self.clock, self.last = opener or urllib.request.urlopen, clock, -1e9
        self.sent = 0

    def notify(self, alert: dict) -> bool:
        if alert["decision"] != "BLOCK" or self.clock() - self.last < self.min_gap:
            return False
        import urllib.request
        self.last = self.clock()
        body = (f"${alert['amount']:,.2f} in {alert.get('city')}, {alert.get('country')} was blocked on your card. "
                "Was this you?").encode()
        req = urllib.request.Request(f"{self.server}/{self.topic}", data=body, method="POST", headers={
            "Title": "Sentinel: purchase blocked", "Tags": "warning,credit_card",
            "Click": f"{self.public_url}/customer/{alert['txn_id']}"})
        try:
            with self.opener(req, timeout=5):
                pass
            self.sent += 1
            return True
        except Exception:
            return False
