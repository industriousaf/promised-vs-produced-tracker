"""quality.py -- is this Tracker any good?

Row counts cannot answer that. "23 screened" is a finished Tracker or a 23-item
backlog depending on facts the counts do not carry, and the question a reader of
the paper will actually ask is not how many rows there are but how many of them
can carry the claim.

So this measures several things, and deliberately does NOT blend them into one
score. A single number invites an argument about the weights, and a referee will
ask what is in it. Separate measures, each with the projects behind them, make a
triage screen: it says which projects to go fix, not what grade the Tracker
deserves.

They come in two groups, because they answer different questions.

THE GATES are work. Every project short of one is somebody's next task, and the
three are a funnel: each set sits inside the one above.

  sourced          both source links are real, resolvable URLs.
  clean            the deterministic checker does not FAIL it.
  publishable      a human has verified it. Verify is the only gate that puts a
                   project in the published Tracker, so this is what gates the
                   paper.

THE FINDINGS are what the published projects say about the world, counted on the
projects that cleared all three gates:

  gestation lag    years from the announcement to first output.
  slip             years between the promised first output and the actual one.

They are counts, not scores, and they are not coloured. Both need a project to
have produced, and most of these plants have not: reporting that as a failing
percentage said the Tracker was broken when what it was reporting is how new
this construction wave is. What IS a gap here is a project producing with no
confirmed date, which a verifier can go and close, so those are named.

Plus the open-flag split, which is the thing the raw flag count cannot tell you.
"""

from __future__ import annotations

import json
import re
import sqlite3

from pipeline.dates import interpret_date
from pipeline.schema_check import check_url

# The two findings, defined once, because the dashboard and `tracker.py quality`
# both state them and a reader comparing the two must not find two definitions.
# `tip` is the hover note in the web panel and a printed line in the terminal:
# what the number is measured from, which is the question the label cannot
# answer on its own. A lag measured from the announcement is not the
# time-to-build of the literature, and saying so is the honest part.
FINDING_LABELS = {
    "lag": {
        "label": "Gestation lag",
        "why": "announced → first output",
        "tip": ("Years from the announcement to first output, to one decimal, on "
                "projects where both dates are known. An imprecise date resolves "
                "by convention first: 2024 becomes 1 July 2024. Measured from the "
                "announcement, not from the start of construction."),
    },
    "slip": {
        "label": "Slip",
        "why": "promised → first output",
        "tip": ("Years between the promised first output and the actual one, on "
                "projects that promised a date and have produced. Negative means "
                "early. Called schedule overrun or slippage in the literature."),
    },
}

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
    """The three gates, the findings behind them, and the flag split.

    Every value carries the projects behind it, because the panel's job is to
    say which ones to go and fix.
    """
    rows = conn.execute("SELECT * FROM screen_extracted").fetchall()
    total = len(rows)

    published = {r["project"] for r in conn.execute(
        "SELECT project FROM verify_verified")}

    latest = {}
    for r in conn.execute("SELECT screen_extracted_id, result_status, id "
                          "FROM screen_check ORDER BY id"):
        latest[r["screen_extracted_id"]] = r["result_status"]

    flags = {"provenance": [], "substantive": [], "none": []}
    hits = {k: [] for k in ("sourced", "clean", "publishable")}

    for r in rows:
        rid = r["id"]
        # promised_date_source is optional, but a row that supplies one is
        # making a citation and it has to be a real link. #18 supplied the four
        # characters "None", which is exactly the case this must not wave past.
        pds = (r["promised_date_source"] or "").strip()
        if (_has_url(r["promise_source"]) and _has_url(r["status_source"])
                and (not pds or _has_url(pds))):
            hits["sourced"].append(rid)
        if latest.get(rid) != "FAIL":
            hits["clean"].append(rid)
        if r["project"] in published:
            hits["publishable"].append(rid)
        kind = classify_flag(r["flag"])
        flags[kind or "none"].append(rid)

    # In pipeline order, and each set sits inside the one above: a project is
    # not verified before it is checked, and not checked usefully before it
    # cites two real pages. Read down, the three say where the work is.
    gates = []
    for key, label, why in (
        ("sourced", "Sourced", "both source links are real URLs"),
        ("clean", "Structurally clean", "the deterministic check does not FAIL"),
        ("publishable", "Publishable", "a human has verified it"),
    ):
        ids = hits[key]
        gates.append({
            "key": key, "label": label, "why": why,
            "n": len(ids), "total": total,
            "pct": (100.0 * len(ids) / total) if total else 0.0,
            "missing": [r["id"] for r in rows if r["id"] not in set(ids)],
        })

    return {"total": total, "gates": gates, "findings": findings(conn), "flags": flags}


def findings(conn: sqlite3.Connection) -> dict:
    """What the published projects say about the world.

    Counted on Verify and not on Screen, because the published record is the
    claim a reader gets: a project nobody has verified is not a finding yet.
    The two counts sit under the gates that produced them, so the 135 that
    cleared every gate are exactly the projects counted here.

    Both counts need a project to have produced, so the rest are returned
    beside them -- still building, cancelled, and producing with no confirmed
    date. The last is the only gap here a person can close, which is why it
    carries its projects.
    """
    rows = conn.execute("SELECT * FROM verify_verified").fetchall()
    produced, undated, cancelled, pending = [], [], [], []
    lag, slip, no_promise = [], [], []

    for r in rows:
        rid = r["id"]
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
        if r["actual_first_output_dt"]:
            produced.append(rid)
            if r["announced_dt"]:
                lag.append(rid)
            if r["promised_first_output_dt"]:
                slip.append(rid)
            else:
                no_promise.append(rid)
            continue
        _iso, kind = interpret_date(r["actual_first_output"])
        (undated if kind == "produced_undated"
         else cancelled if kind == "cancelled" else pending).append(rid)

    out = {"published": len(rows), "produced": produced, "pending": pending,
           "cancelled": cancelled, "undated": undated, "no_promise": no_promise}
    for key, ids in (("lag", lag), ("slip", slip)):
        out[key] = dict(FINDING_LABELS[key], key=key, n=len(ids), ids=ids)
    return out


def render_bar(pct: float, width: int = 22) -> str:
    """A bar in eighths, so a small share is visible instead of rounding to
    nothing -- 4% of 22 characters is otherwise an empty line."""
    eighths = round(pct / 100.0 * width * 8)
    full, rest = divmod(eighths, 8)
    return ("█" * full + (" ▏▎▍▌▋▊▉"[rest] if rest else "")).ljust(width, "·")
