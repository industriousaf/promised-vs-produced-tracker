"""
screen.py -- the Screen stage pages of the web interface.

Routes are collected on an APIRouter here and mounted onto the app in main.py,
so this module never creates a server of its own.
"""

from __future__ import annotations

from typing import Optional

import html
import json
import os
import sys

# webapp/ -> scoreboard/, so `pipeline` imports resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import APIRouter, Request  # noqa: E402
from fastapi.responses import HTMLResponse, RedirectResponse  # noqa: E402

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

from webapp.shared import (  # noqa: E402
    _cell, _conn, _db_bar, _downstream_map, _keep, _lineage_pill, _page,
    _remember_show, _resolve_show, _stage_toggle, _to_int, _verdict_span, esc,
)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Screen                                                                       #
# --------------------------------------------------------------------------- #

@router.get("/screen", response_class=HTMLResponse)
def screen_page(request: Request, msg: Optional[str] = None, show: Optional[str] = None):
    show = _resolve_show(request, "/screen", show)
    conn = _conn()
    try:
        all_rows = screen.list_extracted(conn)
        promoted = _downstream_map(conn, "verify_verified", "screen_extracted_id")
        rows = [r for r in all_rows if _keep(r["id"], promoted, show)]
        checks = {r["id"]: screen.latest_check(conn, r["id"]) for r in rows}
        queue = screen.review_queue(conn)
    finally:
        conn.close()

    n_done = sum(1 for r in all_rows if r["id"] in promoted)
    n_pending = len(all_rows) - n_done
    toggle = _stage_toggle("/screen", show, {
        "all": f"All ({len(all_rows)})",
        "pending": f"Not yet in Verify ({n_pending})",
        "done": f"Already in Verify ({n_done})",
    })

    def _row_html(r):
        chk = checks[r["id"]]
        verdict = chk["result_status"] if chk else None
        # Promotion now lives INSIDE the per-row inspect page (so you can review
        # every extracted field first) -- the list just links there.
        return f"""<div class="card"><b>#{r['id']}</b> {esc(r['project'])}
          <small>({esc(r['sector'])}, {esc(r['state'])})</small>
          {_lineage_pill(r['id'], promoted, "Verify", "not promoted yet")}
          — check: {_verdict_span(verdict)}
          <form class="inline" method="post" action="/screen/check">
            <input type="hidden" name="screen_id" value="{r['id']}">
            <button type="submit">Run check</button></form>
          <a href="/screen/{r['id']}/inspect"><button type="button" class="primary">Inspect &amp; promote →</button></a>
          <br><small>{esc(r['current_status'])}</small>
          {"<br><small>flag: " + esc(r['flag']) + "</small>" if r['flag'] else ""}
        </div>"""

    items = "".join(_row_html(r) for r in rows) or "<p>(no Screen rows match this filter)</p>"
    # lag_years / slip_years and the *_dt columns are derived on insert, so the
    # paste-in example omits them (supplying them is harmless -- they're overwritten).
    # It DOES include each date's *_raw verbatim partner, which the extractor supplies.
    example_keys = [c for c in V0_COLUMNS if c not in ("lag_years", "slip_years")]
    example_keys += list(RAW_DATE_COLUMNS)
    example = json.dumps({c: "" for c in example_keys}, indent=1)

    # The review queue leads. It used to sit below "Extract a row" and "Add a row
    # — paste JSON", so the page opened on the two things a person almost never
    # does and buried the one thing that is actually waiting on them. The
    # add-forms are still here, at the bottom, where a rarely-used tool belongs.
    # Ask the same helper the dashboard and `status` ask, so the three cannot
    # quote different numbers at the same person. n_pending counts rows not yet
    # in Verify; some of those are blocked by a failing check and cannot be
    # promoted at all, and saying so is the difference between a queue of 22 and
    # an unexplained 23.
    q = queue
    n_ready, n_blocked = len(q["ready"]), len(q["blocked"])
    if n_ready or n_blocked:
        blocked_note = ""
        if n_blocked:
            links = ", ".join(f'<a href="/screen/{r["id"]}/inspect">#{r["id"]}</a>'
                              for r in q["blocked"][:8])
            blocked_note = (f" A further <b>{n_blocked}</b> cannot be promoted until a "
                            f"failing check is fixed: {links}.")
        lede = (f"<p><b>{n_ready} row(s) are waiting for you.</b> Open one, check "
                "every field against the two sources, then promote it. Nothing "
                "reaches the published Scoreboard until a person does this."
                + blocked_note + "</p>")
    else:
        lede = ("<p>Nothing is waiting: every Screen row has been through the human "
                "gate. <a href=\"/verify\">See the published rows</a>.</p>")

    body = f"""
<h2>Review queue</h2>
<div class="card">{lede}
<p><small>Or work the same queue in a terminal, largest capital first:
<code>python3 scoreboard.py review</code></small></p></div>

<h2>Rows ({len(rows)} of {len(all_rows)})</h2>
<p><form class="inline" method="post" action="/screen/check-all">
  <button type="submit">Run the deterministic check on all rows</button></form></p>
{toggle}
{items}

<hr>
<h2>Add rows by hand</h2>
<p><small>Rarely needed — the collection loops fill this stage. Use these to
extract a specific lead, or to paste a row you built yourself.</small></p>

<div class="card"><form method="get" action="/screen/prompt">
  <label>Extract a Source lead with Claude Code — lead id</label>
  <input type="text" name="source_id" required>
  <p><button type="submit">Show Screen prompt to run</button></p>
</form></div>

<div class="card"><form method="post" action="/screen/add">
  <label>source_id (optional lineage)</label><input type="text" name="source_id">
  <label>row JSON (v0 columns; verification_tier forced to P; lag/slip + *_dt derived)</label>
  <textarea name="row_json" rows="8">{esc(example)}</textarea>
  <p><button type="submit">Add row</button></p>
</form></div>
"""
    return _remember_show(_page("Screen", body, msg), "/screen", show)


@router.get("/screen/prompt", response_class=HTMLResponse)
def screen_prompt_page(source_id: int):
    conn = _conn()
    try:
        lead_row = source.get_lead(conn, source_id)
    finally:
        conn.close()
    if lead_row is None:
        return _page("Screen prompt", "<p>No such Source lead.</p>", "Not found")
    lead = {
        "promise_source": lead_row["promise_source"],
        "status_source": lead_row["status_source"],
        "promised_date_source": lead_row["promised_date_source"],
        "summary": lead_row["summary"],
        "source_collected_id": lead_row["id"],
    }
    prompt = llm.render_screen_prompt(lead)
    body = f"""
<h2>Screen prompt for Source #{source_id} — run this in Claude Code</h2>
<div class="card">
  <p>Copy everything below, run it in a web-search-capable assistant, then paste
  the JSON it returns.</p>
  <textarea rows="24" onclick="this.select()">{esc(prompt)}</textarea>
</div>
<div class="card"><form method="post" action="/screen/add">
  <input type="hidden" name="source_id" value="{source_id}">
  <label>Paste the row JSON returned by Claude Code</label>
  <textarea name="row_json" rows="8"></textarea>
  <p><button class="primary" type="submit">Ingest row JSON</button></p>
</form></div>
"""
    return _page("Screen prompt", body)


@router.post("/screen/add")
async def screen_add(request: Request):
    form = await request.form()
    conn = _conn()
    try:
        row = json.loads(form.get("row_json") or "{}")
        bid = form.get("source_id")
        bid = int(bid) if bid else None
        sid = screen.insert_extracted(conn, row, source_collected_id=bid)
        msg = f"Added Screen row #{sid} (tier forced to P)."
    except Exception as e:
        msg = f"Error: {e}"
    finally:
        conn.close()
    return RedirectResponse(f"/screen?msg={html.escape(msg)}", status_code=303)


@router.post("/screen/extract")
async def screen_extract(request: Request):
    form = await request.form()
    conn = _conn()
    try:
        bid = int(form.get("source_id"))
        sid, _ = orch.run_screen_ai(conn, bid)
        msg = f"Extracted Screen row #{sid} from Source #{bid}."
    except LLMUnavailable as e:
        msg = f"API extract failed: {e}"
    except Exception as e:
        msg = f"Error: {e}"
    finally:
        conn.close()
    return RedirectResponse(f"/screen?msg={html.escape(msg)}", status_code=303)


@router.post("/screen/check")
async def screen_check(request: Request):
    form = await request.form()
    conn = _conn()
    try:
        sid = int(form.get("screen_id"))
        res = screen.run_check(conn, sid)
        msg = (f"Checked Screen #{sid}: {res['result_status']} "
               f"({res['n_errors']} errors, {res['n_warnings']} warnings).")
    except Exception as e:
        msg = f"Error: {e}"
    finally:
        conn.close()
    return RedirectResponse(f"/screen?msg={html.escape(msg)}", status_code=303)


@router.post("/screen/check-all")
def screen_check_all():
    conn = _conn()
    try:
        n = 0
        for r in screen.list_extracted(conn):
            screen.run_check(conn, r["id"])
            n += 1
        msg = f"Ran the check on {n} Screen rows."
    finally:
        conn.close()
    return RedirectResponse(f"/screen?msg={html.escape(msg)}", status_code=303)


# --------------------------------------------------------------------------- #
# Screen inspect — review every field, edit, then promote (the human gate).    #
# Same field/edit scheme as the Verify detail form: derived cells are read-only, #
# sector is a dropdown, and any cell you change is recorded in verify_edits on    #
# promotion (applied to the new Verify row via the ordinary verify.edit path).     #
# --------------------------------------------------------------------------- #

# The cells a human may review/edit on the inspect form. verification_tier is
# excluded here because the Promote control below picks the Verify tier explicitly.
INSPECT_COLUMNS = [
    c for c in V0_COLUMNS
    if c not in verify.DERIVED_FIELDS and c != "verification_tier"
]

# What each cell means, shown beside its label.
#
# The two date cells are the ones that need it. A reviewer reading a status
# source that says "output now slated for 2028" reasonably wants to type 2028
# somewhere, and the only date-shaped box on the form is actual_first_output --
# which would record a plant as having produced when it has not. The revised
# date has no cell of its own (the schema carries one promise and one actual),
# so it belongs in current_status, and the form has to say so rather than leave
# the reviewer to work it out. It cost real time twice on the same day: once on
# a TSMC row, once on a Ford one.
FIELD_HINTS = {
    "announced": "when it was announced. YYYY-MM.",
    "promised_first_output": (
        "the <b>original</b> promise, as first announced — slip is measured "
        "against it. Do <b>not</b> update it when the date moves; that belongs "
        "in current_status. A year alone is fine (2025, 2025-Q4, "
        "“2025 (second half)”)."
    ),
    "actual_first_output": (
        "only when it has <b>actually produced</b>. Not yet producing → "
        "<code>pending</code>. A promise revised to a later date is still "
        "<code>pending</code> — put the new date in current_status. "
        "<code>never</code> if cancelled, <code>unconfirmed</code> if it has "
        "produced but no source gives a date."
    ),
    "current_status": (
        "where it really stands today, in the sources' own terms — and where a "
        "revised output date goes."
    ),
    "promised_capital_usd": "digits only, no $ or commas.",
    "promised_jobs": "digits only.",
    "flag": "anything unresolved. Leave empty if the extraction is clean.",
    "promised_date_source": (
        "only if the promised date came from a different document than "
        "promise_source. Leave empty otherwise."
    ),
    "actual_date_source": (
        "only if status_source proves the plant runs today but does not say "
        "when it started. Leave empty otherwise."
    ),
}


@router.get("/screen/{screen_id}/inspect", response_class=HTMLResponse)
def screen_inspect(screen_id: int, msg: Optional[str] = None):
    conn = _conn()
    try:
        r = screen.get_extracted(conn, screen_id)
        if r is None:
            return _page("Screen inspect", "<p>No such Screen row.</p>", "Not found")
        chk = screen.latest_check(conn, screen_id)
    finally:
        conn.close()

    verdict = chk["result_status"] if chk else None
    promotable = verdict in ("PASS", "CLEAN")

    def _field(c: str) -> str:
        hint = FIELD_HINTS.get(c, "")
        hint_html = f' <small>{hint}</small>' if hint else ""
        if c == "sector":
            # Manual sector entry is a dropdown of the live vocabulary (+ the
            # row's current value) -- identical to the Verify edit form.
            opts = set(all_sectors())
            cur = (r["sector"] or "").strip()
            if cur:
                opts.add(cur)
            options = "".join(
                f'<option{" selected" if o == cur else ""}>{esc(o)}</option>'
                for o in sorted(opts)
            )
            return (f"""<div><label>sector</label>
        <select name="sector">{options}</select></div>""")
        return (f"""<div><label>{esc(c)}{hint_html}</label>
        <input type="text" name="{esc(c)}" value="{esc(r[c])}"></div>""")

    fields = "".join(_field(c) for c in INSPECT_COLUMNS)
    derived = "".join(
        f"""<div><label>{esc(c)} <small>(derived)</small></label>
        <input type="text" value="{esc(lag_label(r[c]))}" disabled></div>"""
        for c in ("lag_years", "slip_years")
    )
    dt_display = "".join(
        f"""<div><label>{esc(c)} <small>(derived DATETIME)</small></label>
        <input type="text" value="{esc(_cell(r, c))}" disabled></div>"""
        for c in DERIVED_DATE_COLUMNS
    )
    raw_display = "".join(
        f"""<div><label>{esc(c)} <small>(verbatim source)</small></label>
        <input type="text" value="{esc(_cell(r, c))}" disabled></div>"""
        for c in RAW_DATE_COLUMNS
    )

    if promotable:
        # No tier picker, for the reason spelled out in cmd_review: this page
        # shows the promise and the status, which are two halves of the row and
        # not two readings of one claim, so V2 was never answerable from what
        # is on screen. The CLI queue stamps V1 too; a picker here would be the
        # same non-choice in the other interface. Deliberate V2 goes through
        # `verify-promote --tier V2` once a second source has actually been found.
        promote_controls = """
    <input type="hidden" name="tier" value="V1">
    <p class="msg">Publishes as <b>tier V1</b> — one source checked. For V2, find a
    second independent source, then run
    <code>verify-promote --screen-id N --tier V2</code>.</p>
    <label>Reason — required only if you changed a cell (recorded in <code>verify_edits</code>)</label>
    <input type="text" name="edit_description"
           placeholder="e.g. corrected announced date to match the filing">
    <p><button class="primary" type="submit">Promote to Verify</button></p>"""
    else:
        promote_controls = (
            '<p class="msg">Run the deterministic check and reach '
            "<b>PASS</b> or <b>CLEAN</b> before promoting to Verify.</p>"
        )

    check_note = (
        f"<small> — last run: {chk['n_errors']} error(s), "
        f"{chk['n_warnings']} warning(s)</small>"
        if chk else "<small> — not checked yet</small>"
    )

    body = f"""
<p><a href="/screen">← back to Screen</a></p>
<h2>Screen #{r['id']} — {esc(r['project'])}
  <small>({esc(r['sector'])}, {esc(r['state'])})</small></h2>
<p><small>from source_collected #{esc(r['source_collected_id'])} ·
  extracted {esc(r['datetime'])} · check: {_verdict_span(verdict)}</small></p>

<div class="card">
  <form class="inline" method="post" action="/screen/check">
    <input type="hidden" name="screen_id" value="{r['id']}">
    <button type="submit">Run check</button></form>
  {check_note}
</div>

<h2>Fields — review before promoting</h2>
<div class="card">
  <p>Inspect every extracted cell before it becomes research-grade. Any cell you
  change is applied to the new Verify row on promotion and logged in
  <code>verify_edits</code> — the same editing scheme as a published Verify row.
  <b>lag_years / slip_years and the <code>*_dt</code> columns are derived</b> from
  the date strings and recompute automatically when you edit a date.</p>
  <form method="post" action="/screen/{r['id']}/promote">
    <div class="grid2">{fields}</div>
    <p><small>Verbatim source text (read-only) — the exact page text each date came from:</small></p>
    <div class="grid2">{raw_display}</div>
    <p><small>Derived cells (read-only):</small></p>
    <div class="grid2">{derived}{dt_display}</div>
    {promote_controls}
  </form>
</div>
"""
    return _page(f"Screen #{screen_id}", body, msg)


@router.post("/screen/{screen_id}/promote")
async def screen_inspect_promote(screen_id: int, request: Request):
    form = await request.form()
    conn = _conn()
    try:
        src = screen.get_extracted(conn, screen_id)
        if src is None:
            raise ValueError(f"no screen_extracted row #{screen_id}")
        tier = form.get("tier") or "V1"
        desc = (form.get("edit_description") or "").strip()

        # Diff the inspect form against the stored Screen cells -- same comparison
        # the Verify edit form uses, so only genuinely-changed cells are recorded.
        changes = {}
        for c in INSPECT_COLUMNS:
            new = form.get(c)
            if new is None:
                continue
            old = "" if src[c] is None else str(src[c])
            if new != old:
                changes[c] = new

        # An edit needs its provenance reason before we touch Verify.
        if changes and not desc:
            msg = ("You changed " + ", ".join(sorted(changes))
                   + " — enter a reason (it goes to verify_edits) before promoting.")
            return RedirectResponse(
                f"/screen/{screen_id}/inspect?msg={html.escape(msg)}",
                status_code=303,
            )

        # Promote a faithful copy first (the human gate), then apply the human's
        # edits through the ordinary verify.edit path so each one lands in verify_edits.
        gid = verify.promote(conn, screen_id, verification_tier=tier)
        if changes:
            verify.edit(conn, gid, changes, edit_description=desc)
            msg = (f"Promoted Screen #{screen_id} to Verify #{gid} (tier {tier}); "
                   f"recorded {len(changes)} change(s) in verify_edits.")
        else:
            msg = f"Promoted Screen #{screen_id} to Verify #{gid} (tier {tier})."
        dest = f"/verify/{gid}"
    except verify.PromotionBlocked as e:
        msg = f"Promotion blocked: {e}"
        dest = f"/screen/{screen_id}/inspect"
    except Exception as e:
        msg = f"Error: {e}"
        dest = f"/screen/{screen_id}/inspect"
    finally:
        conn.close()
    return RedirectResponse(f"{dest}?msg={html.escape(msg)}", status_code=303)

