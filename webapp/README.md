# webapp

The browser interface. It is the friendliest way to do the **review workflow**:
read a Screen row *inside* its sources, correct cells, and promote it to Verify
with the reason recorded.

If you only want to *read* the data, you do not need this. Two other routes need
nothing installed at all — see [the three ways to see the
Tracker](../README.md#see-the-tracker) in the main README.

**Contents**

- [Run it](#run-it)
- [The review screen](#the-review-screen)
- [What each file holds](#what-each-file-holds)
- [How the pieces fit](#how-the-pieces-fit)

## Run it

The rest of the Tracker runs on the standard library. This is the one part
that needs packages installed: FastAPI, uvicorn and python-multipart.

From the parent directory (`tracker/`), not from here:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r pipeline/requirements.txt
python3 tracker.py webapp
```

Then open <http://localhost:8100>. Add `--reload` while editing, `--port` to
move it, and `--db PATH` to review a copy rather than the real database. Leave
the environment later with `deactivate`; `.venv/` is gitignored.

### On the virtual environment

It is a recommendation, not a requirement. Installing into your user site works
too, and `tracker.py webapp` finds the packages either way, because it calls
uvicorn in process rather than shelling out to the `uvicorn` script. That script
is the usual source of `command not found: uvicorn`: `pip install --user` puts it
somewhere like `~/Library/Python/3.9/bin`, which is often not on `PATH` even
though the library imported perfectly well.

What the environment buys you is keeping three packages out of the system
interpreter, which on macOS is Apple's and worth leaving alone.

By hand, without the command, still from the parent directory:

```bash
python3 -m uvicorn webapp.main:app --port 8100
```

Server-rendered HTML and plain form posts. No build step and no JavaScript
framework, so it works with the browser alone.

## The review screen

`/screen/N/inspect` — "Inspect & promote" — is what this app is for, and it is
built like a signing flow rather than like a form with links on it.

**The pages are in the screen, not behind it.** Every link the row cites becomes
a tab, and the page underneath renders inside the review screen. Two links in one
cell get two tabs; one page cited by two columns gets one tab, because that is
one document.

**The row's claims are highlighted in the page.** The announcement date, the
promised first-output date, the capital and the jobs on the promise side; the
actual first-output date and the status language on the status side. The
verbatim `*_raw` quote is looked for first, then the ways a publication would
actually print the value — `2022-01` finds "Jan. 21, 2022"; 1,600,000,000 finds
"$1.6 billion", "$1.6B" and "1,600,000,000".

**Arrows at the foot of the pane walk the highlights**, one at a time, with ← →
as well; a chip walks one cell only. The pane opens on the first highlight, so
the page arrives scrolled to the sentence in question.

**A page downloads once.** The first time a tab opens, the page is saved to
`scratch/pages/` (gitignored) and used from there for a week; **fetch again** in
the pane's top bar downloads it anew. Tick **Preload articles** at the foot of
any page and the app downloads every page the waiting projects cite each time it
starts, largest capital first, so nothing waits on a slow site mid-review. **Run
now** does it straight away and retries every page that failed, which is the
thing to press after turning on a VPN. The count beside it opens `/pages`, the
list of what could not be read. The checkbox is saved to `config.env`, so it is
a setting for this machine.

**Two checks, and they answer different questions.**

| | Asks | Costs |
|---|---|---|
| the deterministic check (`screen_check`, which *is* `pipeline/schema.py`) | is the row well-*formed*: real `YYYY-MM` anchor, sector in vocabulary, size floor cleared, dates parse, sources URL-shaped | nothing; blocks promotion on `FAIL` |
| the agentic check | does the cited page actually *say* this — and if not, where is the value | an API call; blocks nothing, stores nothing |

**A check keeps running across a reload, and a repeat answer is free.** It takes
20–60 seconds, and you can do whatever you want (e.g. reload) in the page in the meantime. 
Answers are kept in `outputs/agent_cache/` (gitignored), one JSON file each. The key is a
fingerprint of the row, the ticked cells, **their current values**, the `*_raw`
quotes, the source URLs and the model — so correcting `announced` after a check
does not serve the old "CONFIRMED" back for a value the row no longer holds.
Answers go stale after two weeks; a status page is cited because it changes.

## What each file holds

| File | Holds |
|---|---|
| `main.py` | creates the app, mounts the stage modules and the two panes, and serves the dashboard, the database picker, the preload checkbox and the list of saved pages |
| `shared.py` | the stylesheet, the page skeleton, the database picker bar, and the small formatting helpers every page uses |
| `source.py` | the Source pages: the lead list, the rendered collection prompt, and the three ways to add a lead |
| `screen.py` | the Screen pages: the row list, the extraction prompt, the checker with its rule-by-rule panel, and the review screen |
| `verify.py` | the Verify pages: the published rows, the capital/jobs filter, the sector vocabulary, and the edit form |
| `evidence.py` | the document pane: fetching a cited page, stripping it to safe text, and marking the row's claims in it |
| `page_cache.py` | the pages that pane has saved, and the preload that downloads them ahead of time |
| `agent.py` | the agentic-check pane: the cell picker, and the model's answer rendered |
| `agent_cache.py` | that check's background jobs and its cached answers — and what makes two questions the same question |

## How the pieces fit

Only `main.py` creates a server. Every other module collects its pages on a
`router`, which `main.py` mounts. So none of them runs on its own, and the
dependency runs one way with no cycles:

```
shared.py  <-  evidence.py / agent.py  <-  source.py / screen.py / verify.py  <-  main.py
```

`evidence.py` and `agent.py` are not stages: they render no rows of their own and
write nothing. Each serves a standalone page that the review screen embeds in an
`<iframe>`, which is the whole reason a fetch that takes ten seconds or a model
call that takes thirty never blocks the form — or costs a reviewer the
corrections they had half-typed into it.

The pipeline itself lives in
[`../pipeline/`](../pipeline/).
This directory only renders it; every write goes through the same functions the
CLI calls, so the two interfaces cannot drift.

Promotion to Verify is a human gate here as everywhere else, and every edit is
written to `verify_edits` with a required reason.
