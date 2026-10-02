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
[Jev Browser](https://github.com/jkudish/jev-browser) uses, so Jev Browser can run on local Laya:

```bash
LAYA_MODELS=multilingual LAYA_DEFAULT_MODEL=multilingual LAYA_HOST=127.0.0.1 LAYA_PORT=8799 laya-serve
JEV_PROVIDER=typesafe TYPESAFE_API_KEY=local TYPESAFE_BASE_URL=http://127.0.0.1:8799 \
  jev-browser run "Open the downloads page" https://www.python.org --format markdown
```

See `laya/` for the launcher used in this repo's own setup.

## License

MIT for the code here. Laya weights are Apache-2.0 from Convai Innovations.
