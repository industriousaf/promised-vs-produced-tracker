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
REQUIRED_COLUMNS: list[str] = pvp_schema.REQUIRED_COLUMNS      # 15 core columns
PROVENANCE_COLUMNS: list[str] = pvp_schema.PROVENANCE_COLUMNS  # the provenance columns
ERROR = pvp_schema.ERROR
WARN = pvp_schema.WARN

# The inclusion floor is no longer two integers -- it is a phase, in
# pipeline/settings.py, carrying its own operator and window. Surfaces that want
# to print it ask `criteria.active().describe()` rather than formatting numbers,
# which is what the two re-exported constants here used to be for. Same reason
# they existed: the explore-filter blurb once carried "$100M OR 200 jobs" as
# literal prose and went stale the day the floor moved. Formatting the numbers
# by hand had one more failure in it -- both call sites also hardcoded the word
# OR, so a phase joined by AND would have printed a floor that was not the rule.

# Missing values that arrived as text ('None', 'null', ...). The insert
# paths use this to blank them; the checker uses it to catch any that
# reached the database by some other route.
NULL_STRINGS = pvp_schema.NULL_STRINGS
# Does a provenance cell hold something URL-shaped? Returns None when it does.
# Re-exported so the quality report can ask the same question the checker
# asks, rather than growing its own idea of what a source link looks like.
check_url = pvp_schema.check_url
# Does a numeric cell hold a whole number, and which? Re-exported for the same
# reason as check_url: the size-backfill queue asks the floor question the
# checker's way, so a cell the two read differently cannot exist.
check_int = pvp_schema.check_int
# "Both size figures are known and both are under the floor" -- the one error
# that means the project does not belong, rather than that the row is broken.
# Re-exported so the queues and the dashboard ask the checker what it decided
# instead of pattern-matching its prose in four places.
OUT_OF_SCOPE = pvp_schema.OUT_OF_SCOPE
is_out_of_scope = pvp_schema.is_out_of_scope
UNESTABLISHED = pvp_schema.UNESTABLISHED
is_unestablished = pvp_schema.is_unestablished

# The five verdicts, and the two that a person can do something about.
#
# FAIL used to carry all three of "this row is malformed", "this project does
# not belong" and "nobody published the figure". They call for completely
# different acts -- fix it, leave it alone, go and look -- and the word said
# fault for all three. It reported 8 failures when nothing in the Tracker was
# broken.
VERDICTS = ("CLEAN", "PASS", "FAIL", "SIZE_UNKNOWN", "OUT_OF_SCOPE")
PROMOTABLE = frozenset({"CLEAN", "PASS"})


def blocks_promotion(status: str | None) -> bool:
    """Verify is a gate on the row AND on the project, so everything but the two
    promotable verdicts stops here. Splitting the words did not open a door."""
    return status not in PROMOTABLE
DATE_COLUMN_NULL_STRINGS = pvp_schema.DATE_COLUMN_NULL_STRINGS
# The promised-date sentinel set. Re-exported so the attestation rule tells an
# absence from a value in the checker's own vocabulary, not a copy of it.
PROMISED_SENTINELS = pvp_schema.PROMISED_SENTINELS
# The rest of the date vocabulary, for the correction guard in verify.py and the
# pickers on the web forms, which must offer exactly what the checker accepts.
SENTINELS_FOR = pvp_schema.SENTINELS_FOR
DATE_SENTINELS = pvp_schema.DATE_SENTINELS
SENTINEL_MEANING = pvp_schema.SENTINEL_MEANING
# The status vocabulary, for the web forms and the one-time backfill, which must
# offer and write exactly what the checker accepts.
STATUSES = pvp_schema.STATUSES
STATUS_MEANING = pvp_schema.STATUS_MEANING
statuses_for = pvp_schema.statuses_for

# Sector vocabulary, from the ONE source of truth: pipeline/settings.py, which
# schema.py reads. `all_sectors()` is the active phase's vocabulary. There is no
# longer a runtime registry -- adding a sector is a commit, so the set of things
# that count as in scope cannot move mid-run without leaving a trace.
SECTORS = pvp_schema.SECTORS
all_sectors = pvp_schema.all_sectors

# The full 20-column "v0_out" shape, in CSV-header order. REQUIRED (…, notes)
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
# additions and are NOT part of the 20-column v0_out shape.
RAW_DATE_COLUMNS = [
    "announced_raw",
    "promised_first_output_raw",
    "actual_first_output_raw",
]


def check_row(row: dict, crit=None) -> dict:
    """Run the canonical checker against one extracted row.

    `crit` is the inclusion phase to judge by. Pass the phase that ADMITTED the
    row (`criteria.get(row["criteria_id"])`) so that moving a threshold cannot
    retroactively re-grade data collected under the old one. Omitted, the active
    phase is used, which is right for a row being admitted for the first time.

    `row` is a mapping of the 20 v0 columns to values (missing keys are treated
    as empty). Returns the persisted `screen_check` shape:

        {
          "result_status": one of VERDICTS,
          "n_errors": int,
          "n_warnings": int,
          "report": [ {"column": str, "level": "ERROR"|"WARN", "message": str}, ... ],
        }

    Verdict mirrors what schema.py prints today:
      - any ERROR   -> FAIL   (not structurally admissible; blocks promotion)
      - only WARNs  -> PASS   (admissible; a person still has something to settle)
      - nothing     -> CLEAN  (valid and every cell shows its work)

    CLEAN is the best of the three, not the middle one. The names do not sort
    that way and have been read backwards before.

    Only FAIL blocks `verify-promote`. PASS is promotable by design: the usual
    warning is an open `flag`, no command edits a flag on a Screen row, and
    promotion is what rewrites it into a resolution record.
    """
    # Normalise to strings the way the checker's CSV reader would present them.
    str_row = {c: _as_cell(row.get(c)) for c in V0_COLUMNS}

    # All provenance columns exist in our schema, so the checker can prove
    # everything it knows how to prove.
    has_prov = {c: True for c in PROVENANCE_COLUMNS}

    issues = pvp_schema.validate_row(1, str_row, has_prov, crit)

    errors = [i for i in issues if i.level == ERROR]
    warnings = [i for i in issues if i.level == WARN]

    # Precedence, and the reasoning for it:
    #
    #   OUT_OF_SCOPE first. Both size figures are known and both are under the
    #   floor, so the project is not going into the Tracker. Nothing else about
    #   the row matters -- correcting a date on a project that does not belong
    #   is work that buys nothing -- so this outranks a malformed cell.
    #
    #   FAIL next, and ONLY for errors that are not about the size floor. This
    #   is the word's original meaning: the row is malformed and a person can
    #   fix it. Today no row in the Tracker is in this state.
    #
    #   SIZE_UNKNOWN last of the three. A cell the floor depends on is empty, so
    #   the row is not admissible, but nothing is wrong with it -- the figure was
    #   never published. A source found tomorrow settles it. It ranks below FAIL
    #   because a malformed cell is fixable now and this may never be.
    err_msgs = [i.message for i in errors]
    out_of_scope = any(m.startswith(OUT_OF_SCOPE) for m in err_msgs)
    unestablished = any(m.startswith(UNESTABLISHED) for m in err_msgs)
    other_errors = [m for m in err_msgs
                    if not m.startswith((OUT_OF_SCOPE, UNESTABLISHED))]

    if out_of_scope:
        status = "OUT_OF_SCOPE"
    elif other_errors:
        status = "FAIL"
    elif unestablished:
        status = "SIZE_UNKNOWN"
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
