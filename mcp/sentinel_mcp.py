"""Sentinel MCP server: lets an AI assistant (Claude Desktop, Claude Code, ChatGPT,
Cursor...) query the live fraud stream in plain English.

It speaks the Model Context Protocol over stdio (JSON-RPC 2.0, one message per
line) with the standard library only, and forwards every tool call to the
Sentinel HTTP API, so the assistant sees exactly what the dashboard sees.

    python mcp/sentinel_mcp.py                       # normally started by your AI app
    SENTINEL_URL=http://my-server:8000 python mcp/sentinel_mcp.py

Example (Claude Desktop, claude_desktop_config.json):
    {"mcpServers": {"sentinel": {"command": "python3",
        "args": ["/path/to/fraud-detection-pipeline/mcp/sentinel_mcp.py"]}}}
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

SERVER = {"name": "sentinel", "version": "1.0.0"}
DEFAULT_PROTOCOL = "2025-06-18"
BASE = os.environ.get("SENTINEL_URL", "http://127.0.0.1:8000").rstrip("/")

TOOLS = [
    {"name": "ask_sentinel",
     "description": "Ask the live fraud monitor a plain-English question, e.g. 'why was u01037 blocked?', "
                    "'show me fraud in Europe', 'give me a summary'. Returns the answer and a dashboard link.",
     "inputSchema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
    {"name": "recent_alerts",
     "description": "List the most recent flagged transactions (BLOCK or REVIEW), optionally for one cardholder.",
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
         "user_id": {"type": "string", "description": "cardholder id like u01037"}}}},
    {"name": "fraud_stats",
     "description": "Running totals: transactions scored, flagged and blocked.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "simulate_attack",
     "description": "Inject a simulated fraud attack into the live stream to demonstrate detection.",
     "inputSchema": {"type": "object", "properties": {"kind": {"type": "string", "enum": [
         "card_testing", "impossible_travel", "amount_spike", "account_takeover"]}}, "required": ["kind"]}},
    {"name": "cardholder_alert",
     "description": "What the cardholder was told about a flagged purchase: the store, exact coordinates, the local "
                    "time where it happened, the card's previous purchases with distance and speed between them, "
                    "and whether the cardholder answered 'it was me' or froze the card. Give a cardholder id "
                    "(their latest alert) or a transaction id.",
     "inputSchema": {"type": "object", "properties": {
         "user_id": {"type": "string", "description": "cardholder id like u01037"},
         "txn_id": {"type": "string", "description": "transaction id (or its first characters)"}}}},
]


def http(method: str, path: str, body: dict | None = None, opener=urllib.request.urlopen):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with opener(req, timeout=10) as r:
        return json.loads(r.read() or b"null")


def fmt_alert(a: dict) -> str:
    reasons = ", ".join(a.get("rules_fired") or []) or "model score"
    return (f"- {a['decision']} {a['user_id']} ${a['amount']:,.2f} in {a.get('city')}, {a.get('country')} "
            f"(risk {a['risk_score'] * 100:.0f}/100; {reasons}; txn {a['txn_id'][:10]})")


def call_tool(name: str, args: dict, opener=urllib.request.urlopen) -> str:
    link = f"\n\nLive dashboard: {BASE}/"
    if name == "ask_sentinel":
        q = str(args.get("question", "")).strip()
        if not q:
            raise ValueError("question is required")
        out = http("POST", "/api/ask", {"text": q}, opener)
        parts = [out["text"]] + [f"- {r}" for r in out.get("reasons", [])]
        if out.get("next_step"):
            parts.append(out["next_step"])
        return "\n".join(parts) + link
    if name == "recent_alerts":
        limit = max(1, min(50, int(args.get("limit", 10))))
        qs = {"limit": limit, **({"user": args["user_id"]} if args.get("user_id") else {})}
        alerts = http("GET", "/api/alerts?" + urllib.parse.urlencode(qs), opener=opener)
        return ("\n".join(fmt_alert(a) for a in alerts) or "No alerts yet.") + link
    if name == "fraud_stats":
        s = http("GET", "/api/stats", opener=opener)
        return (f"Scored {s['processed']:,} transactions, flagged {s['flagged']:,}, blocked {s['blocked']:,}." + link)
    if name == "simulate_attack":
        out = http("POST", "/api/ask", {"text": f"simulate {str(args.get('kind', '')).replace('_', ' ')}"}, opener)
        return out["text"] + link
    if name == "cardholder_alert":
        return cardholder_alert(args, opener)
    raise ValueError(f"unknown tool {name!r}")


def cardholder_alert(args: dict, opener=urllib.request.urlopen) -> str:
    uid, tid = str(args.get("user_id") or "").strip(), str(args.get("txn_id") or "").strip()
    if not uid and not tid:
        raise ValueError("give user_id or txn_id")
    alerts = http("GET", "/api/alerts?" + urllib.parse.urlencode({"limit": 200, **({"user": uid} if uid else {})}), opener=opener)
    match = next((a for a in alerts if not tid or a["txn_id"].startswith(tid)), None)
    if match is None:
        return f"No flagged purchase found for {uid or tid}."
    c = http("GET", f"/api/alerts/{match['txn_id']}/context", opener=opener)
    a, m, ans = c["alert"], c["merchant"], c.get("answer")
    status = ("card frozen by the cardholder" if ans and ans["verdict"] == "not_mine" else
              "cardholder confirmed it was them" if ans else "waiting for the cardholder to answer")
    lines = [f"{a['decision']} ${a['amount']:,.2f} at {m['name']} ({m['lat']:.4f}, {m['lon']:.4f}), "
             f"{c['when']['local']} {c['when']['tz']} local time ({c['when']['utc']}). Status: {status}.",
             "Card's recent purchases:"]
    for t in c["timeline"]:
        gap = t.get("gap")
        hop = f"; {gap['km']:,} km after the previous one, {gap['minutes']} min later" if gap and gap["km"] >= 30 else ""
        lines.append(f"- {t['when']['local']} {t['when']['tz']}: ${t['amount']:,.2f} at {t['merchant']}{hop}"
                     + (" <- this one" if t.get("this") else ""))
    return "\n".join(lines) + f"\n\nCardholder's alert page: {BASE}/customer/{a['txn_id']}"


def handle(msg: dict, opener=urllib.request.urlopen) -> dict | None:
    """Handle one JSON-RPC message. Returns the response, or None for notifications."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:  # notification (e.g. notifications/initialized): never answered
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", DEFAULT_PROTOCOL),
                  "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER,
                  "instructions": "Use these tools to answer questions about live card-fraud alerts."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            text = call_tool(params.get("name"), params.get("arguments") or {}, opener)
            result = {"content": [{"type": "text", "text": text}]}
        except urllib.error.URLError:
            result = {"content": [{"type": "text", "text": f"Sentinel isn't reachable at {BASE}. Start it with "
                                                           "`python run.py` and try again."}], "isError": True}
        except Exception as e:
            result = {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            out = handle(msg)
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
