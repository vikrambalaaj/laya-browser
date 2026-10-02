#!/usr/bin/env python
"""End-to-end tests for the laya-browser skill against live public sites.

Each case runs laya_browse.py in a fresh process and asserts on objective outcomes
(final URL, run status, classify label), never on model confidences.
Run: ~/jarvis/laya/.venv/bin/python run_tests.py [-k substring]
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "laya_browse.py")
PY = sys.executable


def run(args, timeout=240):
    t0 = time.time()
    p = subprocess.run([PY, SCRIPT, *args], capture_output=True, text=True, timeout=timeout)
    return p, time.time() - t0


def classify(url):
    p, dt = run(["classify", url, "--json"])
    body = p.stdout[p.stdout.index("{"):] if "{" in p.stdout else "{}"
    return json.loads(body), dt, p


def browse(url, goal, out, extra=()):
    p, dt = run(["browse", url, "--goal", goal, "--out", out, *extra])
    tp = os.path.join(out, "trace.json")
    trace = json.load(open(tp)) if os.path.exists(tp) else {"status": "no-trace", "steps": []}
    return trace, dt, p


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn)); return fn
    return deco


@case("classify: GitHub login page -> login_or_signup")
def _(tmp):
    r, dt, p = classify("https://github.com/login")
    assert r["answers"]["page_type"]["choice"] == "login_or_signup", r["answers"]
    return f"page_type={r['answers']['page_type']} latency={r['latency_ms']}ms", dt


@case("classify: Wikipedia Python article -> article")
def _(tmp):
    r, dt, p = classify("https://en.wikipedia.org/wiki/Python_(programming_language)")
    assert r["answers"]["page_type"]["choice"] == "article", r["answers"]
    return f"page_type={r['answers']['page_type']} region={r['region']}", dt


@case("browse: Wikipedia 'Programming language' -> Python article")
def _(tmp):
    t, dt, p = browse("https://en.wikipedia.org/wiki/Programming_language",
                      "Open the Wikipedia article about the Python programming language", tmp,
                      ["--stop-when", "Python (programming language)"])
    assert t["status"] == "done", (t["status"], p.stdout[-800:])
    assert "Python_(programming_language)" in t["final_url"], t["final_url"]
    s = t["steps"][0]
    return f"steps={len(t['steps'])} pick={s['action']!r} p={s['action_p']}", dt


@case("browse: python.org -> downloads page")
def _(tmp):
    t, dt, p = browse("https://www.python.org", "Open the downloads page", tmp, ["--stop-when", "/downloads"])
    assert t["status"] == "done", (t["status"], p.stdout[-800:])
    assert "/downloads" in t["final_url"], t["final_url"]
    s = t["steps"][0]
    return f"steps={len(t['steps'])} pick={s['action']!r} p={s['action_p']}", dt


@case("browse: Hacker News -> newest submissions")
def _(tmp):
    t, dt, p = browse("https://news.ycombinator.com", "Open the list of newest submissions", tmp,
                      ["--stop-when", "newest"])
    assert t["status"] == "done", (t["status"], p.stdout[-800:])
    assert "newest" in t["final_url"], t["final_url"]
    s = t["steps"][0]
    return f"steps={len(t['steps'])} pick={s['action']!r} p={s['action_p']}", dt


@case("browse: Wikipedia main page -> Current events portal")
def _(tmp):
    t, dt, p = browse("https://en.wikipedia.org/wiki/Main_Page", "Go to the Current events page", tmp,
                      ["--stop-when", "Current_events"])
    assert t["status"] == "done", (t["status"], p.stdout[-800:])
    assert "Current_events" in t["final_url"], t["final_url"]
    s = t["steps"][0]
    return f"steps={len(t['steps'])} pick={s['action']!r} p={s['action_p']}", dt


@case("scope: off-host link is refused, browser stays on example.com")
def _(tmp):
    t, dt, p = browse("https://example.com", "Open the more information link", tmp, ["--max-steps", "2"])
    assert t["status"] == "blocked", (t["status"], p.stdout[-800:])
    assert "example.com" in t["final_url"], t["final_url"]
    assert "iana.org" in t["steps"][-1]["result"], t["steps"][-1]
    return t["steps"][-1]["result"], dt


@case("typing: refused without --allow-typing")
def _(tmp):
    t, dt, p = browse("https://en.wikipedia.org/wiki/Main_Page",
                      'Use the search box to search for "Transformer (deep learning architecture)"', tmp,
                      ["--max-steps", "2"])
    assert t["status"] == "blocked", (t["status"], p.stdout[-800:])
    assert "typing not allowed" in t["steps"][-1]["result"], t["steps"][-1]
    return f"pick={t['steps'][-1]['action']!r} -> {t['steps'][-1]['result']}", dt


@case("typing: Wikipedia search with --allow-typing reaches the article")
def _(tmp):
    t, dt, p = browse("https://en.wikipedia.org/wiki/Main_Page",
                      'Use the search box to search for "Transformer (deep learning architecture)"', tmp,
                      ["--allow-typing", "--stop-when", "Transformer"])
    assert t["status"] == "done", (t["status"], p.stdout[-800:])
    assert "Transformer" in t["final_url"], t["final_url"]
    s = t["steps"][0]
    return f"steps={len(t['steps'])} pick={s['action']!r} -> {s['result']}", dt


def main():
    sel = sys.argv[sys.argv.index("-k") + 1] if "-k" in sys.argv else ""
    root = tempfile.mkdtemp(prefix="laya-browser-tests-")
    results = []
    for i, (name, fn) in enumerate(CASES):
        if sel and sel not in name:
            continue
        out = os.path.join(root, f"case{i}")
        try:
            detail, dt = fn(out)
            results.append(("PASS", name, detail, dt))
        except AssertionError as e:
            results.append(("FAIL", name, str(e)[:300], 0))
        except Exception as e:  # noqa: BLE001
            results.append(("ERROR", name, f"{type(e).__name__}: {str(e)[:300]}", 0))
        print(f"{results[-1][0]:5s} {name}\n      {results[-1][2]}  ({results[-1][3]:.0f}s)", flush=True)
    npass = sum(r[0] == "PASS" for r in results)
    print(f"\n{npass}/{len(results)} passed   artifacts: {root}")
    sys.exit(0 if npass == len(results) else 1)


if __name__ == "__main__":
    main()
