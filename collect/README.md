# collect

The unattended collection runs, and only that. Three loops -- one finds new
projects and files them into Source, one extracts Source leads into Screen rows,
one backfills first-output dates -- plus the accounting each loop needs. Shell
where a loop is a loop, Python where it has to read JSON. Nothing here is a
library; the library is `../pipeline/`, and every loop writes through it. None of
them touches Verify, which is a human gate.

**Contents**

- [Run it](#run-it)
- [What is here](#what-is-here)
- [How the loops behave](#how-the-loops-behave)

## Run it

Run from the repository root. Needs the `claude` CLI logged in
once (`claude`, then `/login`); the loop checks that before it starts.

```bash
python3 tracker.py collect --n 5 --dry-run   # show the plan
python3 tracker.py collect --n 10            # do it
```

The scripts still run directly, and that is the form to use when you want a knob
the flags do not expose (see [`../docs/collecting.md`](../docs/collecting.md)):

```bash
N=5 DRY_RUN=1 bash collect/all.sh   # identical to the first command above
N=10 bash collect/all.sh            # identical to the second
```

## What is here

| Path | What it does |
|---|---|
| `all.sh` | runs Source then Screen back to back. The usual entry point; `tracker.py collect` calls it. |
| `source.sh` | stage A on its own: web discovery into Source |
| `screen.sh` | stage B on its own: Source into Screen |
| `dates.sh` | the backfill: find a dated first-output source for rows that produced but carry none |
| `tally.py` | reads each call's result and prints what it spent; totals a run's ledger at the end |
| `prompts/` | the prompt handed to each iteration, and how each one is configured |

## How the loops behave

Each iteration is a separate run that starts fresh, so nothing carries between passes.
Ctrl-C is safe, and re-running continues where you left off because the database
de-duplicates.

Every knob: [`../docs/collecting.md`](../docs/collecting.md).
