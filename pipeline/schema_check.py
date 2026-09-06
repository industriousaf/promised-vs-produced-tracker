"""
schema_check.py -- the Screen pt-2 (`screen_check`) adapter.

The canonical checker `schema.py` **is** screen_check --
there is deliberately no second validator. So rather than re-implement any
validation, this module *loads*
that canonical checker and runs its row-level validator against a single
`screen_extracted` row, translating the result into the shape `screen_check`
persists (a verdict + error/warning counts + a structured report).

Loading `schema.py` by path (it is a standalone CLI as well as a library, and
is a script, not a package module) keeps it the single source of truth for what
a well-formed row is -- if the checker's rules change, this adapter changes with
them for free.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# --- Load the canonical checker as a module (it's a sibling script) ---------- #
_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.py"
_spec = importlib.util.spec_from_file_location("pvp_schema", _SCHEMA_PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover - defensive
    raise ImportError(f"could not load canonical schema checker at {_SCHEMA_PATH}")
pvp_schema = importlib.util.module_from_spec(_spec)
# Register before exec so the module's own @dataclass (Issue) can resolve its
# __module__ via sys.modules -- exec_module alone doesn't insert it.
sys.modules[_spec.name] = pvp_schema
_spec.loader.exec_module(pvp_schema)

# Re-export the pieces the rest of the package needs from the ONE source of truth.
REQUIRED_COLUMNS: list[str] = pvp_schema.REQUIRED_COLUMNS      # 13 core columns
PROVENANCE_COLUMNS: list[str] = pvp_schema.PROVENANCE_COLUMNS  # 5 provenance columns
ERROR = pvp_schema.ERROR
WARN = pvp_schema.WARN

# Missing values that arrived as text ('None', 'null', ...). The insert
# paths use this to blank them; the checker uses it to catch any that
# reached the database by some other route.
NULL_STRINGS = pvp_schema.NULL_STRINGS
# Does a provenance cell hold something URL-shaped? Returns None when it does.
# Re-exported so the quality report can ask the same question the checker
# asks, rather than growing its own idea of what a source link looks like.
check_url = pvp_schema.check_url
DATE_COLUMN_NULL_STRINGS = pvp_schema.DATE_COLUMN_NULL_STRINGS

# Sector vocabulary, from the ONE source of truth. `all_sectors()` is the live
# vocabulary (base + runtime registry); `register_sector()` is the API-path
# function that extends it without editing code.
SECTORS = pvp_schema.SECTORS
all_sectors = pvp_schema.all_sectors
register_sector = pvp_schema.register_sector

# The full 18-column "v0_out" shape, in CSV-header order. REQUIRED (…, notes)
# then PROVENANCE (promise_source, status_source, flag, promised_date_source,
# actual_date_source) reproduces the header of promised_vs_produced_v0_out.csv
# plus the actual-side date source that file never had.
V0_COLUMNS: list[str] = list(REQUIRED_COLUMNS) + list(PROVENANCE_COLUMNS)

# The two columns stored as integers in SQL.
INT_COLUMNS = {"promised_capital_usd", "promised_jobs"}

# lag_years / slip_years are now stored as REAL floats: they're computed
# deterministically by pipeline/dates.py (with -1.0 "to be completed" / -2.0
# "cancelled" sentinels), not free-text as in the legacy CSV.
FLOAT_COLUMNS = {"lag_years", "slip_years"}

# The standardized DATETIME interpretation of each date string cell (see
# pipeline/dates.py). These live in screen_extracted / verify_verified alongside
# the v0 columns; they hold an ISO 'YYYY-MM-DD' or NULL (for a sentinel date).
DERIVED_DATE_COLUMNS = [
    "announced_dt",
    "promised_first_output_dt",
    "actual_first_output_dt",
]

# The *verbatim* source text each date was extracted from -- exactly what the
# webpage said, copied off the page before ANY normalization (e.g. "…output is
# slated for the first half of 2025…"). Stored next to the normalized token
# (announced / promised_first_output / actual_first_output) and its resolved
# *_dt, giving a full raw -> token -> dt provenance chain per date. The canonical
# checker does NOT validate these (they are free verbatim text); they exist for
# audit and reproducibility. Like the *_dt columns, they are pipeline-stage
# additions and are NOT part of the 18-column v0_out shape.
RAW_DATE_COLUMNS = [
    "announced_raw",
    "promised_first_output_raw",
    "actual_first_output_raw",
]


def check_row(row: dict) -> dict:
    """Run the canonical checker against one extracted row.

    `row` is a mapping of the 18 v0 columns to values (missing keys are treated
    as empty). Returns the persisted `screen_check` shape:

        {
          "result_status": "FAIL" | "PASS" | "CLEAN",
          "n_errors": int,
          "n_warnings": int,
          "report": [ {"column": str, "level": "ERROR"|"WARN", "message": str}, ... ],
        }

    Verdict mirrors what schema.py prints today:
      - any ERROR   -> FAIL   (not structurally admissible)
      - only WARNs  -> PASS   (admissible, not yet publishable)
      - nothing     -> CLEAN  (valid and every cell shows its work)
    """
    # Normalise to strings the way the checker's CSV reader would present them.
    str_row = {c: _as_cell(row.get(c)) for c in V0_COLUMNS}

    # All provenance columns exist in our schema, so the checker can prove
    # everything it knows how to prove.
    has_prov = {c: True for c in PROVENANCE_COLUMNS}

    issues = pvp_schema.validate_row(1, str_row, has_prov)

    errors = [i for i in issues if i.level == ERROR]
    warnings = [i for i in issues if i.level == WARN]

    if errors:
        status = "FAIL"
    elif warnings:
        status = "PASS"
    else:
        status = "CLEAN"

    report = [
        {"column": i.column, "level": i.level, "message": i.message}
        for i in issues
    ]
    return {
        "result_status": status,
        "n_errors": len(errors),
        "n_warnings": len(warnings),
        "report": report,
    }


def _as_cell(value) -> str:
    """Present a stored value the way the checker (a CSV consumer) expects."""
    if value is None:
        return ""
    return str(value)
