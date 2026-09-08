# tools

Scripts that operate on an existing `scoreboard.db`: moving data in or out,
measuring it, or driving collection a different way.

Run them from the parent directory (`scoreboard/`).

Two are also CLI commands, because they are things people do routinely:
`python3 scoreboard.py export` and `python3 scoreboard.py coverage`. Same code
either way, and both scripts still run on their own.

The other two stay scripts, for different reasons. `load_csv.py` is a rare
one-off and its `--promote-tier` can write to Verify in bulk, so putting it
beside `review` in `--help` would make a sharp tool look routine. `gather.py` is
a real collection path, not maintenance: it is the direct-API counterpart to the
`collect` command. It stays a script because `collect` and `gather` are synonyms,
and two commands with the same name for different engines would be worse than
one command and one script. If the API path ever gets regular use, the shape to
reach for is `collect --api`, not a second word for the same idea.

**Contents**

- [`export_tables.py` — database to CSV](#export_tablespy--database-to-csv)
- [`load_csv.py` — CSV to database](#load_csvpy--csv-to-database)
- [`gather.py` — batch collection over the API](#gatherpy--batch-collection-over-the-api)
- [`coverage.py` — recall against a reference list](#coveragepy--recall-against-a-reference-list)

## `export_tables.py` — database to CSV

Dumps one flat CSV per table into `outputs/csv_tables/`: three for the
Scoreboard itself, two for the audit trail.

```bash
python3 scoreboard.py export        # or: python3 tools/export_tables.py
```

Every column of each table, in table order, sorted by `id`, NULLs as empty cells.
`screen_check` and `verify_edits` are exported too, because `scoreboard.db` is
committed and git cannot diff a binary: without them a commit could add fifty
check runs, or a correction to a published figure, and show nothing but
"scoreboard.db changed". Read-only on the database.
`--db` reads a different database, `--out-dir` writes somewhere else.

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
`scoreboard.py` — see [Where the API key goes](../README.md#where-the-api-key-goes).
It stops at Screen, like everything else that is not a person.

The `collect` command needs no API key and is the usual way to collect.

## `coverage.py` — recall against a reference list

How much of a known list of projects does the Scoreboard actually have? This is
the only completeness measure available, because nobody publishes the true
universe of US manufacturing projects, so the denominator has to come from an
enumerable list.

```bash
python3 scoreboard.py coverage --against <reference.csv>
python3 scoreboard.py coverage --against <reference.csv> --min-capital 1000000000
python3 scoreboard.py coverage --against <reference.csv> --stage verify
```

`python3 tools/coverage.py` takes the same flags and still works.

The reference CSV needs `project` and `state` columns; `promised_capital_usd`
enables `--min-capital`, which is how the Phase 1 (≥$1B) line is measured. That
is now the inclusion floor itself, so the flag's real use is trimming a reference
list that reaches further down than this Scoreboard does.
`--stage` picks whether you are measuring what has been collected (`screen`,
the default) or what has been published (`verify`).

**It does not compare names.** The same project appears under different names in
different sources, and different projects appear under similar ones. On the
historical hand-built dataset, exact-name matching scores 0 of 10 and naive
substring matching scores 9 of 10; the truth is 7. Matching is therefore gated on
**state** first — a project is a company plus a physical site — and only scored
on name overlap within that state. That is what separates `TSMC Fab 1 Phoenix`
from `TSMC Arizona Fabs` (same site, different names) and `Nucor plate mill
Brandenburg` in KY from `Nucor Steel Mill` in WV (same company, different sites).

Results come back in three buckets, not two: **covered**, **missing**, and
**ambiguous** — a candidate exists but the call belongs to a person. Ambiguous
rows are never counted as covered, and recall is reported as a range when any
exist. Missing rows are listed largest-capital first, since that is the order
worth chasing.

`--selftest` checks the matcher against the pairs whose answer is known by hand,
needs no database, and should stay at 10/10.
