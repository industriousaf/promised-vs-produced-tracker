# Command reference

*Package: [`../pipeline/`](../pipeline/)*

Every pipeline step is one command. `python3 tracker.py --help` lists all 24
and carries worked examples for each; this file is the flat list, plus the three
things `--help` cannot hold: what needs installing, how the modules fit together,
and a walkthrough on a copy of the database.

> **This file is machine-read.** `collect/source.sh` attaches it to every
> `claude -p` process with an `@`-mention, and both operating prompts point at it
> as the command reference. Keep the command list complete: a process that cannot
> find a flag here will guess at one.

Run everything from the repository root. Two invocations, identical in effect:

```bash
python3 tracker.py <command>     # the documented entry point
python3 -m pipeline.cli <command>   # the long form, and what the prompts use
```

## What you need

| You want to… | Install | Key? |
|---|---|---|
| The pipeline core and CLI (every stage move, the checks, export, coverage) | **nothing**, Python 3.9+ stdlib | no |
| The web interface (`webapp`) | `pip install -r pipeline/requirements.txt` | no |
| The prompt path for the AI steps, with any assistant | nothing extra | **no** |
| The direct-API steps (`source-collect`, `screen-extract`, `tools/gather.py`) | `pip install anthropic` | yes, `ANTHROPIC_API_KEY` |
| The collection loops (`collect`) | the `claude` CLI, logged in | no |

`config.env` in this directory is optional: put `TRACKER_DB` or an API key
there and `tools/gather.py` reads it, though a real shell variable always wins.
It is gitignored. The database defaults to `outputs/tracker.db`.

## Every command

```bash
# Orientation
python3 tracker.py                       # counts, and where to go next
python3 tracker.py status                # row counts per stage
python3 tracker.py initdb                # create the six tables
python3 tracker.py config                # every setting in effect, with the line to edit
python3 tracker.py criteria              # what counts as a project (the inclusion rules)
python3 tracker.py models                # which model each stage runs, and why
python3 tracker.py quality               # five measures of whether the Tracker can carry the claim
python3 tracker.py --help                # all of the below, with examples

# Collect  (needs the claude CLI; spends money)
python3 tracker.py collect --n 5 --dry-run
python3 tracker.py collect --n 10 [--only source|screen] [--continue-on-fail]

# Source
python3 tracker.py source-add --promise URL --status URL [--summary "..."]
python3 tracker.py source-add --json lead.json [--via LABEL]   # or --json -
python3 tracker.py source-prompt                               # no key
python3 tracker.py source-collect                              # needs a key
python3 tracker.py source-list

# Screen
python3 tracker.py screen-prompt --source-id N                 # no key
python3 tracker.py screen-add --json row.json --source-id N
python3 tracker.py screen-extract --source-id N                # needs a key
python3 tracker.py screen-check --id N          # or --all
python3 tracker.py screen-list [--by-capital]
python3 tracker.py screen-show --id N
python3 tracker.py screen-date --id N --date "2021-12" --source URL [--raw "..."] [--note "..."]
python3 tracker.py screen-date --id N --unresolved "what was searched, and what was found"
python3 tracker.py screen-remove --id N --yes   # human only; no undo

# Verify  (the human gate)
python3 tracker.py review [--id N]              # guided, one row at a time
python3 tracker.py verify-promote --screen-id N --tier V1 [--flag "..."] [--set col=val] [--force]
python3 tracker.py verify-edit --id N --set col=val --desc "why"
python3 tracker.py verify-show --id N           # row + edit history
python3 tracker.py verify-list

# Read, export, measure
python3 tracker.py filter --capital 1000000000 --jobs 2000 --op OR --stage verify
python3 tracker.py filter --capital 5000000000 --jobs 5000 --op AND --stage screen
python3 tracker.py export [--out-dir DIR]       # five CSVs
python3 tracker.py coverage --against ref.csv [--stage verify] [--min-capital N]
python3 tracker.py coverage --selftest          # needs no database
python3 tracker.py recompute [--dry-run]        # re-derive lag/slip and the *_dt cells
python3 tracker.py count TABLE                  # one integer, for scripts

# The sector vocabulary  (closed; extending it means editing SECTORS in
# pipeline/settings.py — a human decision, never done from a collection call)
python3 tracker.py sectors-list

# The browser interface  (the review screen: sources rendered in the page,
# the row's claims highlighted in them, and an optional agentic check)
python3 tracker.py webapp [--port 8100] [--reload]

# Batch collection over the direct API  (needs a key)
python3 tools/gather.py --n-source 10 --n-screen 3 [--dry-run]

# Bulk CSV import  (rare; --promote-tier writes to Verify in bulk)
python3 tools/load_csv.py --csv rows.csv --dry-run
```

Any command takes a global `--db PATH` **before** the command, and every command
that changes the database refreshes `outputs/csv_tables/` as it closes.

## The modules behind the commands

Each stage is a small module the two interfaces share:

- `db.py` — the five-table SQLite schema, connection handling, and the
  connection subclass that keeps the CSV exports in step with the database.
- `source.py` — insert and list Source leads.
- `screen.py` — insert extracted rows; `run_check()` is Screen part two.
- `schema_check.py` — **is** `screen_check`: it loads the canonical `schema.py`
  and runs its row validator, returning the `FAIL / PASS / CLEAN` verdict and the
  issue list.
- `settings.py` — **every setting**: the inclusion phases (what counts as a
  project — the size floor, the date window, the countries, the sector
  vocabulary), which model runs each stage, and how a collection run behaves.
  `config` prints it all with the line to edit. If this file and any document
  disagree, this file is right.
- `schema.py` — **the definition of a well-formed row**: the columns and the row
  validator. Also runs standalone against a CSV. The rules it enforces come from
  `settings.py`.
- `verify.py` — `promote()` (the human gate) and `edit()`, which writes a
  `verify_edits` row in the **same transaction** as every Verify update.
- `export_tables.py` — database to CSV (`export`); also runs after every write.
- `coverage.py` — recall against a reference list (`coverage`).
- `orchestrate.py` — the moves between the stages: the AI runners and the
  explore-`filter`.
- `dates.py` — the deterministic date standardization. Each date is kept as a
  **`*_raw` → token → `*_dt`** chain: the extractor supplies the verbatim source
  text (`*_raw`) and a clean normalized token; this module resolves the token to
  a `*_dt` DATETIME (a fuzzy range becomes its healthy middle) and computes the
  float `lag_years` and `slip_years`, with four sentinels in place of a number:
  `-1` "to be completed" (not produced yet — the censored case), `-2`
  "cancelled", `-3` "no promise recorded" (slip only: it produced, but no
  source states what was promised), and `-4` "produced, date unknown" (the
  sources say it is producing but none dates first output).

  `-1` and `-4` both mean "no number", and the difference decides whether a row
  is right-censored. Only `-1` is. A survival analysis filtering on `< 0` will
  treat a mill in full operation as still waiting.
- `llm.py` — the AI steps in two flavours: rendered prompts (no key) and the
  direct API. The prompt builders never import the Anthropic SDK.

## The no-key path, with any assistant

The Source and Screen steps are meant for an assistant, but not for a particular
one. The pipeline renders the operating prompt, **you** run it wherever you like,
and paste back the JSON it returns. ChatGPT, Gemini, Perplexity and Claude all
work, and so does reading the prompt and doing the searching yourself.

**Source — find one new project:**

```bash
python3 tracker.py source-prompt          # prints it; already excludes what you have
# run it in an assistant that can search the web; it ends with one JSON object
python3 tracker.py source-add --json -    # paste the JSON, then Ctrl-D
```

**Screen — extract the row for a lead:**

```bash
python3 tracker.py screen-prompt --source-id 7   # the prompt, with the links filled in
# run it, then:
python3 tracker.py screen-add --json row.json --source-id 7
python3 tracker.py screen-check --id 11
```

In the web app the same flow is a button: Source → "Show Source prompt to run"
(copy, run it, paste the JSON into "Ingest lead JSON"); Screen → enter a Source
id → "Show Screen prompt to run" → paste the row JSON.

> The direct-API path (`source-collect`, `screen-extract`, `tools/gather.py`)
> does the same thing automatically through the Anthropic Messages API, using the
> web-search and web-fetch tools and a JSON-Schema structured-output call. It
> needs `ANTHROPIC_API_KEY`. The path above needs nothing.

## A walkthrough on a copy of the database

Everything below reads and writes a **copy**, so the committed database is
untouched. From the repository root:

```bash
cp outputs/tracker.db /tmp/try.db

# 1. What is in it
python3 tracker.py --db /tmp/try.db status
python3 tracker.py --db /tmp/try.db screen-list
#   -> each row shows check=CLEAN / PASS / FAIL

# 2. Act as the human gate: publish a row that passed its check
python3 tracker.py --db /tmp/try.db verify-promote --screen-id 42 --tier V1 \
    --flag "Resolved: two independent sources agree on the announced date."
#   -> promoted screen #42 -> verify_verified #34 (tier V1)

# 3. Correct it; the change is logged with its reason
python3 tracker.py --db /tmp/try.db verify-edit --id 34 \
    --set current_status="AT VOLUME (corrected)" \
    --desc "Tightened status wording after re-reading the release."
python3 tracker.py --db /tmp/try.db verify-show --id 34
#   -> the row PLUS an "edit history" line from verify_edits

# 4. Try to publish the same project twice; the gate refuses
python3 tracker.py --db /tmp/try.db verify-promote --screen-id 42 --tier V1
#   -> promotion blocked: project already in verify_verified

rm /tmp/try.db
```

**Expected:** the promote succeeds, the second is blocked, and `verify-show`
prints the edited value with one edit-history entry.

The same four steps are available guided, which prints each row's figures and
both source links and asks about them one at a time:

```bash
python3 tracker.py --db /tmp/try.db review
```

Starting from an **empty** database works the same way, except there is nothing
to promote yet, and the CLI tells you what to run to collect some rows first:

```bash
python3 tracker.py --db /tmp/empty.db initdb
python3 tracker.py --db /tmp/empty.db verify-list
```

Then start the web app (`python3 tracker.py webapp`) and click through the
same steps, ending on a Verify detail page to try the edit form.

---
