"""
load_csv.py -- bulk-load a Screen-shape CSV into ../outputs/tracker.db.

Threads every scraped project through the medallion stages using the EXISTING
pipeline modules (no new SQL, no duplicated logic):

    source_collected   one lead per project: promise/status/date URLs + summary
          |            collected_via = --via (default 'bulk-import')
    screen_extracted   the v0_out row, tier 'P', with the raw -> token -> dt
          |            date chain; lag/slip computed by dates.py
    screen_check       the deterministic checker's verdict for that row
          |
    verify_verified    NOT written by this script -- see below.

WHY VERIFY IS NOT WRITTEN AUTOMATICALLY
---------------------------------------
Verify is the human gate: a row reaches it only once a person has confirmed the
sources actually support the claims. This script therefore stops at Screen and
prints the exact `verify-promote` commands to run. Pass --promote-tier to
override that, which is a deliberate, explicit act. Rows whose schema check
FAILs are refused by verify.promote() regardless.

Usage:
    python3 tools/load_csv.py --csv path/to/rows.csv --dry-run
    python3 tools/load_csv.py --csv path/to/rows.csv --via my-label
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRACKER_ROOT = HERE.parent
sys.path.insert(0, str(TRACKER_ROOT))            # so `pipeline` imports

from pipeline import db as mdb          # noqa: E402
from pipeline import source, screen, verify  # noqa: E402
from pipeline.schema_check import V0_COLUMNS, RAW_DATE_COLUMNS  # noqa: E402

DEFAULT_DB = TRACKER_ROOT / "outputs" / "tracker.db"
DEFAULT_COLLECTED_VIA = "bulk-import"   # override with --via

# screen_extracted / verify_verified declare these NOT NULL, so a row missing any
# of them cannot be inserted at all. We skip such rows at Screen (their lead
# still lands in Source) rather than fabricating a value to satisfy the column.
NOT_NULL = ["project", "sector", "state", "announced", "current_status"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Load the verify-shape CSV into a medallion database.")
    ap.add_argument("--csv", required=True, help="path to a CSV in the Screen (v0_out) shape")
    ap.add_argument("--via", default=DEFAULT_COLLECTED_VIA,
                    help="provenance label written to source_collected.collected_via")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--promote-tier", default=None,
                    help="opt in to writing verify_verified at this tier (V1/V2); "
                         "omitted = stop at Screen and leave the human gate alone")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"error: no such CSV: {csv_path}", file=sys.stderr)
        return 2

    with csv_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    mdb.set_active_db(args.db)
    if mdb.is_read_only(args.db):
        print(f"error: {args.db} uses the legacy bronze/silver/gold vocabulary "
              "and is read-only", file=sys.stderr)
        return 2

    print(f"database : {args.db}")
    print(f"csv      : {csv_path}  ({len(rows)} rows)")
    if args.dry_run:
        skipped = [r["project"] for r in rows if any(not (r.get(c) or "").strip() for c in NOT_NULL)]
        print(f"dry run: would load {len(rows)} Source leads, "
              f"{len(rows) - len(skipped)} Screen rows; skipping at Screen: {skipped}")
        return 0

    conn = mdb.connect(args.db)
    mdb.init_db(conn)

    # Re-runs must be idempotent. Checking screen_extracted alone is not enough:
    # a row held at Source (a NOT NULL cell still empty) never reaches Screen, so
    # its lead would be re-inserted on every run. source_collected has no project
    # column by design, but we write the project name as the summary's prefix.
    existing = {r["project"] for r in conn.execute("SELECT project FROM screen_extracted")}
    existing |= {
        (r["summary"] or "").split(" — ")[0]
        for r in conn.execute("SELECT summary FROM source_collected")
    }

    verdicts: dict[str, int] = {}
    skipped: list[tuple[str, list[str]]] = []
    promoted, promote_blocked = [], []

    for r in rows:
        project = (r.get("project") or "").strip()
        if project in existing:
            continue

        lead_id = source.insert_lead(
            conn,
            promise_source=r.get("promise_source") or "",
            status_source=r.get("status_source") or "",
            promised_date_source=r.get("promised_date_source") or None,
            summary=f"{project} — {r.get('notes','')}",
            collected_via=args.via,
        )

        missing = [c for c in NOT_NULL if not (r.get(c) or "").strip()]
        if missing:
            skipped.append((project, missing))
            continue

        row = {c: r.get(c, "") for c in V0_COLUMNS}
        row.update({c: r.get(c, "") for c in RAW_DATE_COLUMNS})
        screen_id = screen.insert_extracted(conn, row, source_collected_id=lead_id)
        result = screen.run_check(conn, screen_id)
        verdicts[result["result_status"]] = verdicts.get(result["result_status"], 0) + 1

        if args.promote_tier:
            try:
                vid = verify.promote(conn, screen_id, args.promote_tier,
                                     flag=r.get("flag") or None)
                promoted.append((project, vid))
            except (verify.PromotionBlocked, ValueError) as exc:
                promote_blocked.append((project, str(exc)))

    counts = mdb.table_counts(conn)
    conn.close()

    print("\n--- screen_check verdicts ---")
    for k in ("CLEAN", "PASS", "FAIL"):
        if verdicts.get(k):
            print(f"  {k:5} {verdicts[k]}")
    if skipped:
        print(f"\n--- {len(skipped)} row(s) held at Source (NOT NULL cell still empty) ---")
        for project, missing in skipped:
            print(f"  {project}: missing {', '.join(missing)}")
    if promoted:
        print(f"\n--- promoted to verify_verified: {len(promoted)} ---")
    if promote_blocked:
        print(f"\n--- promotion blocked: {len(promote_blocked)} ---")
        for project, why in promote_blocked:
            print(f"  {project}: {why}")

    print("\n--- table counts ---")
    for t, n in counts.items():
        print(f"  {t:20} {n}")

    if not args.promote_tier:
        print("\nVerify was left untouched (human gate). To promote a checked row:")
        print(f"  TRACKER_DB={args.db} python -m pipeline.cli "
              "verify-promote --screen-id N --tier V1 --flag \"...\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
