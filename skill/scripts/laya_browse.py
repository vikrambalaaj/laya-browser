#!/usr/bin/env python
"""Laya-driven browser use.

Two modes:
  classify URL            load a page and answer typed questions about it
  browse URL --goal TEXT  step through a site; Laya picks each action from a
                          shortlist of on-page elements until it judges the goal met

Laya never generates text. In `browse`, the only text ever typed into a field is a
string quoted inside --goal, and only with --allow-typing. Navigation is limited to
--allowed-hosts (default: the start host); off-scope document loads are aborted.
"""
import argparse
import json
import os
import re
import sys
import time
from urllib.parse import urlparse

LAYA_REPO = "convaiinnovations/laya"
MAX_CANDIDATES = 1500       # elements collected from the DOM
LEXICAL_KEEP = 40           # kept after cheap goal-word overlap, before the embedding shortlist
STOPWORDS = set("a an the of to in on for and or with about open find go click page site show me "
                "wikipedia article link search press visit read get into from this that is are be".split())
EXCERPT_CHARS = 1500       # classify
BROWSE_EXCERPT_CHARS = 700  # browse: shorter state keeps per-step latency down
FINAL_TEXT_CHARS = 6000     # page text returned with the trace as evidence

CLASSIFY_QUESTIONS = {
    "page_type": {
        "type": "choice",
        "instructions": "What kind of web page is this?",
        "criteria": {
            "article": "an encyclopedia entry, news story, blog post or long-form text",
            "homepage": "a site front page or navigation hub linking to many sections",
            "search_results": "a list of search results or a feed of many items",
            "login_or_signup": "a page whose main purpose is to sign in or create an account",
            "form": "a page whose main purpose is to fill in and submit a form",
            "product_or_pricing": "a product, plan or pricing page",
            "documentation": "technical documentation or reference material",
            "error": "a 404, access denied or other error page",
            "other": "none of the above",
        },
    },
    "requires_login": {"type": "noul",
                       "instructions": "Does the page ask the visitor to log in before continuing?"},
    "has_prices": {"type": "noul",
                   "instructions": "Does the page show prices or paid plans?"},
    "content_depth": {"type": "score",
                      "instructions": "How much substantive content does the page have?",
                      "criteria": ["almost none", "a little", "a moderate amount", "a lot"]},
}

COLLECT_JS = r"""
(max) => {
  const sel = 'a[href], button, input, textarea, select, [role=button], [role=link], [role=searchbox], summary';
  const out = []; let n = 0;
  const seen = new Set();
  for (const el of document.querySelectorAll(sel)) {
    if (out.length >= max) break;
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || style.visibility === 'hidden' || style.display === 'none') continue;
    if (el.type === 'hidden') continue;
    const tag = el.tagName.toLowerCase();
    let kind = tag === 'a' ? 'link' : tag === 'button' ? 'button' :
      (tag === 'input' || tag === 'textarea' || el.getAttribute('role') === 'searchbox') ? 'input' :
      el.getAttribute('role') || tag;
    if (tag === 'input' && ['submit','button','image'].includes(el.type)) kind = 'button';
    if (tag === 'input' && !['text','search','email','url','number','tel',''].includes(el.type || '')) continue;
    let text = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                el.getAttribute('title') || el.getAttribute('alt') || '').replace(/\s+/g, ' ').trim();
    if (!text && tag === 'a') text = (el.getAttribute('href') || '').slice(0, 60);
    if (!text) continue;
    text = text.slice(0, 80);
    const href = tag === 'a' ? el.href : '';
    const key = kind + '|' + text + '|' + href;
    if (seen.has(key)) continue;
    seen.add(key);
    const id = 'laya-' + (n++);
    el.setAttribute('data-laya-id', id);
    out.push({id, kind, text, href, inViewport: r.top >= 0 && r.top < innerHeight});
  }
  return out;
}
"""

EXCERPT_JS = r"""
(chars) => {
  // Prefer the main content region so site chrome (menus, "Log in" links) does not
  // dominate the excerpt; fall back to <body> when no region is larger.
  const regions = ['main','article','[role=main]','#content','#mw-content-text']
    .map(s => document.querySelector(s)).filter(Boolean);
  let el = document.body, best = 0;
  for (const c of regions) { const l = (c.innerText || '').length; if (l > best) { best = l; el = c; } }
  const heads = [...el.querySelectorAll('h1,h2,h3')].slice(0, 8)
    .map(h => h.innerText.replace(/\s+/g,' ').trim()).filter(Boolean);
  const body = (el.innerText || '').replace(/\s+/g, ' ').trim();
  return {title: document.title, headings: heads, text: body.slice(0, chars),
          chars: body.length, lang: document.documentElement.lang || '',
          region: el === document.body ? 'body' : (el.id ? '#' + el.id : el.tagName.toLowerCase())};
}
"""


def log(msg):
    print(msg, flush=True)


def load_agent(device=None):
    import laya
    t0 = time.perf_counter()
    agent = laya.load(LAYA_REPO, subfolder="multilingual", device=device)
    log(f"[laya] multilingual checkpoint loaded on {agent.device} in {time.perf_counter()-t0:.1f}s")
    return agent


def host_of(url):
    return (urlparse(url).hostname or "").lower()


def host_allowed(host, allowed):
    return any(host == h or host.endswith("." + h) for h in allowed)


def launch_browser(pw, headed=False):
    return pw.chromium.launch(headless=not headed)


def new_page(browser, allowed_hosts):
    """Fresh isolated context + page on an already-running browser, with the host gate."""
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    blocked = []

    def gate(route, request):
        if request.resource_type == "document" and not host_allowed(host_of(request.url), allowed_hosts):
            blocked.append(request.url)
            return route.abort()
        return route.continue_()

    ctx.route("**/*", gate)
    page = ctx.new_page()
    page.set_default_timeout(20000)
    return ctx, page, blocked


def open_browser(pw, allowed_hosts, headed=False):
    browser = launch_browser(pw, headed)
    ctx, page, blocked = new_page(browser, allowed_hosts)
    return browser, page, blocked


def snapshot(page, chars=EXCERPT_CHARS):
    info = page.evaluate(EXCERPT_JS, chars)
    info["url"] = page.url
    return info


# --------------------------------------------------------------------------- classify

def cmd_classify(args):
    from playwright.sync_api import sync_playwright
    agent = load_agent(args.device)
    with sync_playwright() as pw:
        browser = launch_browser(pw, args.headed)
        try:
            return classify_once(agent, browser, args)
        finally:
            browser.close()


def classify_once(agent, browser, args):
    """Classify one page on an already-running browser. `args` needs url, questions
    (path or None), max_len, json; a SimpleNamespace is fine."""
    questions = CLASSIFY_QUESTIONS
    if getattr(args, "questions", None):
        if isinstance(args.questions, dict):
            questions = args.questions
        else:
            with open(args.questions) as f:
                questions = json.load(f)
    allowed = [host_of(args.url)]
    if True:  # context on the caller's browser
        ctx, page, _ = new_page(browser, allowed)
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_timeout(500)
        info = snapshot(page)
        ctx.close()

    state = {"title": info["title"], "text": info["text"]}
    agent.predict({"title": "warm-up", "text": "warm-up"}, questions, max_len=args.max_len)
    t0 = time.perf_counter()
    res = agent.predict(state, questions, max_len=args.max_len)
    ms = (time.perf_counter() - t0) * 1000
    out = {"url": info["url"], "title": info["title"], "page_chars": info["chars"],
           "region": info["region"], "excerpt_chars": len(info["text"]),
           "latency_ms": round(ms, 1), "answers": {}}
    for qid, a in res["answers"].items():
        if a["type"] == "choice":
            out["answers"][qid] = {"choice": a["choice"], "p": round(a["answer_confidence"], 3)}
        elif a["type"] == "noul":
            out["answers"][qid] = {"yes_p": round(a["noul"], 3)}
        elif a["type"] == "score":
            out["answers"][qid] = {"score": round(a["score"], 2),
                                   "label": a["legend"][str(int(round(a["score"])))]}
    if getattr(args, "json", False):
        print(json.dumps(out, indent=2, ensure_ascii=False))
    elif getattr(args, "quiet", False):
        pass
    else:
        log(f"\n{out['title']!r}  ({out['url']})")
        log(f"  region={out['region']}  page_chars={out['page_chars']}  "
            f"excerpt={out['excerpt_chars']} chars  laya_latency={out['latency_ms']} ms (warm)")
        for qid, a in out["answers"].items():
            log(f"  {qid:16s} {a}")
    return out


# --------------------------------------------------------------------------- browse

def quoted_text(goal):
    m = re.search(r'["“]([^"”]+)["”]', goal)
    return m.group(1) if m else None


def goal_terms(goal):
    return {w for w in re.findall(r"[\w']+", goal.lower()) if w not in STOPWORDS and len(w) > 2}


def lexical_prefilter(goal, candidates, keep, exclude=()):
    """Cheap first cut: rank by goal-word overlap with the element text, weighting rare
    goal words (idf over the candidate texts) above common ones, tie-break by DOM order
    with in-viewport elements first. Inputs get a bonus when the goal quotes text.
    `exclude` holds (url, text) pairs already tried without leaving the page."""
    import math
    terms = goal_terms(goal)
    wants_input = quoted_text(goal) is not None
    words_of = [set(re.findall(r"[\w']+", c["text"].lower())) for c in candidates]
    n = max(len(candidates), 1)

    def matches(t, words):
        return t in words or any(w.startswith(t) for w in words)

    df = {t: sum(1 for w in words_of if matches(t, w)) for t in terms}
    idf = {t: math.log((n + 1) / (df[t] + 1)) + 1 for t in terms}
    scored = []
    for i, (c, words) in enumerate(zip(candidates, words_of)):
        if c["text"] in exclude:
            continue
        score = sum(idf[t] for t in terms if matches(t, words))
        if wants_input and c["kind"] == "input":
            score += 2
        scored.append((-score, 0 if c["inViewport"] else 1, i, c))
    scored.sort(key=lambda t: t[:3])
    top = scored[:keep]
    return [t[3] for t in top], (-top[0][0] if top else 0.0)


def build_action_question(goal, candidates, embed_fn, k, exclude=()):
    """Offer Laya k element texts as a choice question.

    Measured on four page/goal pairs (see tests): the decision head picks the right
    element when the state is the bare goal string and every option is just the
    element's visible text. Adding page text to the state, href descriptions, or
    control options (scroll / back / done) all degraded the pick. Ranking into the
    shortlist is idf-weighted goal-word overlap; the Laya-encoder embedding shortlist
    is only the fallback when no element text shares a word with the goal.
    """
    from laya.shortlist import shortlist_choice
    pool, best = lexical_prefilter(goal, candidates, LEXICAL_KEEP, exclude)
    crit, by_label = {}, {}
    for c in pool:
        label = c["text"][:60]
        if label in crit:
            continue                      # same visible text: keep the first (DOM order)
        crit[label] = label
        by_label[label] = c
    if best > 0:
        kept = list(crit)[:k]
        how = "lexical"
    else:
        kept = shortlist_choice(goal, crit, embed_fn, k=k,
                                instructions="Which page element helps achieve this goal?")
        how = "embedding"
    q = {"type": "choice",
         "instructions": "Which element should the browser click next to achieve the goal?",
         "criteria": {lab: lab for lab in kept}}
    return q, by_label, kept, how


def same_page(a, b):
    pa, pb = urlparse(a), urlparse(b)
    return (pa.netloc, pa.path.rstrip("/"), pa.query) == (pb.netloc, pb.path.rstrip("/"), pb.query)


def stop_matches(pattern, info):
    if not pattern:
        return False
    pat = pattern.lower()
    return pat in info["url"].lower() or pat in info["title"].lower()


def cmd_browse(args):
    from playwright.sync_api import sync_playwright
    from laya.shortlist import embed_fn_from_agent

    agent = load_agent(args.device)
    embed_fn = embed_fn_from_agent(agent)
    with sync_playwright() as pw:
        browser = launch_browser(pw, args.headed)
        try:
            return browse_once(agent, embed_fn, browser, args)
        finally:
            browser.close()


def browse_once(agent, embed_fn, browser, args):
    """Run one goal on an already-running browser and return the trace.

    `args` needs: url, goal, max_steps, k, allowed_hosts (csv string or list or None),
    allow_typing, done_threshold, stop_when, out, max_len. A SimpleNamespace is fine.
    """
    hosts = args.allowed_hosts
    if isinstance(hosts, (list, tuple)):
        hosts = ",".join(hosts)
    allowed = [h.strip().lower() for h in (hosts or host_of(args.url)).split(",") if h.strip()]
    typed = quoted_text(args.goal)
    os.makedirs(args.out, exist_ok=True)
    trace = {"goal": args.goal, "start_url": args.url, "allowed_hosts": allowed, "steps": []}
    status = "max_steps"
    tried = {}   # url -> set of element texts clicked there (never offered twice on that page)
    typed_done = False

    if True:  # context on the caller's browser
        ctx, page, blocked = new_page(browser, allowed)
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_timeout(600)

        for step in range(1, args.max_steps + 1):
            try:
                info = snapshot(page, BROWSE_EXCERPT_CHARS)
                candidates = page.evaluate(COLLECT_JS, MAX_CANDIDATES)
                shot = os.path.join(args.out, f"step_{step:02d}.png")
                page.screenshot(path=shot)
            except Exception as e:  # noqa: BLE001  (window closed by the user, tab crashed)
                log(f"\n[step {step}] browser unavailable: {type(e).__name__}: {str(e)[:100]}")
                status = "browser_closed"
                break

            if stop_matches(args.stop_when, info):
                log(f"\n[step {step}] stop-when {args.stop_when!r} matched: {info['title']!r}  {info['url']}")
                trace["steps"].append({"step": step, "url": info["url"], "title": info["title"],
                                       "result": "stop_when matched", "screenshot": shot})
                status = "done"
                break

            # A goal with "quoted text" means a typing step comes first, as soon as a page
            # offers an input. Laya picks which input; the quoted text is the only thing typed.
            if typed and not typed_done:
                inputs = {}
                for c in candidates:
                    if c["kind"] == "input" and c["text"][:60] not in inputs:
                        inputs[c["text"][:60]] = c
                if inputs:
                    if not args.allow_typing:
                        rec = {"step": step, "url": info["url"], "title": info["title"],
                               "inputs": list(inputs), "action": None,
                               "result": "refused: typing not allowed (pass --allow-typing to type the quoted text)",
                               "screenshot": shot}
                        log(f"\n[step {step}] {info['title']!r}  {info['url']}\n  " + rec["result"])
                        trace["steps"].append(rec)
                        status = "blocked"
                        break
                    q = {"field": {"type": "choice",
                                   "instructions": "Which input field should receive the quoted text?",
                                   "criteria": {k: k for k in inputs}}}
                    t0 = time.perf_counter()
                    a = agent.predict(args.goal, q, max_len=args.max_len)["answers"]["field"]
                    t_pred = (time.perf_counter() - t0) * 1000
                    c = inputs[a["choice"]]
                    rec = {"step": step, "url": info["url"], "title": info["title"],
                           "inputs": list(inputs), "action": a["choice"],
                           "action_p": round(a["answer_confidence"], 3), "predict_ms": round(t_pred, 1),
                           "screenshot": shot}
                    log(f"\n[step {step}] {info['title']!r}  {info['url']}")
                    log(f"  inputs={list(inputs)}  laya {t_pred:.0f} ms  pick={a['choice']!r} p={a['answer_confidence']:.2f}")
                    try:
                        loc = page.locator(f'[data-laya-id="{c["id"]}"]').first
                        loc.click(timeout=8000); loc.fill(typed); loc.press("Enter")
                        page.wait_for_load_state("domcontentloaded"); page.wait_for_timeout(700)
                        rec["result"] = f"typed {typed!r} into {c['text']!r} + Enter"
                        typed_done = True
                    except Exception as e:  # noqa: BLE001
                        rec["result"] = f"error: {type(e).__name__}: {str(e)[:120]}"
                    log("  -> " + rec["result"])
                    trace["steps"].append(rec)
                    continue

            t0 = time.perf_counter()
            action_q, by_label, kept, how = build_action_question(
                args.goal, candidates, embed_fn, args.k, tried.get(info["url"], set()))
            t_short = (time.perf_counter() - t0) * 1000
            questions = {"next_element": action_q}
            t0 = time.perf_counter()
            res = agent.predict(args.goal, questions, max_len=args.max_len)
            adv = agent.predict({"goal": args.goal, "current_page_title": info["title"]},
                                {"goal_done": {"type": "noul",
                                               "instructions": "Is the goal already achieved on the current page?"}},
                                max_len=512)
            t_pred = (time.perf_counter() - t0) * 1000
            a = res["answers"]["next_element"]
            goal_p = adv["answers"]["goal_done"]["noul"]
            choice, choice_p = a["choice"], a["answer_confidence"]
            top3 = sorted(a["probabilities"].items(), key=lambda kv: -kv[1])[:3]

            rec = {"step": step, "url": info["url"], "title": info["title"],
                   "candidates": len(candidates), "shortlist_by": how, "shortlist": kept[:args.k],
                   "goal_done_p_advisory": round(goal_p, 3), "action": choice, "action_p": round(choice_p, 3),
                   "top3": [(l, round(p, 3)) for l, p in top3],
                   "shortlist_ms": round(t_short, 1), "predict_ms": round(t_pred, 1),
                   "screenshot": shot}
            log(f"\n[step {step}] {info['title']!r}  {info['url']}")
            log(f"  candidates={len(candidates)} -> shortlist={len(kept)} ({how})  "
                f"shortlist {t_short:.0f} ms, laya {t_pred:.0f} ms")
            log(f"  pick={choice!r} p={choice_p:.2f}   top3={rec['top3']}   goal_done(advisory)={goal_p:.2f}")

            c = by_label[choice]
            if c["href"] and same_page(c["href"], info["url"]):
                rec["result"] = "done: Laya chose the link to the page already open"
                log("  -> " + rec["result"])
                trace["steps"].append(rec)
                status = "done"
                break
            if args.done_threshold is not None and goal_p >= args.done_threshold:
                rec["result"] = f"done: advisory goal_done {goal_p:.2f} >= {args.done_threshold}"
                log("  -> " + rec["result"])
                trace["steps"].append(rec)
                status = "done"
                break

            try:
                if True:
                    tried.setdefault(info["url"], set()).add(c["text"])
                    loc = page.locator(f'[data-laya-id="{c["id"]}"]').first
                    if c["kind"] == "input":
                        rec["result"] = "refused: no quoted text in goal to type into this field"
                        log("  " + rec["result"])
                        trace["steps"].append(rec)
                        status = "blocked"
                        break
                    else:
                        if c["href"] and not host_allowed(host_of(c["href"]), allowed):
                            rec["result"] = f"refused: {host_of(c['href'])} not in allowed_hosts"
                            log("  " + rec["result"])
                            trace["steps"].append(rec)
                            status = "blocked"
                            break
                        if c["href"] and c["href"].startswith(("http://", "https://")):
                            # Links are followed by href: deterministic, and immune to
                            # anchors that Playwright considers non-actionable (overlays,
                            # collapsed sections, zero-size wrappers).
                            page.goto(c["href"], wait_until="domcontentloaded")
                            rec["result"] = f"followed link {c['text']!r} -> {c['href'][:80]}"
                        else:
                            loc.scroll_into_view_if_needed(); loc.click(timeout=8000)
                            rec["result"] = f"clicked {c['kind']} {c['text']!r}"
                    page.wait_for_load_state("domcontentloaded"); page.wait_for_timeout(700)
            except Exception as e:  # noqa: BLE001
                rec["result"] = f"error: {type(e).__name__}: {str(e)[:120]}"
            log("  -> " + rec["result"])
            trace["steps"].append(rec)

        try:
            final = snapshot(page, FINAL_TEXT_CHARS)
            page.screenshot(path=os.path.join(args.out, "final.png"))
        except Exception:  # noqa: BLE001
            last = trace["steps"][-1] if trace["steps"] else {}
            final = {"url": last.get("url", ""), "title": last.get("title", ""), "text": ""}
        if status == "max_steps" and stop_matches(args.stop_when, final):
            status = "done"
        trace.update({"status": status, "final_url": final["url"], "final_title": final["title"],
                      "final_text": final.get("text", ""), "blocked_requests": blocked[:10]})
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass

    with open(os.path.join(args.out, "trace.json"), "w") as f:
        json.dump(trace, f, indent=2, ensure_ascii=False)
    log(f"\nstatus={status}  final={final['title']!r}  {final['url']}")
    log(f"trace: {os.path.join(args.out, 'trace.json')}")
    return trace


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--device", default=None, help="torch device (default: auto, mps on Apple)")
    p.add_argument("--headed", action="store_true", help="show the browser window")
    p.add_argument("--max-len", type=int, default=2048, help="Laya token budget for the state")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("classify", help="answer typed questions about one page")
    c.add_argument("url")
    c.add_argument("--questions", help="JSON file of Laya questions (default: built-in set)")
    c.add_argument("--json", action="store_true")

    b = sub.add_parser("browse", help="let Laya drive the browser toward a goal")
    b.add_argument("url")
    b.add_argument("--goal", required=True)
    b.add_argument("--max-steps", type=int, default=4)
    b.add_argument("--k", type=int, default=10, help="shortlist size offered to Laya per step")
    b.add_argument("--allowed-hosts", help="comma-separated hostnames (default: start host)")
    b.add_argument("--allow-typing", action="store_true",
                   help='permit typing the "quoted" text from --goal into an input')
    b.add_argument("--stop-when", help="case-insensitive substring of the URL or title that marks success")
    b.add_argument("--done-threshold", type=float, default=None,
                   help="also stop when the goal_done probability reaches this value; off by default "
                        "because the zero-shot yes/no head is biased toward yes")
    b.add_argument("--out", default="laya-browse-out")

    args = p.parse_args(argv)
    return cmd_classify(args) if args.cmd == "classify" else cmd_browse(args)


if __name__ == "__main__":
    main()
