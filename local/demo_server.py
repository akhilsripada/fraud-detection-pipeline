"""Run Swipestop on your laptop: live pipeline, dashboard, assistant and API. No AWS needed.

    python run.py                          # easiest: starts this and opens the browser
    python local/demo_server.py            # then open http://localhost:8000
    python local/demo_server.py --tps 200 --port 8080

HTTP API (used by the dashboard and by the MCP server in mcp/):
    POST /api/ask            {"text": "why was u01037 blocked?"} -> answer
    GET  /api/alerts         ?limit=20&user=u01037
    GET  /api/alerts/<id>    one alert with its features
    GET  /api/stats          running totals
    POST /api/attack?type=   inject a simulated attack

Cardholder side ("was this you?"):
    GET  /customer/<id>                 the cardholder's alert page (where, when, AR "show me where")
    GET  /api/alerts/<id>/context       merchant, local time, the card's recent purchases
    POST /api/alerts/<id>/feedback      {"verdict": "mine" | "not_mine"}  (not_mine freezes the card)
    POST /api/cards/<user>/unfreeze
    GET  /api/feedback                  cardholder answers as labelled examples

The simulator streams transactions in real time into the same Pipeline the
Lambda runs (in-memory state instead of DynamoDB). Decisions are pushed to
the browser with Server-Sent Events; the AWS deployment uses API Gateway
WebSockets with the same message format, so the dashboard works with both.
Only the standard library is used.
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fraud_pipeline.assistant import Assistant, GeminiRouter  # noqa: E402
from fraud_pipeline.customer import CustomerDesk, PushNotifier, merchant  # noqa: E402
from fraud_pipeline.pipeline import ALLOW, BLOCK, FraudScorer, Pipeline  # noqa: E402
from fraud_pipeline.simulator import ATTACKS, TransactionSimulator  # noqa: E402
from fraud_pipeline.sinks import InMemoryAlertSink, alert_payload  # noqa: E402
from fraud_pipeline.state import InMemoryStateStore  # noqa: E402
from fraud_pipeline.store import AlertStore  # noqa: E402

DASHBOARD = ROOT / "dashboard" / "index.html"
CUSTOMER = ROOT / "dashboard" / "customer.html"


class Hub:
    """Fan-out of JSON messages to every connected browser."""

    def __init__(self):
        self.clients: set[queue.Queue] = set()
        self.lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=1000)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def publish(self, msg: dict):
        data = json.dumps(msg)
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(data)
                except queue.Full:  # slow client: drop rather than block the pipeline
                    pass


class Engine(threading.Thread):
    def __init__(self, hub: Hub, tps: float, users: int, fraud_rate: float, warmup: int,
                 store: AlertStore | None = None, notifier: PushNotifier | None = None):
        super().__init__(daemon=True)
        self.hub, self.tps, self.warmup = hub, tps, warmup
        self.store = store or AlertStore()
        self.desk = CustomerDesk(self.store)
        self.notifier = notifier
        self._pending = {"processed": 0, "flagged": 0, "blocked": 0}
        self.alerts = InMemoryAlertSink()
        self.pipeline = Pipeline(FraudScorer.default(), InMemoryStateStore(), [self.alerts])
        # start the simulated clock in the past so warm-up history ends "now"
        self.sim = TransactionSimulator(n_users=users, tps=tps, fraud_rate=fraud_rate,
                                        seed=None, start_ts=time.time() - warmup / tps)
        self.attacks: queue.Queue = queue.Queue()
        self.recent_alerts: deque = deque(reversed(self.store.recent(50)), maxlen=50)
        self.window: deque = deque()  # (wall_ts, latency_ms, decision, label)
        self.totals = {"processed": 0, "flagged": 0, "blocked": 0, "tp": 0, "fp": 0, "fn": 0}
        self.ready = False

    def run(self):
        # 1) build user history fast on the simulated clock (not shown on the dashboard)
        for txn in self.sim.stream(self.warmup):
            d = self.pipeline.process(txn)
            if d is not None:
                self.desk.record(txn, d.decision)
        self.ready = True
        # 2) real time from here on
        threading.Thread(target=self._stats_loop, daemon=True).start()
        for txn in self.sim.realtime():
            while not self.attacks.empty():
                kind, reply = self.attacks.get()
                user = self.sim.inject_attack(kind)
                if reply is not None:
                    reply.put(user)
                self.hub.publish({"type": "info", "message": f"Injected {kind.replace('_', ' ')} on {user}"})
            self._on_txn(txn)

    def _on_txn(self, txn):
        d = self.pipeline.process(txn)
        if d is None:
            return
        now = time.time()
        model_flagged = d.decision != ALLOW
        t = self.totals
        t["tp"] += model_flagged and d.label == 1  # precision/recall measure the model, before any card freeze
        t["fp"] += model_flagged and d.label == 0
        t["fn"] += (not model_flagged) and d.label == 1
        if self.desk.is_frozen(d.user_id):  # the cardholder said "not me": nothing else goes through
            d.decision = BLOCK
            d.rules_fired = [*d.rules_fired, "card_frozen"]
        flagged = d.decision != ALLOW
        t["processed"] += 1
        t["flagged"] += flagged
        t["blocked"] += d.decision == BLOCK
        self.desk.record(txn, d.decision)
        self.window.append((now, d.latency_ms))
        p = self._pending
        p["processed"] += 1
        p["flagged"] += flagged
        p["blocked"] += d.decision == BLOCK
        if flagged:
            msg = {**alert_payload(d), "features": d.features,
                   "merchant": merchant(txn.get("merchant_id", ""), d.merchant_category, d.city)}
            self.store.save(msg)
            self.recent_alerts.append(msg)
            self.hub.publish(msg)
            if self.notifier:
                threading.Thread(target=self.notifier.notify, args=(msg,), daemon=True).start()

    def answer(self, txn_id: str, verdict: str) -> dict | None:
        """The cardholder's reply to an alert. Returns None if there is no such alert."""
        alert = self.store.get(txn_id)
        if alert is None:
            return None
        out = self.desk.answer(alert, verdict)
        self.hub.publish({"type": "case", "txn_id": txn_id, "user_id": alert["user_id"], **out})
        if out["status"] == "recorded":
            self.hub.publish({"type": "info", "message": f"{alert['user_id']}: " + (
                "cardholder said NOT ME, card frozen" if verdict == "not_mine" else "cardholder confirmed the purchase")})
        return out

    def inject(self, kind: str, timeout: float = 2.0) -> str | None:
        """Queue an attack from another thread and wait for the targeted cardholder id."""
        reply: queue.Queue = queue.Queue(maxsize=1)
        self.attacks.put((kind, reply))
        try:
            return reply.get(timeout=timeout)
        except queue.Empty:
            return None

    def list_alerts(self, limit: int = 100) -> list[dict]:
        return self.store.recent(limit)

    def stats(self) -> dict:
        return {**self.store.stats(), "session": dict(self.totals)}

    def snapshot(self) -> dict:
        now = time.time()
        while self.window and now - self.window[0][0] > 10:
            self.window.popleft()
        lat = sorted(x[1] for x in self.window)
        pct = lambda p: lat[min(len(lat) - 1, int(p / 100 * (len(lat) - 1)))] if lat else 0
        t = self.totals
        precision = t["tp"] / (t["tp"] + t["fp"]) if t["tp"] + t["fp"] else None
        recall = t["tp"] / (t["tp"] + t["fn"]) if t["tp"] + t["fn"] else None
        return {"type": "stats", "ts": now, "tps": round(len(self.window) / 10, 1),
                "p50_ms": round(pct(50), 3), "p99_ms": round(pct(99), 3), **t,
                "precision": precision, "recall": recall,
                "users_tracked": len(self.pipeline.store),
                "model_version": self.pipeline.scorer.model.version}

    def _stats_loop(self):
        while True:
            pending, self._pending = self._pending, {"processed": 0, "flagged": 0, "blocked": 0}
            self.store.bump(**pending)  # one write per second, not one per transaction
            self.hub.publish(self.snapshot())
            time.sleep(1)


def make_handler(engine: Engine, hub: Hub, assistant: Assistant):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep the console readable
            pass

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._send(200, DASHBOARD.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/summary":
                return self._send(200, json.dumps({"ready": engine.ready, **engine.snapshot(),
                                                   "recent": list(engine.recent_alerts)}).encode())
            if path == "/events":
                return self._stream()
            if path == "/api/stats":
                return self._json(200, engine.stats())
            if path == "/api/alerts":
                q = parse_qs(urlparse(self.path).query)
                limit = max(1, min(200, int(q.get("limit", ["20"])[0] or 20)))
                return self._json(200, engine.store.recent(limit, q.get("user", [None])[0]))
            if path.startswith("/api/alerts/") and path.endswith("/context"):
                a = engine.store.get(path.split("/")[3])
                return self._json(200, engine.desk.context(a)) if a else self._json(404, {"error": "no alert with that id"})
            if path == "/api/feedback":
                return self._json(200, engine.desk.feedback())
            if path.startswith("/customer"):
                return self._send(200, CUSTOMER.read_bytes(), "text/html; charset=utf-8")
            if path.startswith("/api/alerts/"):
                a = engine.store.get(path.rsplit("/", 1)[1])
                return self._json(200, a) if a else self._json(404, {"error": "no alert with that id"})
            if path in ("/ar", "/ar/"):
                return self._send(200, (ROOT / "ar" / "index.html").read_bytes(), "text/html; charset=utf-8")
            self._send(404, b'{"error":"not found"}')

        def _json(self, code, obj):
            self._send(code, json.dumps(obj).encode())

        def _body(self) -> bytes:
            # Always consume the request body: with keep-alive, unread bytes would corrupt the next request.
            n = int(self.headers.get("Content-Length", 0) or 0)
            return self.rfile.read(min(n, 4096)) if n else b""

        def do_POST(self):
            url = urlparse(self.path)
            raw = self._body()
            if url.path == "/api/attack":
                kind = parse_qs(url.query).get("type", [""])[0]
                if kind not in ATTACKS:
                    return self._send(400, json.dumps({"error": f"type must be one of {ATTACKS}"})
                                      .encode())
                engine.attacks.put((kind, None))
                return self._send(202, json.dumps({"queued": kind}).encode())
            if url.path.startswith("/api/alerts/") and url.path.endswith("/feedback"):
                try:
                    verdict = str(json.loads(raw or b"{}").get("verdict", ""))
                except (ValueError, AttributeError):
                    verdict = ""
                if verdict not in ("mine", "not_mine"):
                    return self._json(400, {"error": 'send JSON like {"verdict": "not_mine"} (or "mine")'})
                out = engine.answer(url.path.split("/")[3], verdict)
                return self._json(200, out) if out else self._json(404, {"error": "no alert with that id"})
            if url.path.startswith("/api/cards/") and url.path.endswith("/unfreeze"):
                user = url.path.split("/")[3]
                engine.desk.unfreeze(user)
                hub.publish({"type": "info", "message": f"{user}: card unfrozen"})
                return self._json(200, {"user_id": user, "frozen": False})
            if url.path == "/api/ask":
                try:
                    text = str(json.loads(raw or b"{}").get("text", "")).strip()
                except (ValueError, AttributeError):
                    return self._json(400, {"error": 'send JSON like {"text": "give me a summary"}'})
                if not text:
                    return self._json(400, {"error": "text is empty"})
                t0 = time.perf_counter()
                out = assistant.ask(text[:500])
                out["ms"] = round((time.perf_counter() - t0) * 1000, 2)
                return self._json(200, out)
            self._send(404, b'{"error":"not found"}')

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = hub.subscribe()
            try:
                for msg in list(engine.recent_alerts):  # backfill so the feed isn't empty
                    self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        data = q.get(timeout=15)
                        self.wfile.write(f"data: {data}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                hub.unsubscribe(q)

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--tps", type=float, default=50)
    ap.add_argument("--users", type=int, default=5000)
    ap.add_argument("--fraud-rate", type=float, default=0.01,
                    help="share of events that start an attack (higher = more alerts)")
    ap.add_argument("--warmup", type=int, default=100_000,
                    help="historical transactions to build user profiles first")
    ap.add_argument("--db", default=str(ROOT / "data" / "swipestop.db"), help="SQLite file for alerts")
    ap.add_argument("--notify", metavar="TOPIC",
                    help="push 'was this you?' to your phone for blocked purchases via ntfy.sh (free app, no account)")
    ap.add_argument("--public-url", help="address your phone can open, used in the push link "
                                         "(default http://HOST:PORT)")
    args = ap.parse_args()

    hub = Hub()
    notifier = None
    if args.notify:
        notifier = PushNotifier(args.notify, args.public_url or f"http://{args.host}:{args.port}")
        print(f"push alerts: subscribe to '{args.notify}' in the ntfy app")
    engine = Engine(hub, args.tps, args.users, args.fraud_rate, args.warmup, AlertStore(args.db), notifier)
    router = GeminiRouter.from_env()
    assistant = Assistant(engine.list_alerts, engine.stats, engine.inject, router)
    print("assistant: rules" + (" + Gemini for free-form questions" if router else
                                " only (set GEMINI_API_KEY to add free-form questions)"))
    print(f"warming up user profiles with {args.warmup:,} historical transactions...")
    engine.start()
    while not engine.ready:
        time.sleep(0.2)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(engine, hub, assistant))
    server.daemon_threads = True
    print(f"live at http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
