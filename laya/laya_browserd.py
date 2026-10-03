#!/usr/bin/env python
"""Warm Laya browser service for Jarvis.

One resident Laya model and one resident Chromium, behind a loopback HTTP API, so a
browse request starts in well under a second instead of paying ~8 s of model load and
~2 s of browser launch per call. Serves three things:

  POST /browse          the laya-browser skill's goal loop   {url, goal, stop_when, ...}
  POST /classify        typed questions about one page       {url, questions?}
  POST /v1/systemone    TypeSafe protocol for Jev Browser, with the shortlist rewrite
                        from jev_laya_proxy applied in-process
  GET  /health

Playwright's sync API is thread-bound, so all browser work runs on one worker thread.
Environment: LAYA_BROWSERD_PORT (8797), LAYA_RUNS_DIR (~/.local/share/laya/runs).
"""
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_SCRIPTS = os.path.expanduser("~/jarvis/.claude/skills/laya-browser/scripts")
sys.path[:0] = [HERE, SKILL_SCRIPTS]

import laya_browse as lb          # noqa: E402
import jev_laya_proxy as jp       # noqa: E402

PORT = int(os.environ.get("LAYA_BROWSERD_PORT", "8797"))
RUNS = os.path.expanduser(os.environ.get("LAYA_RUNS_DIR", "~/.local/share/laya/runs"))
STARTED = time.time()

state = {"agent": None, "embed_fn": None, "pw": None, "browser": None, "headed": None,
         "jobs": 0, "last_job_ms": None}
browser_thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="browser")
model_lock = threading.Lock()


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ----------------------------------------------------------------------------- model

def load_model():
    from laya.shortlist import embed_fn_from_agent
    t0 = time.perf_counter()
    state["agent"] = lb.load_agent()
    state["embed_fn"] = embed_fn_from_agent(state["agent"])
    # warm the MPS graph so the first real request is not the slow one
    state["agent"].predict("warm-up", {"q": {"type": "choice", "instructions": "pick",
                                             "criteria": {"a": "a", "b": "b"}}})
    log(f"[model] ready in {time.perf_counter()-t0:.1f}s on {state['agent'].device}")


def predict(state_obj, questions, max_len=2048):
    with model_lock:
        return state["agent"].predict(state_obj, questions, max_len=max_len)


# ----------------------------------------------------------------------------- browser (one thread)

def _browser(headed=False):
    """Return the resident browser for the requested mode, launching or relaunching it."""
    from playwright.sync_api import sync_playwright
    if state["pw"] is None:
        state["pw"] = sync_playwright().start()
    key = "headed" if headed else "browser"
    b = state[key]
    if b is None or not b.is_connected():
        t0 = time.perf_counter()
        state[key] = lb.launch_browser(state["pw"], headed)
        log(f"[browser] launched {'headed' if headed else 'headless'} chromium in {time.perf_counter()-t0:.2f}s")
    return state[key]


def _run_browse(req):
    run_dir = os.path.join(RUNS, time.strftime("%Y%m%d-%H%M%S"))
    args = SimpleNamespace(
        url=req["url"], goal=req["goal"], max_steps=int(req.get("max_steps", 4)), k=int(req.get("k", 10)),
        allowed_hosts=req.get("allowed_hosts"), allow_typing=bool(req.get("allow_typing", False)),
        done_threshold=req.get("done_threshold"), stop_when=req.get("stop_when"), out=run_dir,
        max_len=int(req.get("max_len", 2048)))
    headed = bool(req.get("headed", False))
    t0 = time.perf_counter()
    try:
        trace = lb.browse_once(state["agent"], state["embed_fn"], _browser(headed), args)
    except Exception as e:  # noqa: BLE001  (browser died mid-run: relaunch once and retry)
        log(f"[browse] {type(e).__name__}: {str(e)[:120]}; relaunching browser and retrying once")
        state["headed" if headed else "browser"] = None
        trace = lb.browse_once(state["agent"], state["embed_fn"], _browser(headed), args)
    trace["elapsed_ms"] = round((time.perf_counter() - t0) * 1000)
    trace["run_dir"] = run_dir
    state["jobs"] += 1
    state["last_job_ms"] = trace["elapsed_ms"]
    log(f"[browse] {trace['status']} in {trace['elapsed_ms']} ms  {trace.get('final_url')}")
    return trace


def _run_classify(req):
    args = SimpleNamespace(url=req["url"], questions=req.get("questions"), max_len=int(req.get("max_len", 2048)),
                           json=False, quiet=True)
    t0 = time.perf_counter()
    out = lb.classify_once(state["agent"], _browser(bool(req.get("headed", False))), args)
    out["elapsed_ms"] = round((time.perf_counter() - t0) * 1000)
    state["jobs"] += 1
    return out


# ----------------------------------------------------------------------------- http

def create_app():
    from fastapi import FastAPI, HTTPException, Request
    import asyncio

    app = FastAPI(title="laya-browserd")

    @app.on_event("startup")
    def _startup():
        load_model()
        browser_thread.submit(_browser, False).result()   # browser warm before the first request

    @app.get("/health")
    def health():
        b = state["browser"]
        return {"status": "ok" if state["agent"] is not None else "loading",
                "model": "multilingual", "device": str(getattr(state["agent"], "device", "")),
                "browser": bool(b and b.is_connected()), "headed": bool(state["headed"] and state["headed"].is_connected()),
                "jobs": state["jobs"], "last_job_ms": state["last_job_ms"],
                "uptime_s": round(time.time() - STARTED)}

    @app.post("/browse")
    async def browse(request: Request):
        req = await request.json()
        if not req.get("url") or not req.get("goal"):
            raise HTTPException(400, "url and goal are required")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(browser_thread, _run_browse, req)

    @app.post("/classify")
    async def classify(request: Request):
        req = await request.json()
        if not req.get("url"):
            raise HTTPException(400, "url is required")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(browser_thread, _run_classify, req)

    @app.post("/v1/systemone")
    async def systemone(request: Request):
        body = await request.json()
        if not isinstance(body, dict) or "questions" not in body:
            raise HTTPException(400, "body must carry 'questions'")
        loop = asyncio.get_running_loop()

        def work():
            action_req, aux_req, meta = jp.rewrite(body)
            t0 = time.perf_counter()
            if meta is None:
                return predict(body.get("state"), body["questions"])
            resp = predict(action_req["state"], action_req["questions"])
            if aux_req:
                resp.setdefault("answers", {}).update(predict(aux_req["state"], aux_req["questions"],
                                                              max_len=1024).get("answers") or {})
            resp = jp.expand(resp, meta)
            act = (resp.get("answers") or {}).get("action") or {}
            log(f"[jev] {(time.perf_counter()-t0)*1000:5.0f} ms  options {len(meta['original_keys'])} -> {meta['kept']}"
                f"  pick={act.get('choice')!r} p={act.get('answer_confidence')}"
                f"{'  ARRIVED' if resp.get('routing', {}).get('jev_laya_proxy', {}).get('arrived') else ''}")
            return resp

        try:
            return await loop.run_in_executor(None, work)
        except ValueError as e:
            raise HTTPException(422, str(e))

    return app


if __name__ == "__main__":
    import uvicorn
    os.makedirs(RUNS, exist_ok=True)
    log(f"[browserd] starting on 127.0.0.1:{PORT}")
    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")
