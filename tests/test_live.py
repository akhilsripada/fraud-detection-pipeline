"""End-to-end: start the real server on a free port and talk to it over HTTP and MCP."""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "local"))
sys.path.insert(0, str(ROOT / "mcp"))
sys.path.insert(0, str(ROOT / "src"))

import demo_server  # noqa: E402
from fraud_pipeline.assistant import Assistant  # noqa: E402
from fraud_pipeline.store import AlertStore  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        hub = demo_server.Hub()
        cls.engine = demo_server.Engine(hub, tps=200, users=300, fraud_rate=0.05, warmup=4000,
                                        store=AlertStore(os.path.join(tempfile.mkdtemp(), "t.db")))
        cls.engine.start()
        while not cls.engine.ready:
            time.sleep(0.05)
        assistant = Assistant(cls.engine.list_alerts, cls.engine.stats, cls.engine.inject)
        cls.port = free_port()
        cls.server = ThreadingHTTPServer(("127.0.0.1", cls.port), demo_server.make_handler(cls.engine, hub, assistant))
        cls.server.daemon_threads = True
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"
        deadline = time.time() + 20                     # wait for some live alerts
        while time.time() < deadline and len(cls.engine.list_alerts(5)) < 3:
            time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return json.loads(r.read())

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def test_alerts_are_stored_and_served(self):
        alerts = self.get("/api/alerts?limit=5")
        self.assertGreaterEqual(len(alerts), 3)
        one = self.get("/api/alerts/" + alerts[0]["txn_id"])
        self.assertIn("features", one)

    def test_ask_explains_and_attacks(self):
        out = self.post("/api/ask", {"text": "Why was the last transaction blocked?"})
        self.assertTrue(out["text"].startswith(("Blocked", "Sent for review")))
        self.assertLess(out["ms"], 200)
        out = self.post("/api/ask", {"text": "simulate an account takeover"})
        user = out["attack"]["user"]
        self.assertRegex(user, r"^u\d{5}$")
        deadline = time.time() + 15
        while time.time() < deadline and not self.get(f"/api/alerts?user={user}"):
            time.sleep(0.2)
        self.assertTrue(self.get(f"/api/alerts?user={user}"), "the injected attack should be caught")

    def test_bad_requests(self):
        req = urllib.request.Request(self.base + "/api/ask", data=b"not json", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_dashboard_and_ar_pages(self):
        for path, marker in (("/", b"Stop fraud before"), ("/console", b"Ask Swipestop"), ("/ar", b"<html")):
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                self.assertIn(marker, r.read(), path)

    def test_alerts_carry_their_explanation_and_tape_samples_stream(self):
        alert = self.engine.list_alerts(1)[0]
        self.assertTrue(alert["why"])                          # the case file renders the engine's own reasons
        self.assertGreater(alert["usual"], 0)
        summary = self.get("/api/summary")
        self.assertIsInstance(summary["recent"], list)
        q = self.engine.hub.subscribe()
        try:
            deadline, kinds = time.time() + 5, set()
            while time.time() < deadline and "txn" not in kinds:
                try:
                    kinds.add(json.loads(q.get(timeout=1))["type"])
                except Exception:
                    pass
            self.assertIn("txn", kinds)
        finally:
            self.engine.hub.unsubscribe(q)

    def test_keep_alive_connection_survives_a_post_with_a_body(self):
        import http.client
        c = http.client.HTTPConnection(self.base.split("//")[1], timeout=5)
        c.request("POST", "/api/attack?type=amount_spike", body=b"{}", headers={"Content-Type": "application/json"})
        c.getresponse().read()
        c.request("POST", "/api/ask", body=json.dumps({"text": "give me a summary"}), headers={"Content-Type": "application/json"})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn("Scored", json.loads(r.read())["text"])

    def test_mcp_round_trip(self):
        import swipestop_mcp
        swipestop_mcp.BASE = self.base
        init = swipestop_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                    "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "swipestop")
        self.assertIsNone(swipestop_mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = swipestop_mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual({t["name"] for t in tools},
                         {"ask_swipestop", "recent_alerts", "fraud_stats", "simulate_attack", "cardholder_alert"})
        out = swipestop_mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                   "params": {"name": "ask_swipestop", "arguments": {"question": "give me a summary"}}})
        text = out["result"]["content"][0]["text"]
        self.assertIn("Scored", text)
        self.assertIn("Live dashboard:", text)
        bad = swipestop_mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}})
        self.assertTrue(bad["result"]["isError"])
        self.assertEqual(swipestop_mcp.handle({"jsonrpc": "2.0", "id": 5, "method": "x"})["error"]["code"], -32601)


    def test_cardholder_alert_flow(self):
        import swipestop_mcp
        swipestop_mcp.BASE = self.base
        alert = self.get("/api/alerts?limit=1")[0]
        tid = alert["txn_id"]
        ctx = self.get(f"/api/alerts/{tid}/context")
        self.assertEqual(ctx["alert"]["txn_id"], tid)
        self.assertTrue(ctx["merchant"]["name"].endswith(alert["city"]))
        self.assertTrue(ctx["timeline"][-1]["this"])
        self.assertIn("utc", ctx["when"])
        with urllib.request.urlopen(f"{self.base}/customer/{tid}", timeout=5) as r:
            self.assertIn(b"Did you make this purchase", r.read())
        out = self.post(f"/api/alerts/{tid}/feedback", {"verdict": "not_mine"})
        self.assertEqual((out["status"], out["frozen"]), ("recorded", True))
        self.assertTrue(self.get(f"/api/alerts/{tid}/context")["card"]["frozen"])
        self.assertIn(tid, [f["txn_id"] for f in self.get("/api/feedback")])
        text = swipestop_mcp.call_tool("cardholder_alert", {"txn_id": tid[:8]})
        self.assertIn("card frozen by the cardholder", text)
        self.assertIn(f"/customer/{tid}", text)
        self.assertFalse(self.post(f"/api/cards/{alert['user_id']}/unfreeze", {})["frozen"])
        for path, body in ((f"/api/alerts/{tid}/feedback", {"verdict": "perhaps"}), ("/api/alerts/nope/feedback", {"verdict": "mine"})):
            with self.assertRaises(urllib.error.HTTPError):
                self.post(path, body)


class McpOfflineTests(unittest.TestCase):
    def test_unreachable_server_is_a_clear_error(self):
        import swipestop_mcp
        old, swipestop_mcp.BASE = swipestop_mcp.BASE, f"http://127.0.0.1:{free_port()}"
        try:
            out = swipestop_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": "fraud_stats", "arguments": {}}})
        finally:
            swipestop_mcp.BASE = old
        self.assertTrue(out["result"]["isError"])
        self.assertIn("python run.py", out["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
