# pipeline

The Scoreboard itself: the six tables, the commands, and the human gate.
`collect/`, `tools/` and the web app all write through this. If you only read one
directory, read this one.

**Contents**

- [Run it](#run-it)
- [What each file does](#what-each-file-does)
- [More detail](#more-detail)

## Run it

Run it from the parent directory (`scoreboard/`):

```bash
python3 scoreboard.py status
python3 scoreboard.py --help    # every command
```

## What each file does

Fifteen files, six jobs. Read them by job, not by name.

**Configuration**

| File | What it does |
|---|---|
| `settings.py` | **every setting**: the inclusion phases (what counts as a project), which model runs each stage, and how a collection run behaves. `python3 scoreboard.py config` prints all of it with the line to edit. |

**The data**

| File | What it does |
|---|---|
| `db.py` | the five-table SQLite schema and connection handling. Refreshes the CSV exports on every write. |
| `schema.py` | **the definition of a well-formed row**: columns and the row validator. Also runs standalone against a CSV. The sector vocabulary and size floor it enforces come from `settings.py`. |
| `schema_check.py` | the adapter that runs `schema.py` against one stored row and saves the verdict |
| `dates.py` | turns the extracted date text into real dates and computes `lag_years` / `slip_years` |

**The three stages**

| File | What it does |
|---|---|
| `source.py` | insert and list Source leads |
| `screen.py` | insert extracted rows, run the check, and the narrow date-backfill writer |
| `verify.py` | the promotion gate, and the edit log that records every change afterwards |

**The AI steps**

| File | What it does |
|---|---|
| `llm.py` | each AI step in two forms: a prompt you paste into Claude Code, or a direct API call. Two collect (Source, Screen); the third checks a row against its own links, for the review screen. |
| `orchestrate.py` | the moves between the stages, used by both interfaces |
| `prompts/` | the operating prompts `llm.py` renders |

**Measuring and moving the data**

| File | What it does |
|---|---|
| `quality.py` | five measures of whether the Scoreboard can carry the claim (`quality`) |
| `coverage.py` | recall against a reference list of projects (`coverage`). Runs standalone too. |
| `export_tables.py` | database to CSV (`export`). Runs standalone too, and on every write via `db.py`. |

**The command line**

| File | What it does |
|---|---|
| `cli.py` | the command line. Every step is one subcommand; `scoreboard.py` in the parent directory is its launcher. |

## More detail

Full command reference: [`../docs/cli.md`](../docs/cli.md).
Table shapes and the date handling: [`../docs/schema.md`](../docs/schema.md).
What to check before promoting a row: [`../docs/verify_methods.md`](../docs/verify_methods.md).

Batch collection over the direct API lives in [`../tools/gather.py`](../tools/gather.py).
The browser interface onto these same functions is [`../webapp/`](../webapp/).
