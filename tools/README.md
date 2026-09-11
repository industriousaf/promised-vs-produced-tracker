# tools

Standalone scripts that nobody imports. Each operates on an existing
`tracker.db` and is run directly rather than through the CLI -- deliberately,
for a different reason each:

- `load_csv.py` is a rare one-off, and its `--promote-tier` can write to Verify
  in bulk. Putting it beside `review` in `--help` would make a sharp tool look
  routine.
- `gather.py` is a real collection path, the direct-API counterpart to the
  `collect` command. It stays a script because `collect` and `gather` are
  synonyms, and two commands with the same name for different engines would be
  worse than one command and one script. If the API path ever gets regular use,
  the shape to reach for is `collect --api`, not a second word for the same idea.

Run them from the parent directory (`tracker/`).

`export` and `coverage` lived here too, until it was noticed that `pipeline/db.py`
imports the exporter on every write -- core code depending on a "tool". Both are
now in `../pipeline/` beside the code that uses them, and both still run on their
own.

**Contents**

- [`load_csv.py` — CSV to database](#load_csvpy--csv-to-database)
- [`gather.py` — batch collection over the API](#gatherpy--batch-collection-over-the-api)

## `load_csv.py` — CSV to database

Bulk-loads a CSV that is already in the Screen (v0_out) shape, threading each row
through the real pipeline modules so the dates, the derived columns, and the
schema check all behave exactly as they would for a normal row.

```bash
python3 tools/load_csv.py --csv path/to/rows.csv --dry-run
python3 tools/load_csv.py --csv path/to/rows.csv --via my-import-label
```

`--via` stamps `source_collected.collected_via` so the rows stay attributable to
where they came from. It stops at Screen: promotion to Verify is a human gate.
`--promote-tier` will do it in bulk, and is off by default for that reason.

The CSV needs the Screen (v0_out) column shape; `docs/schema.md` documents it.
Rows that clear the schema check land in Screen, ready for review.

## `gather.py` — batch collection over the API

An alternative to the `collect` command: collects N Source leads and optionally
extracts some of them into Screen, in one run, through the Anthropic API rather
than the `claude` CLI.

```bash
python3 tools/gather.py --n-source 5 --dry-run        # the plan, no API calls
python3 tools/gather.py --n-source 10 --n-screen 3    # 10 leads, extract the first 3
```

Needs `ANTHROPIC_API_KEY`, either exported or in a `config.env` beside
`tracker.py` — see [Where the API key goes](../README.md#where-the-api-key-goes).
It stops at Screen, like everything else that is not a person.

The `collect` command needs no API key and is the usual way to collect.
