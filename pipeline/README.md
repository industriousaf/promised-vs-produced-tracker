# pipeline

The Scoreboard itself: the five tables, the commands, and the human gate.
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

| File | What it does |
|---|---|
| `cli.py` | the command line. Every pipeline step is one subcommand. |
| `db.py` | the five-table SQLite schema and connection handling |
| `source.py` | insert and list Source leads |
| `screen.py` | insert extracted rows, and run the check |
| `schema.py` | **the definition of the data**: columns, sector vocabulary, size floor, and the row validator. Also runs standalone against a CSV. |
| `schema_check.py` | the adapter that runs `schema.py` against one stored row and saves the verdict |
| `verify.py` | the promotion gate, and the edit log that records every change afterwards |
| `dates.py` | turns the extracted date text into real dates and computes `lag_years` / `slip_years` |
| `llm.py` | the AI steps, each in two forms: a prompt you paste into Claude Code, or a direct API call. Two of them collect (Source, Screen); the third checks what was collected against its own links, for the review screen. |
| `models.py` | which Claude model each stage runs. The one place the names live. |
| `orchestrate.py` | the moves between the stages, used by both interfaces |
| `prompts/` | the operating prompts `llm.py` renders for the Source and Screen steps |

## More detail

Full command reference: [`../docs/cli.md`](../docs/cli.md).
Table shapes and the date handling: [`../docs/schema.md`](../docs/schema.md).
What to check before promoting a row: [`../docs/verify_methods.md`](../docs/verify_methods.md).

Batch collection over the direct API lives in [`../tools/gather.py`](../tools/gather.py).
The browser interface onto these same functions is [`../webapp/`](../webapp/).
