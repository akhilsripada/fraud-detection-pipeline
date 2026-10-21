"""Sentinel assistant: answers plain-English questions about the live stream.

Design rule: **the language model routes, the code answers.**

1. A fast rule-based parser turns the question into an intent
   ("explain u01037", "summary", "show Europe", "simulate an attack").
2. Only when the rules don't recognise a question, and an LLM is configured,
   the LLM is asked to pick one of those same intents. It never writes the
   answer itself.
3. Every answer is built from the decision's real features, so an explanation
   can't contain a number the model didn't compute.

That keeps answers instant and free for the common case, and makes it
impossible for a model to invent a fact about a customer's transaction.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from collections import Counter

log = logging.getLogger(__name__)

ATTACKS = ["card_testing", "impossible_travel", "amount_spike", "account_takeover"]
NICE = {
    "card_testing": "card testing", "impossible_travel": "impossible travel", "amount_spike": "amount spike",
    "account_takeover": "account takeover", "new_device_new_country": "new device in a new country",
    "high_velocity_1m": "burst of charges", "risky_category_high_amount": "large resellable purchase",
}
REGIONS = {
    "europe": {"label": "Europe", "countries": {"GB", "FR", "DE", "RO", "RU"}},
    "asia": {"label": "Asia", "countries": {"IN", "SG", "JP"}},
    "africa": {"label": "Africa", "countries": {"NG"}},
    "oceania": {"label": "Australia", "countries": {"AU"}},
    "southamerica": {"label": "South America", "countries": {"BR"}},
    "northamerica": {"label": "North America", "countries": {"US", "CA", "MX"}},
}
_REGION_WORDS = [
    ("europe", r"\b(europe|european|uk|britain|london|paris|berlin|germany|france|romania|russia|moscow)\b"),
    ("asia", r"\b(asia|asian|india|mumbai|hyderabad|japan|tokyo|singapore)\b"),
    ("africa", r"\b(africa|african|nigeria|lagos)\b"),
    ("oceania", r"\b(australia|sydney|oceania)\b"),
    ("southamerica", r"\b(south america|latin america|brazil|sao paulo)\b"),
    ("northamerica", r"\b(north america|usa|us|america|united states|canada|mexico|states)\b"),
]


# ----------------------------------------------------------------------------- parsing

def _norm(text: str) -> str:
    t = text.lower().replace("’", "'")
    t = re.sub(r"[^a-z0-9%'\-\s.]", " ", t)
    return " " + re.sub(r"\s+", " ", t).strip() + " "


def _attack_kind(t: str) -> str | None:
    if re.search(r"card.?test|testing|micro|tiny|small charges", t):
        return "card_testing"
    if re.search(r"travel|teleport|impossible|location|abroad", t):
        return "impossible_travel"
    if re.search(r"spike|large|big|huge|expensive|high amount", t):
        return "amount_spike"
    if re.search(r"takeover|\bato\b|hack|log ?in|account|stolen login", t):
        return "account_takeover"
    return None


def parse_intent(text: str) -> dict:
    """Rule-based intent parser. Returns {"intent": ..., ...}; "unknown" if nothing matched."""
    t = _norm(text)
    user = (re.search(r"\bu\d{1,5}\b", t) or [None])[0]
    if re.search(r"\b(simulate|inject|trigger|launch|start|run|fire|send)\b", t) or re.search(r"\battack\b", t):
        return {"intent": "attack", "kind": _attack_kind(t)}
    if re.search(r"\b(why|explain|reason|what happened|tell me about)\b", t) or user:
        return {"intent": "explain", "user": user}
    if re.search(r"\b(report|summary|summari[sz]e|today|so far|what changed|overview|how many|stats|statistics|"
                 r"going on|status|overall|numbers|totals?)\b", t):
        return {"intent": "report"}
    if re.search(r"\b(clear|reset|show all|everything|all regions|whole world|everywhere)\b", t):
        return {"intent": "clear"}
    for region, pattern in _REGION_WORDS:
        if re.search(pattern, t):
            return {"intent": "region", "region": region}
    if re.search(r"\b(latest|last|recent|newest|most recent|blocked|flagged|declined|denied|stopped|suspicious|fraudulent|"
                 r"alert|alerts|transaction|transactions|purchase|charge|where|when)\b", t):
        return {"intent": "explain", "user": None}  # "tell me the latest blocked transaction", "what got flagged?"
    return {"intent": "unknown"}


# ----------------------------------------------------------------------------- optional LLM routing

INTENT_SCHEMA_PROMPT = """You route questions for a fraud-monitoring dashboard.
Reply with ONLY one JSON object, no prose. Allowed shapes:
{"intent":"explain","user":"u01037" or null}
{"intent":"attack","kind":"card_testing"|"impossible_travel"|"amount_spike"|"account_takeover"|null}
{"intent":"region","region":"europe"|"asia"|"africa"|"oceania"|"southamerica"|"northamerica"}
{"intent":"report"}
{"intent":"clear"}
{"intent":"unknown"}
Question: """


class GeminiRouter:
    """Maps free-form questions to an intent with Google's Gemini API (free tier works).

    Enabled only when GEMINI_API_KEY is set. Results are cached per question,
    so repeated questions cost nothing, and any failure falls back to "unknown".
    """

    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

    def __init__(self, api_key: str, model: str = "gemini-flash-latest", timeout: float = 6.0, opener=None):
        self.api_key, self.model, self.timeout = api_key, model, timeout
        self.opener = opener or urllib.request.urlopen
        self.cache: dict[str, dict] = {}

    @classmethod
    def from_env(cls) -> "GeminiRouter | None":
        key = os.environ.get("GEMINI_API_KEY")
        return cls(key, os.environ.get("GEMINI_MODEL", "gemini-flash-latest")) if key else None

    def route(self, text: str) -> dict:
        key = _norm(text)
        if key in self.cache:
            return self.cache[key]
        body = json.dumps({"contents": [{"parts": [{"text": INTENT_SCHEMA_PROMPT + text[:500]}]}],
                           "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}).encode()
        req = urllib.request.Request(self.URL.format(model=self.model, key=self.api_key), data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
            intent = _validate(json.loads(raw))
        except Exception as e:  # network, quota, malformed output: never break the dashboard
            log.warning("LLM routing failed: %s", e)
            intent = {"intent": "unknown"}
        self.cache[key] = intent
        return intent


def _validate(d: dict) -> dict:
    """Only accept intents and values the code knows how to answer."""
    i = d.get("intent")
    if i == "explain":
        user = d.get("user")
        return {"intent": "explain", "user": user if isinstance(user, str) and re.fullmatch(r"u\d{1,5}", user) else None}
    if i == "attack":
        return {"intent": "attack", "kind": d.get("kind") if d.get("kind") in ATTACKS else None}
    if i == "region" and d.get("region") in REGIONS:
        return {"intent": "region", "region": d["region"]}
    if i in ("report", "clear"):
        return {"intent": i}
    return {"intent": "unknown"}


# ----------------------------------------------------------------------------- answers

def _money(x: float) -> str:
    return f"${x:,.2f}"


def explain(alert: dict) -> dict:
    """Plain-English explanation built only from the decision's own features."""
    f = alert.get("features", {})
    amount = float(alert["amount"])
    ratio = float(f.get("amount_ratio", 1) or 1)
    usual = amount / ratio if ratio > 0 else amount
    reasons = []
    fired = set(alert.get("rules_fired", []))
    mins = float(f.get("seconds_since_last", 0)) / 60
    if "impossible_travel" in fired:
        reasons.append(f"{max(1, round(mins))} minutes after this card's previous purchase, it was used "
                       f"{f.get('km_from_last', 0):,.0f} km away. Getting there in time means travelling at "
                       f"{f.get('speed_kmh', 0):,.0f} km/h; an airliner manages about 900.")
    if "new_device_new_country" in fired:
        reasons.append(f"It came from a device this cardholder has never used, in a country they have never "
                       f"bought from ({alert.get('country')}).")
    elif f.get("is_new_device"):
        reasons.append("It came from a device this cardholder has never used.")
    if f.get("velocity_10m", 0) >= 3 and amount < 10:
        reasons.append(f"It is charge number {int(f['velocity_10m'])} in ten minutes, each under $10, which is how "
                       "stolen card numbers get tested.")
    if ratio >= 4:
        reasons.append(f"It is {ratio:.1f}× this cardholder's usual spend of about {_money(usual)}.")
    if not reasons:
        reasons.append(f"The fraud model scored it {alert['risk_score'] * 100:.0f}/100 from the combination of "
                       "amount, merchant type, timing and device, even though no single rule fired.")
    blocked = alert["decision"] == "BLOCK"
    headline = (f"{'Blocked' if blocked else 'Sent for review'}: {_money(amount)} at a "
                f"{alert.get('merchant_category', '').replace('_', ' ')} merchant in {alert.get('city')}, "
                f"{alert.get('country')}. Cardholder {alert['user_id']}, risk {alert['risk_score'] * 100:.0f}/100.")
    next_step = (f'Text to the customer: "We blocked a {_money(amount)} charge in {alert.get("city")}. Reply YES if '
                 'this was you, or NO and we\'ll freeze the card."' if blocked else
                 "Next step: an analyst reviews it within 15 minutes. The card keeps working meanwhile.")
    return {"headline": headline, "reasons": reasons, "next_step": next_step}


class Assistant:
    """Answers questions against a source of recent alerts and running totals.

    `alerts()` returns recent alert dicts, newest first (the same shape the
    dashboard receives). `stats()` returns running totals. `inject(kind)`
    starts a simulated attack and returns the cardholder it targets.
    """

    def __init__(self, alerts, stats, inject=None, router: GeminiRouter | None = None):
        self.alerts, self.stats, self.inject, self.router = alerts, stats, inject, router

    def ask(self, text: str) -> dict:
        intent = parse_intent(text)
        routed_by = "rules"
        if intent["intent"] == "unknown" and self.router:
            intent, routed_by = self.router.route(text), "llm"
        answer = getattr(self, "_" + intent["intent"])(intent)
        return {"intent": intent, "routed_by": routed_by, **answer}

    def _explain(self, it):
        alerts = self.alerts()
        if it.get("user"):
            a = next((x for x in alerts if x["user_id"] == it["user"]), None)
            if not a:
                recent = ", ".join(dict.fromkeys(x["user_id"] for x in alerts[:5])) or "none yet"
                return {"text": f"{it['user']} has no flagged transactions. Recently flagged: {recent}."}
        else:
            a = next((x for x in alerts if x["decision"] == "BLOCK"), alerts[0] if alerts else None)
            if not a:
                return {"text": "Nothing has been flagged yet. Try “simulate an account takeover”."}
        e = explain(a)
        return {"text": e["headline"], "reasons": e["reasons"], "next_step": e["next_step"], "alert": a}

    def _attack(self, it):
        if not self.inject:
            return {"text": "This deployment doesn't allow simulated attacks."}
        kind = it.get("kind") or "account_takeover"
        user = self.inject(kind)
        return {"text": f"Launched {NICE[kind]} against cardholder {user}. Watch the alert feed.",
                "attack": {"kind": kind, "user": user}}

    def _region(self, it):
        r = REGIONS[it["region"]]
        alerts = self.alerts()
        hits = [a for a in alerts if a.get("country") in r["countries"]]
        if not hits:
            return {"text": f"No alerts in {r['label']} among the last {len(alerts)}.", "filter": it["region"]}
        blocked = sum(a["decision"] == "BLOCK" for a in hits)
        top = _top_reason(hits)
        return {"text": f"{len(hits)} of the last {len(alerts)} alerts were in {r['label']}: {blocked} blocked, "
                        f"{len(hits) - blocked} sent for review." + (f" Most common reason: {top}." if top else ""),
                "filter": it["region"]}

    def _report(self, _):
        s, alerts = self.stats(), self.alerts()
        rate = s["flagged"] / s["processed"] * 100 if s.get("processed") else 0
        top = _top_reason(alerts)
        cities = Counter(a.get("city") for a in alerts).most_common(1)
        truth = [a for a in alerts if a.get("label") is not None]
        reasons = [f"Most common reason: {top}." if top else "No rule-based reasons yet.",
                   f"Busiest alert location: {cities[0][0]} ({cities[0][1]} alerts)." if cities else ""]
        if truth:
            real = sum(a["label"] == 1 for a in truth)
            reasons.append(f"Of the last {len(truth)} alerts, {real} were real fraud by the simulator's ground truth.")
        return {"text": f"Scored {s.get('processed', 0):,} transactions: {s.get('flagged', 0):,} flagged "
                        f"({rate:.2f}%), {s.get('blocked', 0):,} blocked.", "reasons": [r for r in reasons if r]}

    def _clear(self, _):
        return {"text": "Showing all regions.", "filter": None}

    def _unknown(self, _):
        hint = "" if self.router else " (Set GEMINI_API_KEY to also understand free-form questions.)"
        return {"text": "I didn't catch that. Try “why was the last transaction blocked?”, “simulate impossible "
                        "travel”, “show me fraud in Asia” or “give me a summary”." + hint}


def _top_reason(alerts) -> str | None:
    c = Counter(r for a in alerts for r in a.get("rules_fired", []))
    return NICE.get(c.most_common(1)[0][0], c.most_common(1)[0][0]) if c else None
