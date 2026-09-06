# Command reference

*Package: [`../pipeline/`](../pipeline/)*

Every pipeline step is one command. `python3 scoreboard.py --help` lists all 24
and carries worked examples for each; this file is the flat list, plus the three
things `--help` cannot hold: what needs installing, how the modules fit together,
and a walkthrough on a copy of the database.

> **This file is machine-read.** `collect/source.sh` attaches it to every
> `claude -p` process with an `@`-mention, and both operating prompts point at it
> as the command reference. Keep the command list complete: a process that cannot
> find a flag here will guess at one.

Run everything from `aici/scoreboard`. Two invocations, identical in effect:

```bash
python3 scoreboard.py <command>     # the documented entry point
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

`config.env` in this directory is optional: put `SCOREBOARD_DB` or an API key
there and `tools/gather.py` reads it, though a real shell variable always wins.
It is gitignored. The database defaults to `outputs/scoreboard.db`.

## Every command

```bash
# Orientation
python3 scoreboard.py                       # counts, and where to go next
python3 scoreboard.py status                # row counts per stage
python3 scoreboard.py initdb                # create the five tables
python3 scoreboard.py --help                # all of the below, with examples

# Collect  (needs the claude CLI; spends money)
python3 scoreboard.py collect --n 5 --dry-run
python3 scoreboard.py collect --n 10 [--only source|screen] [--continue-on-fail]

# Source
python3 scoreboard.py source-add --promise URL --status URL [--summary "..."]
python3 scoreboard.py source-add --json lead.json [--via LABEL]   # or --json -
python3 scoreboard.py source-prompt                               # no key
python3 scoreboard.py source-collect                              # needs a key
python3 scoreboard.py source-list

# Screen
python3 scoreboard.py screen-prompt --source-id N                 # no key
python3 scoreboard.py screen-add --json row.json --source-id N
python3 scoreboard.py screen-extract --source-id N                # needs a key
python3 scoreboard.py screen-check --id N          # or --all
python3 scoreboard.py screen-list [--by-capital]
python3 scoreboard.py screen-show --id N
python3 scoreboard.py screen-date --id N --date "2021-12" --source URL [--raw "..."] [--note "..."]
python3 scoreboard.py screen-date --id N --unresolved "what was searched, and what was found"

# Verify  (the human gate)
python3 scoreboard.py review [--id N]              # guided, one row at a time
python3 scoreboard.py verify-promote --screen-id N --tier V1 [--flag "..."] [--set col=val] [--force]
python3 scoreboard.py verify-edit --id N --set col=val --desc "why"
python3 scoreboard.py verify-show --id N           # row + edit history
python3 scoreboard.py verify-list

# Read, export, measure
python3 scoreboard.py filter --capital 1000000000 --jobs 2000 --op OR --stage verify
python3 scoreboard.py filter --capital 500000000  --jobs 400  --op AND --stage screen
python3 scoreboard.py export [--out-dir DIR]       # five CSVs
python3 scoreboard.py coverage --against ref.csv [--stage verify] [--min-capital N]
python3 scoreboard.py coverage --selftest          # needs no database

# The sector vocabulary  (sectors-add is a HUMAN decision — NEVER run it automatically)
python3 scoreboard.py sectors-list
python3 scoreboard.py sectors-add "Cement"

# The browser interface
python3 scoreboard.py webapp [--port 8100] [--reload]

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
- `schema.py` — **the definition of the data**: the columns, the sector
  vocabulary, the inclusion floor, and the row validator. Also runs standalone
  against a CSV. If this file and any document disagree, this file is right.
- `verify.py` — `promote()` (the human gate) and `edit()`, which writes a
  `verify_edits` row in the **same transaction** as every Verify update.
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
python3 scoreboard.py source-prompt          # prints it; already excludes what you have
# run it in an assistant that can search the web; it ends with one JSON object
python3 scoreboard.py source-add --json -    # paste the JSON, then Ctrl-D
```

**Screen — extract the row for a lead:**

```bash
python3 scoreboard.py screen-prompt --source-id 7   # the prompt, with the links filled in
# run it, then:
python3 scoreboard.py screen-add --json row.json --source-id 7
python3 scoreboard.py screen-check --id 11
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
untouched. From `aici/scoreboard`:

```bash
cp outputs/scoreboard.db /tmp/try.db

# 1. What is in it
python3 scoreboard.py --db /tmp/try.db status
python3 scoreboard.py --db /tmp/try.db screen-list
#   -> each row shows check=CLEAN / PASS / FAIL

# 2. Act as the human gate: publish a row that passed its check
python3 scoreboard.py --db /tmp/try.db verify-promote --screen-id 42 --tier V1 \
    --flag "Resolved: two independent sources agree on the announced date."
#   -> promoted screen #42 -> verify_verified #34 (tier V1)

# 3. Correct it; the change is logged with its reason
python3 scoreboard.py --db /tmp/try.db verify-edit --id 34 \
    --set current_status="AT VOLUME (corrected)" \
    --desc "Tightened status wording after re-reading the release."
python3 scoreboard.py --db /tmp/try.db verify-show --id 34
#   -> the row PLUS an "edit history" line from verify_edits

# 4. Try to publish the same project twice; the gate refuses
python3 scoreboard.py --db /tmp/try.db verify-promote --screen-id 42 --tier V1
#   -> promotion blocked: project already in verify_verified

rm /tmp/try.db
```

**Expected:** the promote succeeds, the second is blocked, and `verify-show`
prints the edited value with one edit-history entry.

The same four steps are available guided, which prints each row's figures and
both source links and asks about them one at a time:

```bash
python3 scoreboard.py --db /tmp/try.db review
```

Starting from an **empty** database works the same way, except there is nothing
to promote yet, and the CLI tells you what to run to collect some rows first:

```bash
python3 scoreboard.py --db /tmp/empty.db initdb
python3 scoreboard.py --db /tmp/empty.db verify-list
```

Then start the web app (`python3 scoreboard.py webapp`) and click through the
same steps, ending on a Verify detail page to try the edit form.

---
