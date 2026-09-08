"""
shared.py -- the pieces every page needs: the stylesheet, the page skeleton, the
database picker, and the small formatting helpers.

No routes and no app live here, so every page module can import it without a
circular reference. The dependency runs one way: shared <- the page modules <-
main.
"""

from __future__ import annotations

import html
import json
import os
import sys

# webapp/ -> scoreboard/, so `pipeline` imports resolve
# whether this is run as a package or by path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.responses import HTMLResponse  # noqa: E402

from pipeline import source, screen, verify, orchestrate as orch, llm  # noqa: E402
from pipeline.db import (  # noqa: E402
    connect, db_path, discover_databases, init_db, is_read_only, set_active_db,
    table_counts,
)
from pipeline.dates import lag_label  # noqa: E402
from pipeline.schema_check import (  # noqa: E402
    V0_COLUMNS,
    DERIVED_DATE_COLUMNS,
    RAW_DATE_COLUMNS,
    all_sectors,
    register_sector,
)
from pipeline.llm import LLMUnavailable  # noqa: E402


def _conn():
    return connect()


# --------------------------------------------------------------------------- #
# HTML helpers                                                                 #
# --------------------------------------------------------------------------- #

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font: 15px/1.5 system-ui, sans-serif; margin: 0; padding: 0 1.5rem 4rem;
       max-width: 1000px; margin-inline: auto; }
h1 { font-size: 1.4rem; } h2 { font-size: 1.1rem; margin-top: 2rem;
     border-bottom: 1px solid #8884; padding-bottom: .3rem; }
nav a { margin-right: 1rem; text-decoration: none; font-weight: 600; }
.stages { display: grid; grid-template-columns: repeat(5, 1fr); gap: .5rem;
         text-align: center; margin: 1rem 0; }
.stages div { border: 1px solid #8886; border-radius: 8px; padding: .6rem; }
.stages .n { font-size: 1.6rem; font-weight: 700; }
/* The tiles are links now, so each one is a door rather than a readout. They
   must not look like body links: default anchor styling underlined every label
   and repainted the counts visited-purple, which reads as decoration on the
   numbers rather than as a control. Inherit the text colour, drop the
   underline, and let the border do the affordance on hover. */
.stages a { color: inherit; text-decoration: none; display: block; }
.stages a div { transition: border-color .12s ease, background .12s ease; }
.stages a:hover div { border-color: #888c; background: #8881; }
.card { border: 1px solid #8885; border-radius: 8px; padding: .8rem 1rem;
        margin: .6rem 0; }
.card small { color: #8889; }
/* Quality panel: label, bar, count. Grid rather than a table so the bars line
   up at one width and the numbers stay right-aligned against them. */
.qrow { display: grid; grid-template-columns: 15rem 1fr 5rem; gap: .75rem;
        align-items: center; margin: .5rem 0; }
.qtrack { background: #8882; border-radius: 4px; height: 1.1rem; overflow: hidden; }
.qfill { height: 100%; border-radius: 4px; }
.qnum { text-align: right; font-variant-numeric: tabular-nums; }
@media (max-width: 640px) { .qrow { grid-template-columns: 1fr; } }
form.inline { display: inline; }
label { display: block; margin: .4rem 0 .1rem; font-size: .85rem; color: #8889; }
input[type=text], textarea, select { width: 100%; padding: .35rem .5rem;
    border: 1px solid #8887; border-radius: 6px; background: transparent;
    color: inherit; font: inherit; }
textarea { font-family: ui-monospace, monospace; font-size: .82rem; }
button { padding: .35rem .8rem; border: 1px solid #8887; border-radius: 6px;
    background: #6663; color: inherit; cursor: pointer; font: inherit; }
button.primary { background: #2b6cb0; color: #fff; border-color: #2b6cb0; }
.msg { background: #2b6cb022; border: 1px solid #2b6cb066; padding: .6rem 1rem;
       border-radius: 6px; margin: 1rem 0; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: .3rem .8rem; }
.verdict-FAIL { color: #c0392b; font-weight: 700; }
.verdict-PASS { color: #b7791f; font-weight: 700; }
.verdict-CLEAN { color: #2f855a; font-weight: 700; }
table { border-collapse: collapse; width: 100%; font-size: .85rem; }
td, th { border: 1px solid #8884; padding: .25rem .4rem; text-align: left; }
code { background: #8882; padding: 0 .2rem; border-radius: 3px; }
.pill { display: inline-block; font-size: .72rem; padding: .05rem .45rem;
        border-radius: 999px; border: 1px solid #8886; vertical-align: middle; }
.pill.todo { background: #b7791f22; border-color: #b7791f66; }
.pill.done { background: #2f855a22; border-color: #2f855a66; }
.toggle { display: flex; flex-wrap: wrap; gap: .3rem; margin: .6rem 0; }
.toggle a { text-decoration: none; color: inherit; font-size: .85rem;
    padding: .25rem .7rem; border: 1px solid #8887; border-radius: 6px; }
.toggle a.on { background: #2b6cb0; color: #fff; border-color: #2b6cb0; }
.dbbar { display: flex; align-items: center; gap: .5rem; flex-wrap: wrap;
    margin: .75rem 0 1rem; padding: .5rem .7rem; border: 1px solid #8886;
    border-radius: 8px; font-size: .85rem; }
.dbbar label { font-weight: 600; }
.dbbar select { font-size: .85rem; padding: .2rem; max-width: 26rem; }
.dbbar small { color: #8889; font-family: ui-monospace, monospace; }
.dbbar .ro { background: #b45309; color: #fff; border-radius: 4px;
    padding: .1rem .45rem; font-weight: 600; }
.dbbar .rw { background: #15803d; color: #fff; border-radius: 4px;
    padding: .1rem .45rem; font-weight: 600; }

/* ---- the review screen ------------------------------------------------- */
/* Two columns: the cited page on the left, the row's cells on the right. The
   left column is sticky, because the whole point is to keep the document in
   view while you work down the fields -- scrolling the article out of sight to
   reach the cell it proves is the exact problem this layout exists to remove.
   Below 1100px they stack, document first. */
.review { display: grid; grid-template-columns: 1.15fr 1fr; gap: 1rem;
    align-items: start; margin-top: .6rem; }
@media (max-width: 1100px) { .review { grid-template-columns: 1fr; } }
.review .doccol { position: sticky; top: .5rem; }
.tabs { display: flex; flex-wrap: wrap; gap: .3rem; margin-bottom: .35rem; }
.tabs a { text-decoration: none; color: inherit; font-size: .8rem;
    padding: .25rem .6rem; border: 1px solid #8887; border-radius: 6px 6px 0 0;
    border-bottom-color: transparent; }
.tabs a.on { background: #2b6cb0; color: #fff; border-color: #2b6cb0; }
.tabs a small { opacity: .7; }
.pane { width: 100%; border: 1px solid #8887; border-radius: 0 8px 8px 8px;
    background: Canvas; display: block; }
.pane.tall { height: 74vh; min-height: 440px; }
.pane.short { height: 26rem; }
.panehint { font-size: .78rem; color: #8889; margin: .3rem 0 0; }

/* The deterministic-check panel: rule, this row's value, verdict. A table
   rather than a list because the middle column is the point -- "announced is a
   real YYYY-MM anchor" says nothing until it is sitting next to 2022-01. */
table.rules td { vertical-align: top; font-size: .82rem; }
table.rules td.val { font-family: ui-monospace, monospace; word-break: break-all; }
table.rules td.ok { color: #2f855a; font-weight: 600; white-space: nowrap; }
table.rules td.err { color: #c0392b; font-weight: 600; white-space: nowrap; }
table.rules td.warn { color: #b7791f; font-weight: 600; white-space: nowrap; }
details.explain { margin: .5rem 0 0; }
details.explain summary { cursor: pointer; font-size: .85rem; color: #8889; }
details.explain[open] summary { margin-bottom: .5rem; }

/* The agentic-check picker: cells across, one line, before the pane. */
.cells { display: flex; flex-wrap: wrap; gap: .1rem .9rem; margin: .4rem 0 .6rem; }
.cells label { display: inline-flex; align-items: baseline; gap: .3rem;
    margin: 0; font-size: .85rem; color: inherit; }
.cells input { width: auto; }
.cells code { font-size: .95em; }
"""


VOCABULARY = {
    "renamed": "Source / Screen / Verify",
    "legacy":  "Bronze / Silver / Gold (pre-rename)",
    "empty":   "no tables yet",
    "missing": "unreadable",
}


def _db_bar() -> str:
    """The database picker: every scoreboard*.db under outputs/, one click to switch."""
    current = db_path()
    options = []
    for d in discover_databases():
        label = f"{d['rel']}  —  {VOCABULARY.get(d['flavour'], d['flavour'])}, {d['rows']} rows"
        sel = " selected" if d["active"] else ""
        options.append(f'<option value="{html.escape(str(d["path"]))}"{sel}>{html.escape(label)}</option>')
    badge = ('<span class="ro">read-only</span>' if is_read_only()
             else '<span class="rw">writable</span>')
    return f"""<form class="dbbar" method="post" action="/db">
<label for="dbsel">Database</label>
<select id="dbsel" name="path">{''.join(options)}</select>
<button type="submit">Switch</button>{badge}
<small>{html.escape(str(current))}</small></form>"""


def _page(title: str, body: str, msg: str | None = None,
          wide: bool = False) -> HTMLResponse:
    """The page skeleton. `wide` lifts the 1000px reading measure for the review
    screen, which puts a cited article beside the row it is evidence for and
    needs the room; every other page keeps the narrow column."""
    banner = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""
    widen = "<style>body { max-width: 1560px; }</style>" if wide else ""
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title><style>{_CSS}</style>{widen}</head><body>
<h1>Promised vs. Produced — Source → Verify Pipeline</h1>
<nav><a href="/">Dashboard</a><a href="/source">Source</a>
<a href="/screen">Screen</a><a href="/verify">Verify</a></nav>
{_db_bar()}
{banner}{body}</body></html>"""
    return HTMLResponse(doc)


def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _cell(row, col):
    """Value of a column that may not exist on this row -- e.g. a database that
    predates the *_raw / *_dt date columns and hasn't been re-init'd yet. Returns
    None (rendered as blank) instead of raising IndexError on a sqlite3.Row."""
    try:
        return row[col]
    except (IndexError, KeyError):
        return None


def _verdict_span(v: str | None) -> str:
    v = v or "—"
    cls = f"verdict-{v}" if v in ("FAIL", "PASS", "CLEAN") else ""
    return f'<span class="{cls}">{esc(v)}</span>'


# --------------------------------------------------------------------------- #
# "What have I not moved down yet?" -- a purely presentational filter.         #
# Both stages already carry their lineage FK (screen_extracted.source_collected_id,
# verify_verified.screen_extracted_id), so backlog is a read of existing columns:  #
# nothing here writes, and no schema/pipeline behaviour changes.               #
# --------------------------------------------------------------------------- #

SHOW_MODES = ("all", "pending", "done")

# The chosen toggle is remembered per stage in a cookie, so it survives every
# redirect the app does (add a lead, run a check, promote a row...) instead of
# snapping back to "all" and making you re-click. Purely presentational: the
# cookie only ever selects which rows are RENDERED.
SHOW_COOKIE = {"/source": "pvp_show_source", "/screen": "pvp_show_screen"}
SHOW_COOKIE_MAX_AGE = 60 * 60 * 24 * 365   # a year; it is a UI preference


def _resolve_show(request: Request, stage: str, show: str | None) -> str:
    """The toggle to render with.

    An explicit `?show=` in the URL wins (that's the user clicking the toggle);
    otherwise fall back to the remembered choice, then to "all"."""
    if show in SHOW_MODES:
        return show
    remembered = request.cookies.get(SHOW_COOKIE[stage])
    if remembered in SHOW_MODES:
        return remembered
    # "pending" rather than "all" on a first visit: these pages exist to work a
    # queue, and a queue that opens showing the finished items alongside the
    # unfinished ones makes the reader do the filtering the page could do.
    return "pending"


def _remember_show(resp: HTMLResponse, stage: str, show: str) -> HTMLResponse:
    resp.set_cookie(SHOW_COOKIE[stage], show,
                    max_age=SHOW_COOKIE_MAX_AGE, samesite="lax")
    return resp


def _downstream_map(conn, table: str, fk: str) -> dict[int, list[int]]:
    """{parent row id: [ids of the rows it produced in `table`]}.

    A parent missing from this map has never been carried to the next stage.
    Rows whose FK is NULL (seeded / hand-pasted, no lineage recorded) simply
    contribute nothing -- they can't prove any parent was promoted.
    """
    out: dict[int, list[int]] = {}
    for child_id, parent_id in conn.execute(
        f"SELECT id, {fk} FROM {table} WHERE {fk} IS NOT NULL ORDER BY id"
    ):
        out.setdefault(int(parent_id), []).append(int(child_id))
    return out


def _keep(row_id: int, downstream: dict[int, list[int]], show: str) -> bool:
    if show == "pending":
        return row_id not in downstream
    if show == "done":
        return row_id in downstream
    return True


def _stage_toggle(path: str, show: str, labels: dict[str, str]) -> str:
    return '<div class="toggle">' + "".join(
        f'<a class="{"on" if show == mode else ""}" '
        f'href="{path}?show={mode}">{lbl}</a>'
        for mode, lbl in labels.items()
    ) + "</div>"


def _lineage_pill(row_id: int, downstream: dict[int, list[int]],
                  next_stage: str, todo_label: str) -> str:
    kids = downstream.get(row_id)
    if kids:
        ids = ", ".join(f"#{k}" for k in kids)
        return f'<span class="pill done">→ {esc(next_stage)} {esc(ids)}</span>'
    return f'<span class="pill todo">{esc(todo_label)}</span>'


def _to_int(v) -> int | None:
    try:
        return int(str(v).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# "The flag IS the reason"                                                     #
# --------------------------------------------------------------------------- #

def flag_only_reason(changes: dict, old_flag) -> str | None:
    """The provenance note a flag-only edit writes for itself, or None.

    Every write to a Verify row needs a reason in `verify_edits`; that rule is
    what makes the published Scoreboard auditable and it is not moving. But when
    the ONLY cell that changed is `flag`, the required reason had become a tax
    on the exact case that least needs one. The flag is free text whose entire
    job is to say what is unresolved about the row — so typing "resolved the
    flag" into a second box beside it records nothing the first box does not
    already hold, and it was being typed on every pass through the queue.

    So a flag-only edit is its own reason: this returns the note, quoting both
    the old text and the new so the history reads as a change and not just as a
    new assertion.

    The narrowness matters. The moment any other cell moves — with or without
    the flag moving too — this returns None and the reviewer must say why. A
    changed date is a changed fact about the world, and nothing in the `flag`
    cell can be relied on to explain it.
    """
    if set(changes) != {"flag"}:
        return None
    was = ("" if old_flag is None else str(old_flag)).strip()
    now = (changes["flag"] or "").strip()
    if not now:
        return f"Flag cleared (was: {was})" if was else "Flag cleared"
    if not was:
        return f"Flag set: {now}"
    return f"Flag changed from “{was}” to “{now}”"

