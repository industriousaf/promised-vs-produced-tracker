"""
schema.py -- the canonical schema + hand-verification
pipeline for the Promised vs. Produced scoreboard.

This is the "verifiable by hand" step from scoreboard/old/SPEC.md. It does NOT
pull from the web, enter data, or call any model. It is the contract every row
must satisfy before a human runs the citation pass -- run it, read the report,
fix the rows it flags, run it again. That loop is the whole Level 1 pipeline.

Design notes
------------
- Zero third-party deps (stdlib csv/re/argparse only) so it runs in CI or a bare
  venv without pulling pandas.
- Two severities. ERROR = the row is not structurally admissible (bad type, bad
  enum, missing anchor, size-floor fail, duplicate key). WARN = the row is
  admissible but not yet publishable (a verified tier with no inline sources, or
  an open/unresolved flag). ERRORs fail the run; WARNs fail only under --strict.
- The schema mirrors promised_vs_produced_v0_out.csv (the enriched/"screen"
  shape with provenance columns), plus `actual_date_source`. The five provenance
  columns are OPTIONAL as columns, but their absence downgrades what
  verifiability the checker can prove.

Usage
-----
    python -m pipeline.schema PATH/TO/scoreboard.csv
    python -m pipeline.schema PATH/TO/scoreboard.csv --strict
    python -m pipeline.schema PATH/TO/scoreboard.csv --quiet   # summary only

Exit code is 0 when the file passes (no ERRORs; also no WARNs under --strict),
1 otherwise -- so it drops straight into a pre-commit hook or CI gate.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- #
# Controlled vocabularies                                                      #
# --------------------------------------------------------------------------- #

# The required core columns (the 13-column v0 "source" shape). Every scoreboard must
# carry these, in any order.
REQUIRED_COLUMNS = [
    "project",
    "sector",
    "state",
    "announced",
    "promised_capital_usd",
    "promised_jobs",
    "promised_first_output",
    "actual_first_output",
    "current_status",
    "lag_years",
    "slip_years",
    "verification_tier",
    "notes",
]

# Provenance columns (the enriched "screen" shape). Optional as columns, but a
# verified tier that lacks them can only ever be trusted on faith.
#
# The two `*_date_source` columns exist because one URL often cannot carry two
# facts. `promise_source` says a promise was made; `promised_date_source` says
# when it was for. `status_source` says the plant runs today; `actual_date_source`
# says when it first produced. Twenty-two of the first 112 rows needed exactly
# that split -- a 2025 earnings release proves a mill is at volume and can never
# also date its 2021 first coil -- and without the second column the only way to
# record the date was to overwrite the evidence of current operation.
PROVENANCE_COLUMNS = [
    "promise_source",
    "status_source",
    "flag",
    "promised_date_source",
    "actual_date_source",
]

KNOWN_COLUMNS = set(REQUIRED_COLUMNS) | set(PROVENANCE_COLUMNS)

# Sector vocabulary. The pipeline covers a **defined set of manufacturing
# sectors** (it is no longer sector-agnostic). This BASE set is the settled
# vocabulary; it stays *extensible* two ways so a genuinely new manufacturing
# sector can be added:
#   * Claude Code / a human edits this set (a code change), or
#   * the API path calls register_sector() at runtime (a data change), which
#     appends to the JSON registry below.
# A sector outside the live vocabulary is an ERROR -- use one of these, or add
# the new manufacturing sector first (edit SECTORS / register_sector()); see
# sector_status().
SECTORS = {
    "Aerospace and Defense",
    "Auto Assembly",
    "Battery",
    "Chemicals and Plastics",
    "Food and Beverage",
    "Machinery",
    "Pharmaceuticals",
    "Semiconductors",
    "Solar",
    "Steel",
    "Other",
}

# Runtime-registered sectors live here (the API path's "add onto the schema
# through a function", as distinct from editing SECTORS in code). Stdlib-only so
# the checker can read it in CI without any pipeline deps.
SECTOR_REGISTRY_PATH = Path(__file__).resolve().parent / "sector_registry.json"


def load_registered_sectors() -> set:
    """The sectors added at runtime via register_sector() (may be empty)."""
    try:
        data = json.loads(SECTOR_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return set()
    return {str(s).strip() for s in data if str(s).strip()}


def all_sectors() -> set:
    """The full live vocabulary: the base set plus any runtime registrations."""
    return set(SECTORS) | load_registered_sectors()


def register_sector(name: str) -> bool:
    """Add `name` to the runtime sector registry file. Returns True if newly
    added, False if blank or already known. This is the function the API path
    uses to extend the schema without editing code."""
    v = (name or "").strip()
    if not v or v in all_sectors():
        return False
    updated = sorted(load_registered_sectors() | {v})
    SECTOR_REGISTRY_PATH.write_text(json.dumps(updated, indent=2), encoding="utf-8")
    return True

# US postal abbreviations (50 states + DC + inhabited territories).
US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR", "GU", "VI", "AS", "MP",
}

# Verification tiers. These measure HOW DEEPLY a row was checked by a person,
# not where the citation came from:
#   P  -- provisional. Extracted, but nobody has confirmed it against a source.
#   V1 -- a person confirmed each load-bearing cell against ONE source.
#   V2 -- a person confirmed each load-bearing cell against TWO INDEPENDENT
#         sources (different origins: a company release and independent trade
#         press count as two; a wire story republished twice counts as one).
# A cell is a single tier or a slash-joined pair (e.g. "V1/P" == the announcement
# is verified, first output is still provisional).
TIER_TOKENS = {"V1", "V2", "P"}

# Sentinel tokens allowed in first-output cells that carry no calendar date.
DATE_SENTINELS = {"pending", "never", "unconfirmed", "n/a", "tbd", "open"}

# A missing value that arrived as text. These are what a serializer writes when
# it is handed nothing -- Python's str(None) is "None", JavaScript's is "null"
# or "undefined" -- and they are not data, they are the absence of data wearing
# its coat. Stored as-is they are worse than an empty cell, because every check
# downstream sees a present value and waves it through. One reached the N=20
# batch: promised_date_source held the four characters "None", and it was caught
# only because that column happens to be URL-checked. In `current_status` or
# `project` nothing would have noticed at all.
NULL_STRINGS = {"none", "null", "nan", "nil", "undefined", "n/a", "na", "-", "--"}

# In the two first-output columns some of those words are real answers rather
# than absences: 'n/a' is a documented DATE_SENTINEL meaning "no calendar date".
# Exempting the whole column was too broad -- it let 'None' stand in a date cell
# too. Only the overlap is exempt, so a genuine sentinel survives and a
# stringified null is still blanked wherever it lands.
DATE_COLUMN_NULL_STRINGS = NULL_STRINGS - DATE_SENTINELS

# The inclusion floor (updated per prompts/prompt_source_collected.md): a project must
# clear EITHER announced capital >= $100M OR >= 200 promised jobs. A row is out
# of scope only if it falls below BOTH floors. (This is the looser OR rule; the
# prototype's explore-filter lets you probe AND / other thresholds separately.)
CAPITAL_FLOOR_USD = 100_000_000
JOBS_FLOOR = 200

YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
YEAR_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
URL_RE = re.compile(r"^https?://", re.IGNORECASE)

ERROR = "ERROR"
WARN = "WARN"


@dataclass
class Issue:
    row: int          # 1-indexed data row (0 = file-level)
    project: str
    column: str
    level: str
    message: str


# --------------------------------------------------------------------------- #
# Cell-level validators (return None if OK, else a message string)            #
# --------------------------------------------------------------------------- #

def check_required_nonempty(value: str) -> str | None:
    if value is None or value.strip() == "":
        return "required cell is empty"
    return None


def check_year_month(value: str) -> str | None:
    """The `announced` anchor: strict YYYY-MM. This is the row's identity and the
    denominator for every lag/slip figure, so it is not allowed to be fuzzy."""
    v = (value or "").strip()
    if not YEAR_MONTH_RE.match(v):
        return f"must be YYYY-MM (the row anchor), got {value!r}"
    month = int(v[5:7])
    if not 1 <= month <= 12:
        return f"month out of range in {value!r}"
    return None


def check_flexible_date(value: str) -> str | None:
    """promised_/actual_first_output: a 4-digit year (optionally with a month,
    quarter, or parenthetical qualifier) OR a recognized sentinel."""
    v = (value or "").strip()
    if v == "":
        return "empty (use a year or a sentinel like 'pending'/'never')"
    if any(tok in v.lower() for tok in DATE_SENTINELS):
        return None
    if YEAR_RE.search(v):
        return None
    return f"no 4-digit year and no recognized sentinel in {value!r}"


def check_int(value: str) -> tuple[int | None, str | None]:
    v = (value or "").strip().replace(",", "").replace("_", "")
    if v == "":
        return None, "required numeric cell is empty"
    if not re.fullmatch(r"\d+", v):
        return None, f"must be a non-negative integer, got {value!r}"
    return int(v), None


def sector_status(value: str) -> tuple[str, str] | None:
    """Sector must be one of the defined manufacturing sectors. Returns None if
    OK, else (level, message):
      * empty          -> ERROR (sector is required)
      * unknown value  -> ERROR (outside the vocabulary -- add it first if it's a
                          genuinely new manufacturing sector)."""
    v = (value or "").strip()
    if v == "":
        return ERROR, "sector is required (empty cell)"
    if v not in all_sectors():
        return ERROR, (
            f"{v!r} is not in the sector vocabulary {sorted(all_sectors())}; "
            "use one of these, or if it's a genuinely new manufacturing sector "
            "add it first (Claude Code: add to SECTORS; API: register_sector())"
        )
    return None


def check_state(value: str) -> str | None:
    if (value or "").strip().upper() not in US_STATES:
        return f"{value!r} is not a valid US state/territory abbreviation"
    return None


def check_tier(value: str) -> str | None:
    v = (value or "").strip()
    if v == "":
        return "verification_tier is empty"
    parts = v.split("/")
    bad = [p for p in parts if p not in TIER_TOKENS]
    if bad:
        return f"unknown tier token(s) {bad}; allowed: {sorted(TIER_TOKENS)} (slash-joined)"
    return None


def parse_lag(value: str) -> tuple[float | None, bool, str | None]:
    """'3.9' -> (3.9, False, None); '8 open'/'8+' -> (8.0, True, None);
    'n/a' -> (None, False, None). Mirrors plot_promised_vs_produced.parse_lag so
    the checker and the chart agree on what a lag cell means.

    Also understands the standardized numeric sentinels the pipeline now writes
    (pipeline/dates.py): -1 == "to be completed" (treated as open), -2 ==
    "cancelled". These keep the lag/slip columns clean floats."""
    v = str(value if value is not None else "").strip()
    if v == "" or v.lower() == "n/a":
        return None, False, None
    if v in ("-1", "-1.0"):   # "to be completed" -- not yet at first output
        return None, True, None
    if v in ("-2", "-2.0"):   # "cancelled" -- promise never delivered
        return None, False, None
    if v in ("-3", "-3.0"):   # "no promise recorded" -- nothing to measure against
        return None, False, None
    if v in ("-4", "-4.0"):   # "produced, date unknown" -- an event, not censored
        return None, False, None
    is_open = ("+" in v) or ("open" in v.lower())
    num = "".join(ch for ch in v if ch.isdigit() or ch == ".")
    if num.count(".") > 1 or num == "":
        return None, is_open, f"cannot parse a lag number from {value!r}"
    return float(num), is_open, None


def check_url(value: str) -> str | None:
    """Provenance cells may hold one or more URLs separated by ';' or whitespace."""
    v = (value or "").strip()
    if v == "":
        return None  # emptiness is handled by the verifiability rule, not here
    for token in re.split(r"[;\s]+", v):
        if token and not URL_RE.match(token):
            return f"provenance value {token!r} does not look like a URL"
    return None


# --------------------------------------------------------------------------- #
# Row-level and file-level validation                                         #
# --------------------------------------------------------------------------- #

def validate_row(rownum: int, row: dict[str, str], has_prov: dict[str, bool]) -> list[Issue]:
    issues: list[Issue] = []
    project = (row.get("project") or "").strip() or "<no project>"

    # A missing value that arrived as text, in ANY column. This runs first and
    # over everything, because the failure it catches is a cell that looks
    # populated to every check after it. `n/a` is a legitimate DATE_SENTINEL, so
    # the two first-output columns are exempt -- there the word is a real answer.
    for col, value in row.items():
        bad = (DATE_COLUMN_NULL_STRINGS
               if col in ("promised_first_output", "actual_first_output")
               else NULL_STRINGS)
        if isinstance(value, str) and value.strip().lower() in bad:
            issues.append(Issue(rownum, project, col, ERROR,
                                f"{value.strip()!r} is a missing value written as "
                                "text; the cell should be empty"))

    def add(col: str, level: str, msg: str) -> None:
        issues.append(Issue(rownum, project, col, level, msg))

    # project
    if (m := check_required_nonempty(row.get("project", ""))):
        add("project", ERROR, m)

    # sector (a defined manufacturing sector: empty or out-of-vocabulary is an ERROR)
    if (res := sector_status(row.get("sector", ""))):
        add("sector", res[0], res[1])

    # state
    if (m := check_state(row.get("state", ""))):
        add("state", ERROR, m)

    # announced (the anchor)
    if (m := check_year_month(row.get("announced", ""))):
        add("announced", ERROR, m)

    # capital + jobs, then the inclusion floor
    capital, cap_err = check_int(row.get("promised_capital_usd", ""))
    if cap_err:
        add("promised_capital_usd", ERROR, cap_err)
    jobs, jobs_err = check_int(row.get("promised_jobs", ""))
    if jobs_err:
        add("promised_jobs", ERROR, jobs_err)
    if capital is not None and jobs is not None:
        if capital < CAPITAL_FLOOR_USD and jobs < JOBS_FLOOR:
            add(
                "promised_capital_usd", ERROR,
                f"inclusion rule fails: requires capital >= ${CAPITAL_FLOOR_USD:,} "
                f"OR jobs >= {JOBS_FLOOR:,}; got capital ${capital:,} and jobs {jobs:,}",
            )

    # first-output cells
    if (m := check_flexible_date(row.get("promised_first_output", ""))):
        add("promised_first_output", ERROR, m)
    if (m := check_flexible_date(row.get("actual_first_output", ""))):
        add("actual_first_output", ERROR, m)

    # current_status
    if (m := check_required_nonempty(row.get("current_status", ""))):
        add("current_status", ERROR, m)

    # lag / slip
    #
    # An unparseable lag is an error. An *open* lag is not flagged at all: it
    # means the plant has not produced yet, which is the ordinary state of a
    # tracked project and the very thing this Scoreboard exists to record. It
    # was a WARN, which put "not yet publishable" on 72% of the rows that had
    # already been published, and left CLEAN identifying finished factories
    # rather than well-formed rows. The fact is already on the row, in
    # actual_first_output and lag_years; the checker does not need to repeat it
    # as a problem.
    _, _, lag_err = parse_lag(row.get("lag_years", ""))
    if lag_err:
        add("lag_years", ERROR, lag_err)

    # verification tier
    tier = (row.get("verification_tier") or "").strip()
    if (m := check_tier(tier)):
        add("verification_tier", ERROR, m)

    # provenance URL shape (only if the columns exist at all)
    for col in ("promise_source", "status_source",
                "promised_date_source", "actual_date_source"):
        if has_prov[col] and (m := check_url(row.get(col, ""))):
            add(col, ERROR, m)

    # --- verifiability rule: a verified tier must show its work ---
    claims_verified = "V1" in tier or "V2" in tier
    if claims_verified:
        if not has_prov["promise_source"] or not (row.get("promise_source") or "").strip():
            add("promise_source", WARN,
                f"tier {tier} claims verification but has no inline promise_source "
                "(spike checklist item 1: links belong in the CSV)")
        if not has_prov["status_source"] or not (row.get("status_source") or "").strip():
            add("status_source", WARN,
                f"tier {tier} claims verification but has no inline status_source")

    # --- open-flag surfacing ---
    if has_prov["flag"]:
        flag = (row.get("flag") or "").strip()
        if flag and not flag.lower().startswith(("none", "resolved", "n/a")):
            add("flag", WARN, f"unresolved flag: {flag[:80]}")

    return issues


def validate_file(path: str) -> tuple[list[Issue], int]:
    issues: list[Issue] = []
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []

        # File-level: required columns present? unknown columns?
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        for c in missing:
            issues.append(Issue(0, "<file>", c, ERROR, "required column missing from header"))
        for c in header:
            if c not in KNOWN_COLUMNS:
                issues.append(Issue(0, "<file>", c, WARN, "unrecognized column (not in schema)"))

        has_prov = {c: (c in header) for c in PROVENANCE_COLUMNS}
        for c in PROVENANCE_COLUMNS:
            if not has_prov[c]:
                issues.append(Issue(0, "<file>", c, WARN,
                                    "provenance column absent -- verifiability cannot be proven for it"))

        seen: dict[str, int] = {}
        nrows = 0
        for i, row in enumerate(reader, start=1):
            nrows += 1
            key = (row.get("project") or "").strip()
            if key:
                if key in seen:
                    issues.append(Issue(i, key, "project", ERROR,
                                        f"duplicate project key (first seen on data row {seen[key]})"))
                else:
                    seen[key] = i
            issues.extend(validate_row(i, row, has_prov))

    return issues, nrows


# --------------------------------------------------------------------------- #
# Reporting                                                                    #
# --------------------------------------------------------------------------- #

def report(path: str, issues: list[Issue], nrows: int, quiet: bool) -> None:
    errors = [x for x in issues if x.level == ERROR]
    warns = [x for x in issues if x.level == WARN]

    print("=" * 72)
    print(f"PROMISED VS. PRODUCED -- schema check: {path}")
    print(f"{nrows} data rows | {len(errors)} errors | {len(warns)} warnings")
    print("=" * 72)

    if not quiet and issues:
        by_row: dict[int, list[Issue]] = {}
        for x in issues:
            by_row.setdefault(x.row, []).append(x)
        for rownum in sorted(by_row):
            label = "FILE" if rownum == 0 else f"row {rownum}"
            proj = by_row[rownum][0].project
            print(f"\n[{label}] {proj}")
            for x in sorted(by_row[rownum], key=lambda i: (i.level != ERROR, i.column)):
                print(f"  {x.level:5} {x.column:22} {x.message}")

    print("\n" + "-" * 72)
    if errors:
        print(f"RESULT: FAIL -- {len(errors)} schema error(s) must be fixed.")
    elif warns:
        print(f"RESULT: PASS with {len(warns)} warning(s) "
              "(not yet publishable; clear warnings before promoting to Verify).")
    else:
        print("RESULT: CLEAN -- schema valid and every cell shows its work.")
    print("-" * 72)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate a Promised vs. Produced scoreboard CSV against the canonical schema.")
    ap.add_argument("csv_path", help="path to the scoreboard CSV")
    ap.add_argument("--strict", action="store_true", help="treat warnings as failures (Verify gate)")
    ap.add_argument("--quiet", action="store_true", help="print the summary only, not per-row detail")
    args = ap.parse_args(argv)

    try:
        issues, nrows = validate_file(args.csv_path)
    except FileNotFoundError:
        print(f"error: file not found: {args.csv_path}", file=sys.stderr)
        return 2

    report(args.csv_path, issues, nrows, args.quiet)

    errors = [x for x in issues if x.level == ERROR]
    warns = [x for x in issues if x.level == WARN]
    if errors:
        return 1
    if args.strict and warns:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
