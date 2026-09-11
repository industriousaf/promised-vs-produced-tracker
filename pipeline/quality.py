"""quality.py -- is this Tracker any good?

Row counts cannot answer that. "23 screened" is a finished Tracker or a 23-item
backlog depending on facts the counts do not carry, and the question a reader of
the paper will actually ask is not how many rows there are but how many of them
can carry the claim.

So this measures five things, and deliberately does NOT blend them into one
score. A single number invites an argument about the weights, and a referee will
ask what is in it. Five bars, each with the rows behind it, is a triage screen:
it says which rows to go fix, not what grade the Tracker deserves.

The measures, in the order they matter for publication:

  publishable      a human has verified it. Verify is the only gate that makes
                   a row part of the published Tracker, so this is the one
                   that gates the paper.
  measurable slip  promised AND actual first-output dates are both known. Slip
                   is the promised-vs-produced quantity; a row missing either
                   date contributes nothing to it, however complete it looks.
  measurable lag   announced AND actual are both known. Weaker than slip and
                   available on more rows -- announcement-anchored gestation
                   survives a missing promise.
  sourced          both source links are real, resolvable URLs.
  clean            the deterministic checker does not FAIL it.

Plus the open-flag split, which is the thing the raw flag count cannot tell you.
"""

from __future__ import annotations

import json
import re
import sqlite3

from pipeline.schema_check import check_url

# A flag that says "I could not read the page" is a very different problem from
# one that says "the page does not support the claim". The first is an access
# failure and is fixed by fetching better; the second is a fact about the world
# and is fixed only by a human. Lumping them together is why 22 of 23 rows
# carried an "unresolved flag" warning that nobody could act on.
_PROVENANCE_FLAG = re.compile(
    r"\b40[0-9]\b|http\s*\d{3}|forbidden|timed?\s*out|timeout|unreachable|akamai"
    r"|cloudflare|could not be (?:scraped|read|reached|opened)|did not render"
    r"|web\.archive|wayback|paywall|video page|no article",
    re.I,
)


def _has_url(value) -> bool:
    v = (value or "").strip()
    return bool(v) and check_url(v) is None


def classify_flag(text: str | None) -> str | None:
    """'provenance' | 'substantive' | None (no flag)."""
    t = (text or "").strip()
    if not t:
        return None
    return "provenance" if _PROVENANCE_FLAG.search(t) else "substantive"


def measure(conn: sqlite3.Connection) -> dict:
    """The five measures plus the flag split. Every value carries its rows."""
    rows = conn.execute("SELECT * FROM screen_extracted").fetchall()
    total = len(rows)

    published = {r["project"] for r in conn.execute(
        "SELECT project FROM verify_verified")}

    latest = {}
    for r in conn.execute("SELECT screen_extracted_id, result_status, id "
                          "FROM screen_check ORDER BY id"):
        latest[r["screen_extracted_id"]] = r["result_status"]

    bars, flags = [], {"provenance": [], "substantive": [], "none": []}
    hits = {k: [] for k in ("publishable", "slip", "lag", "sourced", "clean")}

    for r in rows:
        rid = r["id"]
        if r["project"] in published:
            hits["publishable"].append(rid)
        # Ask the resolved dates, not the sign of lag/slip.
        #
        # "slip_years >= 0" was the obvious test and it is wrong twice over. A
        # negative slip is a real measurement -- it means the project delivered
        # EARLY, and row #10 did, by 0.3 years -- so that test discards genuine
        # observations as unmeasurable. And the sentinels are themselves plain
        # negative floats, so a project that happened to deliver exactly one
        # year early would be indistinguishable from "not produced yet".
        #
        # Both dates being resolved is what actually decides whether the
        # quantity exists, so ask that instead and the sign never comes into it.
        if r["promised_first_output_dt"] and r["actual_first_output_dt"]:
            hits["slip"].append(rid)
        if r["announced_dt"] and r["actual_first_output_dt"]:
            hits["lag"].append(rid)
        # promised_date_source is optional, but a row that supplies one is
        # making a citation and it has to be a real link. #18 supplied the four
        # characters "None", which is exactly the case this must not wave past.
        pds = (r["promised_date_source"] or "").strip()
        if (_has_url(r["promise_source"]) and _has_url(r["status_source"])
                and (not pds or _has_url(pds))):
            hits["sourced"].append(rid)
        if latest.get(rid) != "FAIL":
            hits["clean"].append(rid)
        kind = classify_flag(r["flag"])
        flags[kind or "none"].append(rid)

    for key, label, why in (
        ("publishable", "Publishable",     "a human has verified it"),
        ("slip",        "Can measure slip", "promised AND actual dates known"),
        ("lag",         "Can measure lag",  "announced AND actual dates known"),
        ("sourced",     "Sourced",          "both source links are real URLs"),
        ("clean",       "Structurally clean", "the deterministic check does not FAIL"),
    ):
        ids = hits[key]
        bars.append({
            "key": key, "label": label, "why": why,
            "n": len(ids), "total": total,
            "pct": (100.0 * len(ids) / total) if total else 0.0,
            "missing": [r["id"] for r in rows if r["id"] not in set(ids)],
        })

    return {"total": total, "bars": bars, "flags": flags}


def render_bar(pct: float, width: int = 22) -> str:
    """A bar in eighths, so a small share is visible instead of rounding to
    nothing -- 4% of 22 characters is otherwise an empty line."""
    eighths = round(pct / 100.0 * width * 8)
    full, rest = divmod(eighths, 8)
    return ("█" * full + (" ▏▎▍▌▋▊▉"[rest] if rest else "")).ljust(width, "·")
