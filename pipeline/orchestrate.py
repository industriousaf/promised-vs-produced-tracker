"""
orchestrate.py -- the moves between the stages.

This is the "move between the stages" logic the prompt asked for, shared by both
interfaces (CLI and web). Every step here works whether it is being automated or
a human is doing it:

  * Source and Screen pt-1 can be done manually (source.insert_lead /
    screen.insert_extracted) OR computationally (run_source_ai / run_screen_ai).
  * Screen pt-2 (the check) is always deterministic/computational.
  * Verify promotion is the human gate (verify.promote), and nothing here
    an explicit, clearly-labelled auto-promote for the "all the way to verify"
    convenience option, which bypasses that gate on purpose.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date

from pipeline import source, screen, verify, llm


def published_project_names(conn: sqlite3.Connection) -> list[str]:
    """Names of the projects already published, from the verify table.

    These are the finished, human-checked rows -- the Scoreboard's actual
    product -- so this is the authoritative "we already have this one, do not
    collect it again" list. It deliberately reads only the verify table:
    projects that are merely collected and not yet published are the other
    function's job (`unpublished_project_names`). Passed to
    `llm.render_source_prompt` as the live exclusion list, on every path that
    renders that prompt."""
    names = set()
    for r in conn.execute("SELECT project FROM verify_verified").fetchall():
        if r["project"]:
            names.add(r["project"])
    return sorted(names)


_TITLE_DASH = re.compile(r"\s+(?:\u2014|\u2013|--)\s+")
_SENTENCE_END = re.compile(r"(?<=[a-z0-9)])\.\s+(?=[A-Z])")


def _shorten_summary(summary: str, floor: int = 80, cap: int = 140) -> str:
    """Shorten a Source summary to just the part that identifies the project.

    Source rows carry no project name, so the summary is their only identifier
    -- but it is written for a human reader and averages ~310 characters. The
    exclusion list is re-read in full on every round of collection, so each
    entry is paid for once per round: at N=300 the untrimmed summaries cost
    ~2.5M tokens more than their shortened forms do.

    Collectors write summaries as `Title -- prose. Operator: X.`, and that title
    ("Eli Lilly Houston API Plant") makes a better entry than the prose. It is
    ~130 characters, and it usually names the company *and* the site -- which is
    what the prompt's exclude-sites-not-companies rule needs in order to tell
    one Eli Lilly plant from another. A title shorter than `floor` is not
    trusted to stand alone ("Nucor plate mill" leaves the town on the far side
    of the dash), so those read on to the floor instead; measured over the
    archived corpus that lifts entries keeping a place name from 46% to 73%,
    at 42% of the full summary length rather than 30%. Summaries written in
    some other shape fall back to the first sentence, then to a word-boundary
    cut at `cap`.
    """
    summary = (summary or "").strip()
    if not summary:
        return ""
    for pattern in (_TITLE_DASH, _SENTENCE_END):
        m = pattern.search(summary)
        # Ignore a match so early it cannot be a title (a stray "--"), or so
        # late that keeping only the head would save nothing.
        if m and 8 <= m.start() <= cap:
            end = m.start() + (1 if pattern is _SENTENCE_END else 0)
            if end >= floor:
                return summary[:end].strip()
            # Title too terse to stand alone ("Nucor plate mill" when the town
            # is on the far side of the dash). Keep reading to the floor.
            break
    if len(summary) <= cap:
        return summary
    cut = summary.rfind(" ", 0, cap)
    return summary[:cut if cut > floor else cap].rstrip() + "\u2026"


def unpublished_project_names(conn: sqlite3.Connection) -> list[str]:
    """Names of projects already collected but not yet published.

    Publishing is a human decision, so during a collection run the verify table
    stays empty however many projects have been found. If the collector were
    shown only the published list, it would find the same obvious project over
    and over. This list is what actually prevents that.

    A project appears here under its Screen `project` name once it has been
    extracted, and before that under a shortened form of its Source `summary`,
    because Source rows carry no name of their own (see `_shorten_summary`).
    Because a collected lead is saved immediately, one found a minute ago is
    already in this list for the next round. Passed to
    `llm.render_source_prompt` beside the published list, as its own section."""
    names = set()
    for r in conn.execute("SELECT project FROM screen_extracted").fetchall():
        project = (r["project"] or "").strip()
        if project:
            names.add(project)
    # Summaries only for leads that have not been screened yet: once a lead
    # reaches Screen it has a `project` name for the same site, and listing
    # both names the project twice.
    for r in conn.execute(
        "SELECT summary FROM source_collected s WHERE NOT EXISTS "
        "(SELECT 1 FROM screen_extracted e WHERE e.source_collected_id = s.id)"
    ).fetchall():
        summary = _shorten_summary(r["summary"] or "")
        if summary:
            names.add(summary)
    return sorted(names)


def announced_year_coverage(conn: sqlite3.Connection,
                           first_year: int = 2017) -> dict[int, int]:
    """{announcement year: rows collected}, across the eligible window.

    Every Source lead that has reached Screen has a parsed `announced_dt`, so
    this counts what the corpus actually covers rather than what it meant to.

    It exists because the first N=20 batch came back 70% announced in 2021-2022
    and empty in three years of the window. Nobody chose that: the prompt says
    to vary the search axis, but the most heavily reported projects cluster in
    the CHIPS/IRA period, so an open-ended search returns them and the exclusion
    list only removes the exact sites already taken -- never the vintage.

    Note what this does and does not claim. Some of that concentration is real;
    there genuinely were more announcements in 2021-2022. The problem is that
    the corpus cannot tell you which part is the world and which is the search,
    and a reader will ask. Showing the collector its own coverage makes the
    year distribution a decision rather than a residue.
    """
    counts = {y: 0 for y in range(first_year, date.today().year + 1)}
    for row in conn.execute(
        "SELECT announced_dt FROM screen_extracted WHERE announced_dt IS NOT NULL"
    ):
        try:
            year = int(str(row["announced_dt"])[:4])
        except (TypeError, ValueError):
            continue
        if year in counts:
            counts[year] += 1
    return counts


def filter_by_thresholds(
    conn: sqlite3.Connection,
    capital_min: int = 0,
    jobs_min: int = 0,
    op: str = "AND",
    stage: str = "verify",
) -> list[sqlite3.Row]:
    """Explore-filter: rows clearing a capital and/or jobs threshold.

    The inclusion floor in the checker is fixed ($100M OR 200 jobs). This lets
    you *explore* alternative thresholds and combinators over what's already
    collected -- e.g. "$1B AND 2,000 jobs", or "$500M AND 400 jobs" -- WITHOUT
    changing that gate. It is a plain parameterised SQL query:

        SELECT * FROM <table>
        WHERE promised_capital_usd >= ?  <AND|OR>  promised_jobs >= ?

    `op` is 'AND' or 'OR' (anything else is treated as 'OR'); `stage` is 'verify'
    or 'screen'. Both the table and the combiner come from fixed whitelists, so
    the string interpolation is injection-safe; the two thresholds are bound
    parameters.
    """
    table = {"verify": "verify_verified", "screen": "screen_extracted"}.get(stage)
    if table is None:
        raise ValueError(f"stage must be 'verify' or 'screen', got {stage!r}")
    combiner = "AND" if str(op).strip().upper() == "AND" else "OR"
    sql = (
        f"SELECT * FROM {table} "
        f"WHERE promised_capital_usd >= ? {combiner} promised_jobs >= ? "
        "ORDER BY promised_capital_usd DESC, promised_jobs DESC"
    )
    return conn.execute(sql, (int(capital_min or 0), int(jobs_min or 0))).fetchall()


def run_source_ai(conn: sqlite3.Connection) -> tuple[int, dict]:
    """AI Source: collect one new lead from the web and store it.

    Returns (source_id, lead_dict). Raises llm.LLMUnavailable on failure.
    """
    lead = llm.collect_source_lead(
        avoid_published=published_project_names(conn),
        avoid_unpublished=unpublished_project_names(conn),
        year_coverage=announced_year_coverage(conn),
    )
    bid = source.insert_lead(
        conn,
        promise_source=lead["promise_source"],
        status_source=lead["status_source"],
        promised_date_source=lead.get("promised_date_source"),
        summary=lead.get("summary"),
        collected_via="api",
    )
    return bid, lead


def run_screen_ai(conn: sqlite3.Connection, source_id: int) -> tuple[int, dict]:
    """AI Screen pt-1: extract a 18-column row from a stored Source lead.

    Returns (screen_id, row_dict). Raises llm.LLMUnavailable on failure.
    """
    lead_row = source.get_lead(conn, source_id)
    if lead_row is None:
        raise ValueError(f"no source_collected lead with id {source_id}")
    lead = {
        "promise_source": lead_row["promise_source"],
        "status_source": lead_row["status_source"],
        "promised_date_source": lead_row["promised_date_source"],
        "summary": lead_row["summary"],
    }
    row = llm.extract_screen_row(lead)
    sid = screen.insert_extracted(conn, row, source_collected_id=source_id)
    return sid, row
