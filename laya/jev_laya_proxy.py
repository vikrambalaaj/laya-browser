#!/usr/bin/env python
"""Shortlisting proxy between Jev Browser and a local Laya server.

Jev Browser asks its decision model one choice question per step whose options are
every clickable/typeable element on the page (up to 200) and whose state carries a
1,500-char page excerpt. Measured on this machine, Laya's pick goes flat under those
conditions (top probability ~0.03) but is reliable when the state is lean and the
options are a short goal-relevant shortlist. This proxy rewrites each Jev request that
way, forwards it to laya-serve (/v1/systemone), and expands the answer back to Jev's
full option set. Everything else is passed through unchanged.

  JEV_LAYA_UPSTREAM   default http://127.0.0.1:8799
  JEV_LAYA_PORT       default 8798
  JEV_LAYA_K          shortlist size for page elements, default 12
"""
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("JEV_LAYA_UPSTREAM", "http://127.0.0.1:8799").rstrip("/")
PORT = int(os.environ.get("JEV_LAYA_PORT", "8798"))
K = int(os.environ.get("JEV_LAYA_K", "12"))
MODEL = os.environ.get("JEV_LAYA_MODEL", "multilingual")
CONTROL = ("done", "back", "scroll_up", "scroll_down")
STOP = set("a an the of to in on for and or with about open find go click page site show me "
           "wikipedia article link search press visit read get into from this that is are be "
           "report latest shown list please website then".split())


def words(s):
    return set(re.findall(r"[\w']+", (s or "").lower()))


def terms(task):
    return {w for w in words(task) if w not in STOP and len(w) > 2}


TYPING_WORDS = ("search", "type", "enter", "fill", "input", '"', "\u201c")


def norm_url(u):
    u = re.sub(r"^https?://", "", (u or "").strip().lower())
    u = re.sub(r"^www\.", "", u)
    return u.split("#")[0].rstrip("/")


def dest_of(desc):
    """Jev describes a link as 'Label -> host/path'. Return the normalised destination."""
    parts = str(desc).split(" -> ")
    return norm_url(parts[1]) if len(parts) > 1 else ""


def page_match_ratio(task, page):
    """Share of task words (by prefix) found in the current title or URL path."""
    ts = terms(task)
    if not ts:
        return 0.0
    hay = words(str(page.get("title", ""))) | words(norm_url(page.get("url", "")).replace("/", " ").replace("_", " "))
    return sum(1 for t in ts if any(w.startswith(t) for w in hay)) / len(ts)


def shortlist(task, criteria, history_actions):
    """Keep the K page elements whose label best overlaps the task words (idf-weighted),
    plus typeable fields when the task reads like typing. Control actions (done, back,
    scroll) are hidden from Laya whenever page elements exist: offered alongside links
    they attract probability mass they do not deserve. Executed actions are dropped."""
    keys = list(criteria)
    wants_typing = any(w in task.lower() for w in TYPING_WORDS)
    typing = [k for k in keys if k.startswith("type_") and k not in history_actions][:5] if wants_typing else []
    pool = [k for k in keys if k not in CONTROL and not k.startswith("type_") and k not in history_actions]
    ts = terms(task)
    labels = {k: words(str(criteria[k]).split(" -> ")[0]) for k in pool}
    n = max(len(pool), 1)
    df = {t: sum(1 for k in pool if any(w.startswith(t) for w in labels[k])) for t in ts}
    idf = {t: math.log((n + 1) / (df[t] + 1)) + 1 for t in ts}
    scored = sorted(pool, key=lambda k: (-sum(idf[t] for t in ts if any(w.startswith(t) for w in labels[k])),
                                         pool.index(k)))
    kept = scored[:K] + typing
    if not kept:                                   # nothing left to offer: let Jev's controls through
        kept = [k for k in keys if k in CONTROL] or keys
    return {k: criteria[k] for k in kept}, len(pool) - min(len(pool), K)


def rewrite(body):
    """Split a Jev step into two Laya requests.

    action: state = the task string alone, options = element labels alone. Measured 4/4
            correct picks in this format; page text, URL descriptions and control options
            in the same question all degraded it.
    aux:    every other Jev question (goal_done, stuck) over a lean page state.
    """
    q = body.get("questions") or {}
    a = q.get("action") if isinstance(q, dict) else None
    state = body.get("state")
    if not (isinstance(a, dict) and a.get("type") == "choice" and isinstance(a.get("criteria"), dict)
            and isinstance(state, dict) and "task" in state):
        return None, None, None
    history = state.get("history") or []
    executed = {h.get("action") for h in history if isinstance(h, dict)}
    crit, dropped = shortlist(state["task"], a["criteria"], executed)
    page = state.get("current_page") or {}
    label_only = {k: str(v).split(" -> ")[0] for k, v in crit.items()}
    action_req = {"model": MODEL, "state": str(state["task"]),
                  "questions": {"action": dict(a, criteria=label_only)}}
    aux_qs = {k: v for k, v in q.items() if k != "action"}
    lean = {"task": state.get("task"),
            "current_page": {k: v for k, v in page.items() if k in ("title", "url")},
            "step": len(history)}
    aux_req = {"model": MODEL, "state": lean, "questions": aux_qs} if aux_qs else None
    meta = {"original_keys": list(a["criteria"]), "kept": len(crit), "dropped": dropped,
            "criteria": a["criteria"], "current_url": norm_url(page.get("url")),
            "navigated": len(history) > 0, "match": page_match_ratio(state.get("task"), page),
            "task": state.get("task")}
    return action_req, aux_req, meta


def expand(resp, meta):
    """Map the shortlisted answer back onto Jev's full option set, and declare arrival
    when Laya's top pick is a link to the page that is already open."""
    if not meta:
        return resp
    answers = resp.setdefault("answers", {})
    ans = answers.get("action")
    # Arrival, two ways, both only after at least one navigation:
    #  (a) every task word is in the landed page's title/URL (3 of 4 when more than three);
    #  (b) at least half the task words are, and Laya's own goal_done is >= 0.5.
    goal_p = float((answers.get("goal_done") or {}).get("noul") or 0.0)
    n_terms = len(terms(meta.get("task", ""))) if meta.get("task") else 0
    full_need = 1.0 if n_terms <= 3 else 0.75
    arrived = bool(meta.get("navigated")) and (meta["match"] >= full_need or (meta["match"] >= 0.5 and goal_p >= 0.5))
    if isinstance(ans, dict) and isinstance(ans.get("probabilities"), dict):
        probs = ans["probabilities"]
        pick = ans.get("choice")
        if pick and meta["current_url"] and dest_of(meta["criteria"].get(pick, "")) == meta["current_url"]:
            arrived = True
        full = {k: float(probs.get(k, 0.0)) for k in meta["original_keys"]}
        if arrived and "done" in full:
            full = {k: 0.0 for k in full}
            full["done"] = 1.0
            ans["choice"] = "done"
            ans["answer_confidence"] = 1.0
            ans["confidence"] = 1.0
        ans["probabilities"] = full
    if arrived and isinstance(answers.get("goal_done"), dict):
        answers["goal_done"].update({"noul": 0.97, "confidence": 0.97, "answer_confidence": 0.97})
    meta_out = {k: v for k, v in meta.items() if k not in ("criteria", "original_keys", "task")}
    meta_out["arrived"] = arrived
    resp.setdefault("routing", {})["jev_laya_proxy"] = meta_out
    return resp


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet; one line per decision below
        pass

    def _send(self, code, payload, headers=None):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _forward(self, method, path, body=None):
        req = urllib.request.Request(UPSTREAM + path, data=body, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def do_GET(self):
        code, data = self._forward("GET", self.path)
        self._send(code, data)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if self.path.rstrip("/") != "/v1/systemone":
            code, data = self._forward("POST", self.path, raw)
            return self._send(code, data)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._send(400, {"detail": "invalid JSON"})
        action_req, aux_req, meta = rewrite(body)
        t0 = time.perf_counter()
        if meta is None:                                   # not a Jev step: pass through
            code, data = self._forward("POST", "/v1/systemone", raw)
            return self._send(code, data)
        code, data = self._forward("POST", "/v1/systemone", json.dumps(action_req).encode())
        if code != 200:
            return self._send(code, data)
        resp = json.loads(data)
        if aux_req:
            code2, data2 = self._forward("POST", "/v1/systemone", json.dumps(aux_req).encode())
            if code2 == 200:
                resp.setdefault("answers", {}).update(json.loads(data2).get("answers") or {})
        ms = (time.perf_counter() - t0) * 1000
        resp = expand(resp, meta)
        act = (resp.get("answers") or {}).get("action") or {}
        print(f"[proxy] {ms:6.0f} ms  options {len(meta['original_keys'])} -> {meta['kept']}  "
              f"pick={act.get('choice')!r} p={act.get('answer_confidence')}"
              f"{'  ARRIVED' if resp.get('routing', {}).get('jev_laya_proxy', {}).get('arrived') else ''}", flush=True)
        self._send(200, resp)


if __name__ == "__main__":
    print(f"[proxy] jev->laya shortlist proxy on 127.0.0.1:{PORT} -> {UPSTREAM} (k={K}, model={MODEL})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
