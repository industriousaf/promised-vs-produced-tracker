"""
export_tables.py -- dump every tracker table to flat CSVs.

Reads ../outputs/tracker.db and writes one CSV per table into
../outputs/csv_tables/:

    tracker_source.csv        <- source_collected     the data
    tracker_screen.csv        <- screen_extracted
    tracker_verify.csv        <- verify_verified

    tracker_screen_check.csv      <- screen_check      the audit trail
    tracker_screen_attested.csv   <- screen_attested
    tracker_verify_edits.csv      <- verify_edits

The first three are what someone opens to read the Tracker. The last three
record what the checker tested, which fields a person confirmed against which
page, and what was corrected after publication.

They are exported because tracker.db is committed, and git cannot diff a
binary. Without them a commit can add fifty check runs, or a correction to a
published figure, and show nothing but "tracker.db changed". The audit trail
is the part a reader of this repository would most want to see in a diff.

Each CSV carries every column of its table, in table order, with a header row and
rows ordered by `id`. NULLs become empty cells. Read-only on the database and
stdlib-only (sqlite3 + csv), matching the rest of the pipeline.

Run it from anywhere:

    python3 pipeline/export_tables.py
    python3 pipeline/export_tables.py --db /path/to/tracker.db --out-dir .
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Needed only when run as a script: sys.path[0] is then pipeline/ and the
# package is not importable without the root on the path.
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))
# The ONE resolver, shared with db.py -- this file's own copy ignored the web
# app's active-database picker.
from pipeline.db import db_path  # noqa: E402

# label -> source table. The label is what lands in the filename.
#
# Split in two because they answer different questions, not because they are
# exported differently: STAGE_TABLES is the Tracker, AUDIT_TABLES is the
# record of how it got that way.
STAGE_TABLES = {
    "source": "source_collected",
    "screen": "screen_extracted",
    "verify": "verify_verified",
}

AUDIT_TABLES = {
    "screen_check": "screen_check",
    "screen_attested": "screen_attested",
    "verify_edits": "verify_edits",
}

ALL_TABLES = {**STAGE_TABLES, **AUDIT_TABLES}


def export_table(conn: sqlite3.Connection, table: str, dest: Path) -> int:
    """Write every row of `table` to `dest` as CSV. Returns the row count."""
    cur = conn.execute(f"SELECT * FROM {table} ORDER BY id")
    header = [d[0] for d in cur.description]

    n = 0
    with open(dest, "w", newline="", encoding="utf-8") as fh:
        # lineterminator is explicit: csv.writer defaults to CRLF, which would
        # rewrite every line of a previously LF-committed file and bury the real
        # row changes in the diff. These CSVs exist to be diffed.
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        for row in cur:
            # sqlite3 hands back None for NULL; csv renders that as an empty cell.
            writer.writerow(["" if v is None else v for v in row])
            n += 1
    return n


def export_dir(out_dir: str | Path | None = None,
               db: str | Path | None = None) -> Path:
    """Where export_all writes: csv_tables/ beside the database it read.

    Defined once and used both to write and to report, so the path printed can
    never be a different path from the one written to -- which is the failure
    mode that let a scratch export land on the real database unnoticed.
    """
    if out_dir is not None:
        return Path(out_dir)
    source = Path(db) if db is not None else db_path()
    return source.parent / "csv_tables"


def export_all(db: str | Path | None = None, out_dir: str | Path | None = None) -> dict[str, int]:
    """Export every table. Returns {label: rows written}, in ALL_TABLES order.

    The CSVs are written next to the database they came from -- `csv_tables/`
    in the database's own directory -- not to a fixed path. For the committed
    database, `outputs/tracker.db`, that resolves to `outputs/csv_tables/`,
    which is where they have always gone; nothing about the normal case moves.

    What changes is every other case. The source honoured $TRACKER_DB and
    --db while the destination did not, so exporting a scratch database wrote
    its rows straight over the real database's CSVs, with no warning and nothing
    in the output naming the file it had just overwritten. It is a quiet way to
    replace a 25-row published export with a 1-row test fixture, and it is how
    this bug was found. pipeline/db.py already refuses the same thing on the
    automatic path -- it declines to mirror any database that is not the
    committed one -- and this brings the explicit command into line with it.

    --out-dir still wins, for the case where you really do want them elsewhere.
    """
    source = Path(db) if db is not None else db_path()
    target_dir = export_dir(out_dir, source)
    if not source.exists():
        raise FileNotFoundError(f"database not found: {source}")
    target_dir.mkdir(parents=True, exist_ok=True)

    # mode=ro so an export can never mutate (or lock out) the pipeline's database.
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        counts = {}
        for stage, table in ALL_TABLES.items():
            dest = target_dir / f"tracker_{stage}.csv"
            counts[stage] = export_table(conn, table, dest)
    finally:
        conn.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--db", default=None, help="path to tracker.db (default: outputs/tracker.db, or $TRACKER_DB)")
    parser.add_argument("--out-dir", default=None, help="where to write the CSVs (default: csv_tables/ beside the database)")
    args = parser.parse_args()

    counts = export_all(db=args.db, out_dir=args.out_dir)
    for stage, n in counts.items():
        print(f"tracker_{stage}.csv  <- {ALL_TABLES[stage]}  ({n} rows)")


if __name__ == "__main__":
    main()
