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
  admissible and carries something a person still has to settle (a verified tier
  with no inline sources, or an open/unresolved flag). ERRORs fail the run;
  WARNs fail only under --strict.
- A WARN does NOT block promotion, and the Verify gate does not run --strict.
  That is deliberate for the open-flag warning, which is the common one: no
  command edits a flag on a Screen row, so the only thing that can resolve one
  is `verify-promote`, which rewrites it into a resolution record. Telling a
  person to clear that warning first would be telling them to do the one thing
  the CLI gives them no way to do. --strict is for validating a CSV by hand
  before it is loaded, where fixing the file is the workflow.
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline import settings as _criteria  # noqa: E402

# --------------------------------------------------------------------------- #
# Controlled vocabularies                                                      #
# --------------------------------------------------------------------------- #

# The required core columns (the 13-column v0 "source" shape). Every scoreboard must
# carry these, in any order.
REQUIRED_COLUMNS = [
    "project",
    "sector",
    # The country the facility is in, and its subdivision inside that country.
    # `country` exists from the start even though the Scoreboard is US-only,
    # because the plan covers allied countries and adding one should be a config
    # change in settings.py rather than a migration of every stored row. A blank
    # cell means the phase's only country.
    "country",
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
    # WHICH RULES ADMITTED THIS ROW. The Scoreboard is built by sweeping at a
    # high threshold and lowering it later, and without this stamp a later,
    # looser sweep is indistinguishable from the earlier one -- "no $300M plants
    # in 2019" and "we were not looking for $300M plants in 2019" collapse into
    # the same silence. It is also what lets a row be checked against the rule
    # that admitted it rather than whatever is active now.
    "criteria_id",
]

KNOWN_COLUMNS = set(REQUIRED_COLUMNS) | set(PROVENANCE_COLUMNS)

# Sector vocabulary, the country list and the size floor all live in
# pipeline/settings.py now -- one place for every rule about what counts as a
# project. These names are kept as thin pass-throughs because the rest of the
# package and the tests import them from here.
SECTORS = _criteria.SECTORS


def all_sectors() -> set:
    """The sector vocabulary of the phase in effect."""
    return set(_criteria.active().sectors)


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
#
# The two columns do NOT share a vocabulary, and treating them as if they did is
# what this split exists to stop. One flat set of six was offered to the
# extractor for three behaviours dates.py distinguishes, so four were synonyms
# picked by feel -- and `unconfirmed` landed in promised_first_output on 40 of
# the 44 projects with no promised date. There it cannot mean anything:
# dates.py reads it as "the event happened, nobody dated it", and a promise is
# not an event that can have happened. The only thing missing in that column is
# the promise itself.
PROMISED_SENTINELS = {"n/a"}                              # no promise was stated
ACTUAL_SENTINELS = {"pending", "never", "unconfirmed"}    # not yet / cancelled / undated

# Retired: still parsed, so rows written before the vocabulary was narrowed keep
# resolving exactly as they did, but nothing may write them now.
RETIRED_SENTINELS = {"tbd", "open"}

# The union, for anything that asks "is this word a sentinel at all" without
# caring which column it is in -- DATE_COLUMN_NULL_STRINGS below is the caller
# that matters, and `n/a` has to survive it in both columns.
DATE_SENTINELS = PROMISED_SENTINELS | ACTUAL_SENTINELS | RETIRED_SENTINELS

SENTINELS_FOR = {
    "promised_first_output": PROMISED_SENTINELS,
    "actual_first_output": ACTUAL_SENTINELS,
}

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


def check_flexible_date(value: str, allowed: set | None = None) -> str | None:
    """promised_/actual_first_output: a 4-digit year (optionally with a month,
    quarter, or parenthetical qualifier) OR a sentinel this column may carry.

    `allowed` is the column's own sentinel set, from SENTINELS_FOR. Omitted, any
    sentinel passes -- which is what this function did for every caller until
    `unconfirmed` turned up in 40 promised cells, asserting about a promise the
    one thing only an actual first output can assert.

    A real date still wins over a stray sentinel word: "2019 (pending permits)"
    carries a year and is fine in either column.
    """
    v = (value or "").strip()
    ok = allowed if allowed is not None else DATE_SENTINELS
    if v == "":
        return ("empty (use a year or "
                + " / ".join(f"{t!r}" for t in sorted(ok)) + ")")
    if any(tok in v.lower() for tok in ok):
        return None
    if YEAR_RE.search(v):
        return None
    wrong = sorted(t for t in DATE_SENTINELS - ok if t in v.lower())
    if wrong:
        use = " / ".join(f"{t!r}" for t in sorted(ok)) + ", or a 4-digit year"
        # The one that will actually happen, and the one whose reason is not
        # self-evident from the word.
        if "unconfirmed" in wrong and ok == PROMISED_SENTINELS:
            return ("'unconfirmed' says first output happened but no source "
                    "dated it, which cannot be true of a promise. If no source "
                    "stated a promised date, that is 'n/a'")
        return f"{value!r} is not a sentinel this column may carry. Use {use}"
    return f"no 4-digit year and no recognized sentinel in {value!r}"


def check_int(value: str) -> tuple[int | None, str | None]:
    v = (value or "").strip().replace(",", "").replace("_", "")
    if v == "":
        return None, "required numeric cell is empty"
    if not re.fullmatch(r"\d+", v):
        return None, f"must be a non-negative integer, got {value!r}"
    return int(v), None


def sector_status(value: str, crit=None) -> tuple[str, str] | None:
    """Sector must be one of the defined manufacturing sectors. Returns None if
    OK, else (level, message):
      * empty          -> ERROR (sector is required)
      * unknown value  -> ERROR (outside the vocabulary -- add it to
                          pipeline/settings.py first if it is a genuinely new
                          manufacturing sector)."""
    crit = crit or _criteria.active()
    v = (value or "").strip()
    if v == "":
        return ERROR, "sector is required (empty cell)"
    if v not in crit.sectors:
        return ERROR, (
            f"{v!r} is not in the sector vocabulary {sorted(crit.sectors)}; "
            "use one of these, or if it is a genuinely new manufacturing sector "
            "add it to SECTORS in pipeline/settings.py first"
        )
    return None


def check_country(value: str, crit=None) -> str | None:
    """The country must be one this phase covers.

    Empty is allowed and means the phase's only country -- the Scoreboard was
    US-only before the column existed, so a blank cell on an older row is not a
    defect. It becomes one the moment a phase covers more than one country,
    because then the cell is genuinely load-bearing.
    """
    crit = crit or _criteria.active()
    v = (value or "").strip().upper()
    if v == "":
        if len(crit.countries) > 1:
            return (f"country is required: phase {crit.id!r} covers "
                    f"{', '.join(crit.countries)}, so a blank cell is ambiguous")
        return None
    if v not in crit.countries:
        return (f"{value!r} is outside phase {crit.id!r}, which covers "
                f"{', '.join(crit.countries)}")
    return None


def check_state(value: str, crit=None) -> str | None:
    """The subdivision must be valid in one of the phase's countries."""
    crit = crit or _criteria.active()
    valid = crit.subdivisions()
    if (value or "").strip().upper() not in valid:
        where = "/".join(crit.countries)
        return f"{value!r} is not a valid {where} state/province/subdivision code"
    return None


def check_announced_from(value: str, crit=None) -> str | None:
    """The announcement must fall inside the phase's window.

    README stated an announced-from date and until now nothing enforced it --
    the rule lived in prose in four files and in no code at all.
    """
    crit = crit or _criteria.active()
    v = (value or "").strip()
    if not YEAR_MONTH_RE.match(v):
        return None          # shape is check_year_month's job, not this one
    if v < crit.announced_from:
        return (f"announced {v} is before {crit.announced_from}, the earliest "
                f"in scope for phase {crit.id!r}")
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

def validate_row(rownum: int, row: dict[str, str], has_prov: dict[str, bool],
                 crit=None) -> list[Issue]:
    """Validate one row against a phase's inclusion rules.

    `crit` is the phase to judge by. Callers pass the phase that ADMITTED the
    row (`criteria.get(row['criteria_id'])`), not whatever is active now, so
    lowering or raising a threshold can never retroactively invalidate data
    that was in scope when it was collected. Omitted, it falls back to the
    active phase, which is right for a row being admitted for the first time.
    """
    crit = crit or _criteria.active()
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
    if (res := sector_status(row.get("sector", ""), crit)):
        add("sector", res[0], res[1])

    # where: the country in scope, then a subdivision valid inside it
    if (m := check_country(row.get("country", ""), crit)):
        add("country", ERROR, m)
    if (m := check_state(row.get("state", ""), crit)):
        add("state", ERROR, m)

    # announced (the anchor), then the phase's window
    if (m := check_year_month(row.get("announced", ""))):
        add("announced", ERROR, m)
    elif (m := check_announced_from(row.get("announced", ""), crit)):
        add("announced", ERROR, m)

    # capital + jobs, then the inclusion floor.
    #
    # The floor is usually an OR -- capital OR jobs -- so EITHER figure
    # on its own can put a row in scope. This used to demand both cells parse
    # before it would evaluate that OR, which made a missing figure fatal even
    # when the other one settled the question. Two rows of the N=100 run were
    # rejected that way -- ES Foundry Greenwood and Meyer Burger Goodyear, both
    # clearly over the jobs floor of the day, both failing because no source
    # printed a dollar figure. The extractor was right to leave the cell empty;
    # the checker was wrong to call that a defect. That reasoning is about the
    # OR, not about where the floor sits, so it survives the floor moving.
    cap_raw = str(row.get("promised_capital_usd", "") or "").strip()
    jobs_raw = str(row.get("promised_jobs", "") or "").strip()
    capital, cap_err = check_int(cap_raw)
    jobs, jobs_err = check_int(jobs_raw)

    # A cell that HOLDS something unreadable is always an error -- that is a bad
    # value, not a missing one, and no other cell can excuse it.
    if cap_raw and cap_err:
        add("promised_capital_usd", ERROR, cap_err)
    if jobs_raw and jobs_err:
        add("promised_jobs", ERROR, jobs_err)

    # An EMPTY cell is fatal only when the row cannot be shown to be in scope
    # without it. One figure over its floor is the whole test.
    clears = crit.clears(capital, jobs)
    if not clears:
        if capital is not None and jobs is not None:
            add(
                "promised_capital_usd", ERROR,
                f"inclusion rule fails: phase {crit.id!r} requires "
                f"{crit.describe()}; got capital ${capital:,} and jobs {jobs:,}",
            )
        else:
            # Neither known figure clears the floor and at least one is missing,
            # so the row may or may not qualify. Say that, on the cell that would
            # settle it -- "required numeric cell is empty" pointed at the
            # symptom and left the reader to work out why it mattered.
            known = (f"capital ${capital:,}" if capital is not None else
                     f"jobs {jobs:,}" if jobs is not None else "neither figure")
            for col, val in (("promised_capital_usd", capital),
                             ("promised_jobs", jobs)):
                if val is None:
                    add(col, ERROR,
                        f"size floor cannot be established: phase {crit.id!r} "
                        f"needs {crit.describe()}, and {known} is below it with "
                        f"this cell empty")

    # first-output cells
    if (m := check_flexible_date(row.get("promised_first_output", ""),
                                 SENTINELS_FOR["promised_first_output"])):
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
    crit = _criteria.active()
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
            issues.extend(validate_row(i, row, has_prov, crit))

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
              "(admissible; each warning names something for a person to settle).")
    else:
        print("RESULT: CLEAN -- schema valid and every cell shows its work.")
    print("-" * 72)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate a Promised vs. Produced scoreboard CSV against the canonical schema.")
    ap.add_argument("csv_path", help="path to the scoreboard CSV")
    ap.add_argument("--strict", action="store_true",
                    help="treat warnings as failures (for validating a CSV by "
                         "hand; the Verify gate does NOT use this)")
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
