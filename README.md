# laya-browser

Browser use driven by [Laya](https://huggingface.co/convaiinnovations/laya), Convai Innovations'
open-weight (Apache-2.0) System 1 decision model. Laya answers typed questions
(choice / score / yes-no) over a state in one forward pass, around 100 ms on an Apple M1,
in 100+ languages, and never generates text. This repo pairs it with Playwright so Laya picks
*which on-page element* to act on while deterministic code handles navigation, host scope,
typing and stop rules.

Three things are here:

- `laya/`: the warm service (`laya_browserd.py`), its MCP server, the `lb` CLI, and the
  Jev Browser bridge. See "Warm service" below.
- `skill/`: a Claude Code skill (`SKILL.md` + `scripts/laya_browse.py` + live tests). Drop the
  folder into `.claude/skills/laya-browser/`.
- `laya/smoke_test.py`: a three-language sanity check for a local Laya install.

## Install

Python 3.10+ and roughly 1.5 GB of disk (torch plus the 644 MB multilingual checkpoint).

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python laya playwright
.venv/bin/python -m playwright install chromium     # skip if Playwright's Chromium is already cached
.venv/bin/python laya/smoke_test.py multilingual     # downloads the multilingual checkpoint on first run
```

Only the `multilingual` checkpoint (322M params) is used. `Router()` defaults to the English
checkpoint, so the scripts load `multilingual` explicitly; use `Router(default="multilingual")`
in your own code or it will fetch another 845 MB.

## Use

```bash
PY=.venv/bin/python; S=skill/scripts/laya_browse.py

# classify one page with typed questions
$PY $S classify https://github.com/login

# let Laya drive toward a goal, stop when the URL or title matches
$PY $S browse https://www.python.org --goal "Open the downloads page" --stop-when "/downloads"

# a quoted string in the goal is typed into the input Laya picks (only with --allow-typing)
$PY $S browse https://en.wikipedia.org/wiki/Main_Page \
  --goal 'Use the search box to search for "Transformer (deep learning architecture)"' \
  --allow-typing --stop-when "Transformer"
```

Each `browse` step collects every visible link, button and input, ranks them by idf-weighted
overlap with the goal words (Laya-encoder embedding shortlist as the fallback), and asks Laya one
choice question: state = the goal, options = the top `--k` element texts. Links are followed by
href, buttons clicked. Host scope is `--allowed-hosts` (default: the start host); off-scope
document loads are aborted at the network layer. Output: `step_NN.png`, `final.png`, `trace.json`.

## What the measurements say (M1, MPS, 2026-10-02)

| | result |
|---|---|
| Element pick, goal-only state, text-only options | correct on 4/4 goals, confidence 0.71 to 1.00 |
| Same with page text in the state or scroll/back/done options | degraded; not used |
| Zero-shot "is the goal reached" yes/no | best phrasing 6/8; advisory only |
| `classify` page_type | login page 0.99, encyclopedia article 1.00; weak on a bare form or a link aggregator |
| Latency per Laya decision, warm | 70 to 330 ms |

Stop rules are therefore objective: `--stop-when` substring, Laya choosing a link to the page
already open, or `--max-steps` (default 4). Yes-no questions lean yes on the base checkpoint;
prefer two-option choice questions or fine-tune (Laya ships a notebook).

## Tests

```bash
.venv/bin/python skill/tests/run_tests.py        # 9 live cases, ~2 min, asserts on URLs/statuses
```

9/9 passing on 2026-10-02: two classify cases, four navigation goals (Wikipedia x2, python.org,
Hacker News), an off-host refusal, a typing refusal without the flag, and a typed Wikipedia search.

## Warm service: `laya_browserd`

`laya/laya_browserd.py` keeps one Laya model and one Chromium resident behind a loopback HTTP
API (default 127.0.0.1:8797), so a goal runs in about 5 s end to end instead of paying ~8 s of
model load and ~2 s of browser launch every call. It serves:

| route | what |
|---|---|
| `POST /browse` | the skill's goal loop: `{url, goal, stop_when, allowed_hosts, allow_typing, max_steps, headed}` |
| `POST /classify` | typed questions about one page |
| `POST /v1/systemone` | TypeSafe protocol for Jev Browser, with the shortlist rewrite applied in-process |
| `GET /health` | model, device, browser state |

```bash
laya/browserd.sh start          # or `install` to register a launchd agent that keeps it warm from login
laya/lb "Open the downloads page" https://www.python.org --stop-when /downloads
laya/lb classify https://github.com/login
```

`laya/laya_mcp.py` is an MCP stdio server exposing `laya_navigate` and `laya_classify` to Claude
Code; it starts the service on demand. Example `.mcp.json`:

```json
{"mcpServers": {
  "laya-browser": {"command": "<repo>/.venv/bin/python", "args": ["<repo>/laya/laya_mcp.py"]},
  "jev-browser":  {"command": "<repo>/laya/jev-laya", "args": []}}}
```

Measured 2026-10-03 on an M1, warm service, four navigation goals, all landed in one step:

| task | elapsed | pick |
|---|---|---|
| python.org downloads | 6.9 s | Downloads, p=1.00 |
| python.org community | 4.8 s | Community, p=0.999 |
| Wikipedia current events | 5.1 s | More current events, p=0.79 |
| Hacker News newest | 9.9 s | new, p=0.89 |

Cold standalone runs of the same goals took 17 to 50 s each.

## Jev Browser on Laya

Laya speaks the TypeSafe `/v1/systemone` protocol that
[Jev Browser](https://github.com/jkudish/jev-browser) uses for its decision model, so Jev Browser
runs on the same warm service with no cloud key. `laya/jev-laya` is a drop-in for `jev-browser`
that starts the service and sets `JEV_PROVIDER=typesafe`, `TYPESAFE_BASE_URL`, and
`JEV_BROWSER_MODEL=multilingual`.

`laya/jev_laya_proxy.py` holds the rewrite the service applies to each Jev step: keep the 12 page
elements whose labels best overlap the task words, ask Laya with the bare task as state and
label-only options (the format that measured 4/4 above), ask Jev's goal/stuck questions
separately with page context, hide scroll/back/done from the pick, and report arrival when the
landed page's title or URL carries the task words. Without it Laya's pick over 126 raw options
was flat (top probability 0.03) and it declared done on the start page.

```bash
laya/jev-laya run "Open the downloads page" https://www.python.org --format markdown
```

Measured 2026-10-03 through Jev: python.org downloads and community, Wikipedia current events
and Hacker News "newest page" each landed in one step (p 0.66 to 1.0). Two misses are structural:
"Open the newest submissions list" picks Submit, and links deep in long articles never reach the
model because Jev offers only the first 200 page elements. The service always answers with the
multilingual checkpoint; Jev's unknown model name would otherwise make `laya-serve` auto-route
English text to the English checkpoint and download it.

## License

MIT for the code here. Laya weights are Apache-2.0 from Convai Innovations.
