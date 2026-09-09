#!/usr/bin/env python3
"""coverage.py -- how much of a reference list does the Scoreboard actually have?

Recall against a reference list is the only completeness measure available: the
true universe of US manufacturing projects is not published by anyone, so the
denominator has to come from an enumerable list (a tracker, a CHIPS award list,
a DOE loan cohort). This reports how many of that list's projects are present.

    python3 pipeline/coverage.py --against path/to/reference.csv
    python3 pipeline/coverage.py --against ref.csv --min-capital 1000000000
    python3 pipeline/coverage.py --against ref.csv --stage verify

WHY THIS IS NOT A NAME COMPARISON
---------------------------------
The same project carries different names in different sources, and different
projects carry similar names. Comparing names alone fails in both directions:

    TSMC Fab 1 Phoenix (AZ)        == TSMC Arizona Fabs (AZ)         same site
    Nucor plate mill Brandenburg (KY) != Nucor Steel Mill (WV)       different sites
    Intel New Albany (OH)          != Intel Chandler Expansion (AZ)  different sites

Measured on the historical hand-built dataset, exact-name matching reports 0/10
and naive substring matching reports 9/10. The truth is 7/10. A recall figure
built on either would be wrong, and wrong in the direction that flatters the
pipeline.

So matching is gated on **state** first -- a project is a company plus a physical
site, and the state is the one identifier both sources record reliably -- and
only then scored on name overlap within that state.

THREE OUTCOMES, NOT TWO
-----------------------
Every reference project lands in one of three buckets, because pretending a
fuzzy match is a boolean is how a recall number becomes fiction:

    covered    high confidence the project is present
    missing    high confidence it is not
    ambiguous  a candidate exists but the call belongs to a person

`ambiguous` is reported separately and never counted as covered. Recall is
stated as a range: covered/total at worst, (covered+ambiguous)/total at best.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCOREBOARD_ROOT = HERE.parent
# Needed only when run as a script (`python3 pipeline/coverage.py`): sys.path[0]
# is then pipeline/, and `pipeline.db` below is not importable without the root.
if str(SCOREBOARD_ROOT) not in sys.path:
    sys.path.insert(0, str(SCOREBOARD_ROOT))

# The ONE resolver. This file used to carry its own copy, which ignored the
# MEDALLION_DB alias and the web app's active-database picker -- so `coverage`
# could quietly measure recall against a different database than `status` was
# reporting on.
from pipeline.db import db_path  # noqa: E402

# Words that describe a facility rather than identify it. Stripped before
# scoring so "TSMC Fab 1 Phoenix" and "TSMC Arizona Fabs" are not penalised for
# disagreeing about the word "fab". Company names that happen to contain one of
# these (First Solar, Nucor Steel) still match on their other tokens.
_DESCRIPTORS = {
    "plant", "plants", "facility", "facilities", "fab", "fabs", "mill", "mills",
    "campus", "complex", "expansion", "manufacturing", "factory", "gigafactory",
    "park", "center", "centre", "works", "project", "projects", "site",
    "megafab", "megafabs", "production", "assembly", "the", "and", "of", "at",
    "inc", "llc", "corp", "corporation", "co", "company", "group", "holdings",
    "new", "us", "usa", "american", "america",
}


def _tokens(name: str) -> set[str]:
    """Significant lowercase tokens: punctuation dropped, descriptors removed."""
    raw = re.split(r"[^a-z0-9]+", (name or "").lower())
    return {t for t in raw if t and t not in _DESCRIPTORS}


def _score(a: str, b: str) -> float:
    """0..1 similarity between two project names, already known to share a state.

    Jaccard over significant tokens, with a floor: sharing any distinctive token
    inside one state is strong evidence, because a state rarely holds two
    projects from the same company. "TI Sherman SM1/SM2" and "Texas Instruments
    Sherman Fabs" overlap only on "sherman", and that is enough."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    if not inter:
        return 0.0
    jaccard = len(inter) / len(ta | tb)
    return max(jaccard, 0.34 if inter else 0.0)


COVERED_AT = 0.34      # at least one distinctive token shared within the state
AMBIGUOUS_AT = 0.20    # something matched, but weakly -- a person should look


def _rows_from_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [r for r in csv.DictReader(fh)]


def _capital(value) -> int | None:
    s = str(value or "").strip()
    return int(s) if s.isdigit() else None


def compare(reference: list[dict], have: list[dict], min_capital: int | None = None):
    """Classify every reference project as covered / ambiguous / missing."""
    by_state: dict[str, list[dict]] = {}
    for h in have:
        by_state.setdefault((h["state"] or "").strip().upper(), []).append(h)

    covered, ambiguous, missing = [], [], []
    for r in reference:
        cap = _capital(r.get("promised_capital_usd"))
        if min_capital is not None and (cap is None or cap < min_capital):
            continue
        state = (r.get("state") or "").strip().upper()
        best, best_score = None, 0.0
        for cand in by_state.get(state, []):
            s = _score(r.get("project", ""), cand["project"])
            if s > best_score:
                best, best_score = cand, s
        entry = {"project": r.get("project", ""), "state": state, "capital": cap,
                 "match": best["project"] if best else None, "score": best_score}
        if best_score >= COVERED_AT:
            covered.append(entry)
        elif best_score >= AMBIGUOUS_AT:
            ambiguous.append(entry)
        else:
            missing.append(entry)
    return covered, ambiguous, missing


def _fmt_cap(c) -> str:
    return f"${c/1e9:>6.1f}B" if c else "    n/a"


# The pairs that define correct behaviour. Derived by hand from the historical
# corpora, and the reason this tool exists: a name comparison gets these wrong.
_SELFTEST = [
    # (reference name, reference state, candidate name, candidate state, same project?)
    ("TSMC Fab 1 Phoenix", "AZ", "TSMC Arizona Fabs", "AZ", True),
    ("Samsung Taylor", "TX", "Samsung Taylor Fab", "TX", True),
    ("Micron Clay NY", "NY", "Micron Megafab (Clay NY)", "NY", True),
    ("Intel New Albany", "OH", "Intel Silicon Heartland Fabs", "OH", True),
    ("TI Sherman SM1/SM2", "TX", "Texas Instruments Sherman Fabs", "TX", True),
    ("Hyundai Metaplant Ellabell", "GA", "Hyundai Motor Group Metaplant", "GA", True),
    ("Panasonic Energy De Soto", "KS", "Panasonic Energy Battery Plant", "KS", True),
    # Same company, different site -- these must NOT match.
    ("Nucor plate mill Brandenburg", "KY", "Nucor Steel Mill", "WV", False),
    ("Ultium Cells Spring Hill", "TN", "GM Orion EV + Ultium Battery Projects", "MI", False),
    ("Intel New Albany", "OH", "Intel Chandler Expansion", "AZ", False),
]


def selftest() -> int:
    """Check the matcher against pairs whose answer is known. No database needed."""
    bad = 0
    for ref, ref_st, cand, cand_st, expected in _SELFTEST:
        got = (ref_st == cand_st) and _score(ref, cand) >= COVERED_AT
        ok = got == expected
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {ref:32} vs {cand:38} "
              f"expected={'match' if expected else 'no match'}")
    print(f"\n  {len(_SELFTEST) - bad}/{len(_SELFTEST)} passed")
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Recall of the Scoreboard against a reference list of projects.")
    ap.add_argument("--against",
                    help="reference CSV; needs at least `project` and `state` columns")
    ap.add_argument("--db", default=None, help="scoreboard database (default: outputs/scoreboard.db)")
    ap.add_argument("--stage", choices=["screen", "verify"], default="screen",
                    help="measure what has been collected (screen) or published (verify)")
    ap.add_argument("--min-capital", type=int, default=None,
                    help="only score reference projects at or above this capital, "
                         "e.g. 1000000000 for the Phase 1 line")
    ap.add_argument("--show-covered", action="store_true", help="also list the matches")
    ap.add_argument("--selftest", action="store_true",
                    help="check the matcher against known pairs and exit")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if not args.against:
        raise SystemExit("--against is required (or use --selftest)")
    ref_path = Path(args.against)
    if not ref_path.exists():
        raise SystemExit(f"reference list not found: {ref_path}")
    reference = _rows_from_csv(ref_path)
    if reference and "project" not in reference[0]:
        raise SystemExit(f"{ref_path} has no `project` column")

    db = Path(args.db) if args.db else Path(db_path())
    if not db.exists():
        raise SystemExit(f"database not found: {db}")
    table = "screen_extracted" if args.stage == "screen" else "verify_verified"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    have = [dict(r) for r in conn.execute(f"SELECT project, state FROM {table}")]
    conn.close()

    covered, ambiguous, missing = compare(reference, have, args.min_capital)
    total = len(covered) + len(ambiguous) + len(missing)
    if total == 0:
        print("No reference projects to score (check --min-capital).")
        return 0

    print(f"reference : {ref_path.name}  ({total} projects scored"
          + (f", ≥ ${args.min_capital/1e9:.1f}B" if args.min_capital else "") + ")")
    print(f"scoreboard: {db.name}  [{table}]  {len(have)} rows")
    print()
    lo = 100 * len(covered) / total
    hi = 100 * (len(covered) + len(ambiguous)) / total
    print(f"  covered    {len(covered):>3}/{total}")
    print(f"  ambiguous  {len(ambiguous):>3}      (a person must decide these)")
    print(f"  missing    {len(missing):>3}")
    print()
    print(f"  RECALL {lo:.0f}%" + (f" to {hi:.0f}%  (depending on the ambiguous)" if ambiguous else ""))

    if missing:
        print(f"\n--- missing ({len(missing)}), largest first ---")
        for e in sorted(missing, key=lambda x: -(x["capital"] or 0)):
            print(f"  {_fmt_cap(e['capital'])}  {e['state']}  {e['project']}")
    if ambiguous:
        print(f"\n--- ambiguous ({len(ambiguous)}) -- same state, weak name overlap ---")
        for e in ambiguous:
            print(f"  {_fmt_cap(e['capital'])}  {e['state']}  {e['project']}")
            print(f"        closest: {e['match']}  (score {e['score']:.2f})")
    if args.show_covered and covered:
        print(f"\n--- covered ({len(covered)}) ---")
        for e in sorted(covered, key=lambda x: -(x["capital"] or 0)):
            print(f"  {_fmt_cap(e['capital'])}  {e['state']}  {e['project']}")
            print(f"        matched: {e['match']}  (score {e['score']:.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
