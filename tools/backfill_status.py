"""Give every stored project a `status`, once.

`status` arrived after 173 projects had been extracted and many published, all
with only the free-text `current_status`. This fills the empty ones in
screen_extracted and verify_verified from two things a person has already
checked:

  * actual_first_output, which decides which statuses are possible
    (statuses_for in pipeline/schema.py), and
  * how current_status begins, which picks among them.

A project it cannot place is listed and left empty for a person to choose; it
never guesses. Rows that already have a status are not touched, so it is safe
to run again after pulling someone else's database.

    python3 tools/backfill_status.py            # show what it would write
    python3 tools/backfill_status.py --write    # write it, then refresh the CSVs
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import db  # noqa: E402
from pipeline.export_tables import export_all  # noqa: E402
from pipeline.schema_check import STATUSES, statuses_for  # noqa: E402

# How current_status begins, for a project that has not produced yet.
NOT_YET = {
    "announced": {"ANNOUNCED", "PRE-CONSTRUCTION", "NOT STARTED", "PRE-FID", "PROPOSED",
                  "PLANNING", "PLANNING/PERMITTING", "IN PERMITTING", "SITE SELECTED",
                  "IN DESIGN & ENGINEERING"},
    "under construction": {"UNDER CONSTRUCTION", "COMMISSIONING/STARTUP", "PRE-PRODUCTION",
                           "PILOT PRODUCTION", "NOT YET OPERATIONAL", "NOT YET PRODUCING",
                           "NOT PRODUCING", "NOT IN PRODUCTION", "RETOOLING COMPLETE",
                           "READY"},
    "paused": {"ON HOLD", "PAUSED", "STALLED", "NOT BUILT", "WITHDRAWN"},
}

# "DELAYED" says when, not what stage, so these were read one at a time.
DELAYED = {
    8: "announced",             # Chobani Rome: construction not substantially begun
    11: "under construction",   # Intel Ohio One
    16: "under construction",   # Samsung Taylor
    95: "under construction",   # Toyota Georgetown BEV
    107: "under construction",  # Stellantis Belvidere: retooling under way
}

# A project that produced and has since shut down.
CLOSED = {"CLOSED", "CLOSING"}

# Words saying the promised plant never came to be. On a project whose first
# output says it produced, they mean a person should look at that word.
NOT_BUILT = re.compile(r"never built|not built|cancel", re.IGNORECASE)


def lead(current_status) -> str:
    """How current_status begins: the words before the first ; : ( , or dash."""
    return re.split(r"[;:(—,]| - ", current_status or "")[0].strip().upper()


def classify(screen_id: int, current_status, first_output) -> tuple[str | None, str | None]:
    """(status, note). A None status comes with the reason a person has to choose."""
    fits = statuses_for(first_output)
    how = lead(current_status)
    if fits == ("cancelled",):
        return "cancelled", None
    if fits == ("producing", "closed"):
        note = None
        if NOT_BUILT.search(current_status or ""):
            note = ("actual_first_output says it produced, but current_status reads "
                    f"{(current_status or '')[:60]!r}. Should actual_first_output be never?")
        return ("closed" if how in CLOSED else "producing"), note
    if fits == ("announced", "under construction", "paused"):
        if how == "DELAYED" and screen_id in DELAYED:
            return DELAYED[screen_id], None
        for status, starts in NOT_YET.items():
            if how in starts:
                return status, None
        return None, f"not producing yet, and {how!r} does not say which stage"
    return None, f"actual_first_output {first_output!r} does not say whether it produced"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Give every stored project a status, once.")
    ap.add_argument("--write", action="store_true",
                    help="write the statuses (without it, only show them)")
    ap.add_argument("--db", default=None, help="database path (default: the usual one)")
    args = ap.parse_args(argv)

    conn = db.connect(args.db) if args.db else db.connect()
    if args.write:
        db.init_db(conn)  # adds the column to a database that predates it
    unplaced = 0
    for table, id_col in (("screen_extracted", "id"), ("verify_verified", "screen_extracted_id")):
        has_column = "status" in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        counts, checks, missing, writes = Counter(), [], [], []
        for r in rows:
            if has_column and (r["status"] or "").strip():
                counts["already set"] += 1
                continue
            status, note = classify(r[id_col], r["current_status"], r["actual_first_output"])
            label = f"#{r[id_col]} {r['project'][:40]}"
            if status:
                counts[status] += 1
                writes.append((status, r["id"]))
                if note:
                    checks.append(f"{label}: {note}")
            else:
                missing.append(f"{label}: {note}")
        print(f"{table}: {len(rows)} projects")
        for s in (*STATUSES, "already set"):
            if counts[s]:
                print(f"  {counts[s]:4d}  {s}")
        for line in checks:
            print(f"  look at  {line}")
        for line in missing:
            print(f"  not placed  {line}")
        unplaced += len(missing)
        if args.write and writes:
            conn.executemany(
                f"UPDATE {table} SET status = ? "
                "WHERE id = ? AND (status IS NULL OR status = '')", writes)
    if args.write:
        conn.commit()
        export_all(db=args.db or db.db_path())
        print("Written, and the CSVs refreshed.")
    else:
        print("Nothing written. Add --write to write.")
    conn.close()
    return 1 if unplaced else 0


if __name__ == "__main__":
    raise SystemExit(main())
