# laya-browser

Browser use driven by [Laya](https://huggingface.co/convaiinnovations/laya), Convai Innovations'
open-weight (Apache-2.0) System 1 decision model. Laya answers typed questions
(choice / score / yes-no) over a state in one forward pass, around 100 ms on an Apple M1,
in 100+ languages, and never generates text. This repo pairs it with Playwright so Laya picks
*which on-page element* to act on while deterministic code handles navigation, host scope,
typing and stop rules.

Two things are here:

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

## Jev Browser on Laya

Laya's server speaks the TypeSafe `/v1/systemone` protocol that
[Jev Browser](https://github.com/jkudish/jev-browser) uses for its decision model, so Jev Browser
can run on local Laya with no cloud key. Three small files in `laya/` make it work well:

- `serve_laya.py` runs `laya-serve` with the choice-option cap raised (upstream caps a question
  at 100 options; Jev offers up to 200 page elements).
- `jev_laya_proxy.py` sits between Jev and Laya (127.0.0.1:8798 -> 8799). Per step it keeps the
  12 page elements whose labels best overlap the task words, asks Laya with the bare task as state
  and label-only options (the format that measured 4/4 above), asks Jev's goal/stuck questions
  separately with page context, hides scroll/back/done from the pick, and reports arrival when
  the landed page's title or URL carries the task words. Without it Laya's pick over 126 raw
  options was flat (top probability 0.03) and it declared done on the start page.
- `serve-laya.sh start|stop|status|logs` runs both, loopback only, multilingual checkpoint only.
  `jev-laya` is a drop-in for `jev-browser` that starts them on demand and sets
  `JEV_PROVIDER=typesafe`, `TYPESAFE_BASE_URL`, and `JEV_BROWSER_MODEL=multilingual`.

```bash
laya/jev-laya run "Open the downloads page" https://www.python.org --format markdown
```

For Claude Code, point `.mcp.json` at `laya/jev-laya` with no args and the `jev_navigate` MCP
tool runs on Laya.

Measured 2026-10-03 (M1, about 0.5 s per decision):

| task | result |
|---|---|
| python.org: "Open the downloads page" | 1 step, p=1.00, arrived |
| python.org: "Open the community page" | 1 step, p=0.90, arrived |
| Wikipedia main page: "Go to the Current events page" | 1 step, p=0.96, arrived |
| Hacker News: "Open the newest page" | 1 step, p=0.66, arrived |
| Hacker News: "Open the newest submissions list" | wrong: picked Submit |
| Wikipedia article: "Open the History of programming languages article" | lost: link is past Jev's 200-element cap |

Two caveats worth knowing. Jev sends an unknown model name (`jev-latest`); without an explicit
`model`, `laya-serve` auto-routes English text to the English checkpoint and downloads it, so
the wrapper always sets `JEV_BROWSER_MODEL=multilingual`. And phrase tasks with the target page's
own words: Laya is a decision model, not a reader, and wording that overlaps a wrong label wins.

## License

MIT for the code here. Laya weights are Apache-2.0 from Convai Innovations.
