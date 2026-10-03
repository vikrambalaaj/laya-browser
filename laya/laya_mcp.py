#!/usr/bin/env python
"""MCP stdio server exposing the warm Laya browser service to Claude Code / Jarvis.

Tools: laya_navigate (goal-directed browsing, returns status, steps and page text) and
laya_classify (typed questions about one page). Talks to laya_browserd over loopback and
starts it if it is not running. Newline-delimited JSON-RPC on stdin/stdout; logs to stderr.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = f"http://127.0.0.1:{os.environ.get('LAYA_BROWSERD_PORT', '8797')}"

TOOLS = [
    {"name": "laya_navigate",
     "description": "Browse toward a goal with Laya choosing each click locally (~0.5 s per decision, "
                    "warm browser). Returns status, per-step picks, final URL/title and the page text. "
                    "Use stop_when (substring of the target URL or title) whenever success is checkable.",
     "inputSchema": {"type": "object", "required": ["task", "start_url"], "properties": {
         "task": {"type": "string", "description": "Goal in the target page's own words, e.g. 'Open the downloads page'. "
                                                   "A \"quoted\" string is typed into the input Laya picks (needs allow_typing)."},
         "start_url": {"type": "string"},
         "stop_when": {"type": "string", "description": "case-insensitive substring of URL or title that marks success"},
         "allowed_hosts": {"type": "array", "items": {"type": "string"}, "description": "default: the start host"},
         "allow_typing": {"type": "boolean", "default": False},
         "max_steps": {"type": "integer", "default": 4},
         "headed": {"type": "boolean", "default": False, "description": "show the browser window on screen"},
         "max_chars": {"type": "integer", "default": 4000, "description": "page text returned"}}}},
    {"name": "laya_classify",
     "description": "Answer typed questions (choice / score / yes-no) about one web page with Laya. "
                    "Default questions: page_type, requires_login, has_prices, content_depth.",
     "inputSchema": {"type": "object", "required": ["url"], "properties": {
         "url": {"type": "string"},
         "questions": {"type": "object", "description": "optional Laya question dict"}}}},
]


def err(msg):
    print(f"[laya-mcp] {msg}", file=sys.stderr, flush=True)


def http(path, payload=None, timeout=300):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Content-Type": "application/json"}, method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def ensure_daemon():
    try:
        if http("/health", timeout=3).get("status") == "ok":
            return True
    except Exception:  # noqa: BLE001
        pass
    err("service not running; starting laya_browserd")
    subprocess.run([os.path.join(HERE, "browserd.sh"), "start"], stdout=sys.stderr, stderr=sys.stderr, check=False)
    for _ in range(120):
        try:
            if http("/health", timeout=3).get("status") == "ok":
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    return False


def call(name, a):
    if not ensure_daemon():
        return "laya_browserd did not come up; run ~/jarvis/laya/browserd.sh logs"
    if name == "laya_navigate":
        payload = {"url": a["start_url"], "goal": a["task"], "stop_when": a.get("stop_when"),
                   "allowed_hosts": a.get("allowed_hosts"), "allow_typing": bool(a.get("allow_typing", False)),
                   "max_steps": int(a.get("max_steps", 4)), "headed": bool(a.get("headed", False))}
        t = http("/browse", payload)
        steps = [{k: s.get(k) for k in ("step", "title", "url", "action", "action_p", "result")} for s in t.get("steps", [])]
        out = {"status": t.get("status"), "final_url": t.get("final_url"), "final_title": t.get("final_title"),
               "elapsed_ms": t.get("elapsed_ms"), "steps": steps, "run_dir": t.get("run_dir"),
               "page_text": (t.get("final_text") or "")[: int(a.get("max_chars", 4000))]}
        return json.dumps(out, ensure_ascii=False, indent=1)
    if name == "laya_classify":
        return json.dumps(http("/classify", {"url": a["url"], "questions": a.get("questions")}), ensure_ascii=False, indent=1)
    return f"unknown tool {name}"


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            res = {"protocolVersion": params.get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}},
                   "serverInfo": {"name": "laya-browser", "version": "0.1.0"}}
        elif method == "tools/list":
            res = {"tools": TOOLS}
        elif method == "tools/call":
            try:
                text = call(params.get("name"), params.get("arguments") or {})
                res = {"content": [{"type": "text", "text": text}], "isError": False}
            except urllib.error.HTTPError as e:
                res = {"content": [{"type": "text", "text": f"HTTP {e.code}: {e.read().decode()[:300]}"}], "isError": True}
            except Exception as e:  # noqa: BLE001
                res = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
        elif method == "ping":
            res = {}
        elif mid is None:
            continue                      # notification
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}), flush=True)
            continue
        if mid is not None:
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}), flush=True)


if __name__ == "__main__":
    main()
