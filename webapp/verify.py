"""
verify.py -- the Verify stage pages of the web interface.

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
from pipeline.settings import active as _crit  # noqa: E402
from pipeline.dates import lag_label  # noqa: E402
from pipeline.schema_check import (  # noqa: E402
    V0_COLUMNS,
    DERIVED_DATE_COLUMNS,
    RAW_DATE_COLUMNS,
    all_sectors,
)
from pipeline.llm import LLMUnavailable  # noqa: E402

from webapp import agent as agent_pane, evidence  # noqa: E402
from webapp.shared import (  # noqa: E402
    _cell, _conn, _downstream_map, _keep, _lineage_pill, _page,
    _remember_show, _resolve_show, _stage_toggle, _to_int, _verdict_span, esc,
    flag_only_reason,
)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Verify — the human gate + the edit interface                                  #
# --------------------------------------------------------------------------- #

@router.post("/verify/promote")
async def verify_promote(request: Request):
    form = await request.form()
    conn = _conn()
    try:
        sid = int(form.get("screen_id"))
        tier = form.get("tier") or "V1"
        gid = verify.promote(conn, sid, verification_tier=tier)
        msg = f"Promoted Screen #{sid} to Verify #{gid} (tier {tier})."
        dest = f"/verify/{gid}"
    except verify.PromotionBlocked as e:
        msg = f"Promotion blocked: {e}"
        dest = "/screen"
    except Exception as e:
        msg = f"Error: {e}"
        dest = "/screen"
    finally:
        conn.close()
    return RedirectResponse(f"{dest}?msg={html.escape(msg)}", status_code=303)


@router.get("/verify", response_class=HTMLResponse)
def verify_page(
    msg: Optional[str] = None,
    fcap: Optional[str] = None,
    fjobs: Optional[str] = None,
    fop: str = "AND",
    fstage: str = "verify",
):
    filter_requested = fcap is not None or fjobs is not None
    combiner = "AND" if (fop or "AND").upper() == "AND" else "OR"
    stage = fstage if fstage in ("verify", "screen") else "verify"
    cap_min = _to_int(fcap) or 0
    jobs_min = _to_int(fjobs) or 0

    conn = _conn()
    try:
        rows = verify.list_verified(conn)
        edit_counts = {r["id"]: len(verify.list_edits(conn, r["id"])) for r in rows}
        filtered = None
        if filter_requested:
            filtered = orch.filter_by_thresholds(
                conn, capital_min=cap_min, jobs_min=jobs_min, op=combiner, stage=stage
            )
    finally:
        conn.close()

    items = "".join(
        f"""<div class="card"><b>#{r['id']}</b>
        <a href="/verify/{r['id']}">{esc(r['project'])}</a>
        <small>tier {esc(r['verification_tier'])} · {edit_counts[r['id']]} edit(s)</small>
        <br><small>flag: {esc(r['flag'])}</small></div>"""
        for r in rows
    ) or "<p>(no Verify rows yet — promote a passing Screen row)</p>"

    def _sel(cur: str, val: str) -> str:
        return " selected" if cur == val else ""

    filter_results = ""
    if filtered is not None:
        trows = "".join(
            f"<tr><td>{esc(r['project'])}</td>"
            f"<td>{('$' + format(r['promised_capital_usd'], ',')) if r['promised_capital_usd'] is not None else '—'}</td>"
            f"<td>{format(r['promised_jobs'], ',') if r['promised_jobs'] is not None else '—'}</td>"
            f"<td>{esc(r['verification_tier'])}</td></tr>"
            for r in filtered
        ) or "<tr><td colspan='4'><small>(no rows match)</small></td></tr>"
        filter_results = (
            f"<p><small>{len(filtered)} row(s): capital ≥ ${cap_min:,} "
            f"<b>{combiner}</b> jobs ≥ {jobs_min:,} · stage <code>{esc(stage)}</code></small></p>"
            f"<table><tr><th>project</th><th>capital</th><th>jobs</th><th>tier</th></tr>{trows}</table>"
        )

    filter_panel = f"""
<h3>Explore-filter: capital / jobs thresholds</h3>
<div class="card">
  <p>Probe thresholds other than the active inclusion floor
  ({_crit().describe()}, phase {_crit().id}) without changing the gate.
  Runs a plain SQL query
  <code>WHERE promised_capital_usd ≥ ? {{AND|OR}} promised_jobs ≥ ?</code>.
  Try <code>$5B OR 5000 jobs</code>, or <code>$5B AND 5000 jobs</code>.</p>
  <form method="get" action="/verify">
    <div class="grid2">
      <div><label>min capital (USD)</label><input type="text" name="fcap" value="{esc(fcap or '')}" placeholder="5000000000"></div>
      <div><label>min jobs</label><input type="text" name="fjobs" value="{esc(fjobs or '')}" placeholder="5000"></div>
      <div><label>combine</label>
        <select name="fop"><option{_sel(combiner, 'AND')}>AND</option><option{_sel(combiner, 'OR')}>OR</option></select></div>
      <div><label>stage</label>
        <select name="fstage"><option{_sel(stage, 'verify')}>verify</option><option{_sel(stage, 'screen')}>screen</option></select></div>
    </div>
    <p><button class="primary" type="submit">Run filter</button></p>
  </form>
  {filter_results}
</div>
"""
    sector_chips = " · ".join(esc(s) for s in sorted(all_sectors())) or "(none)"
    sector_panel = f"""
<h3>Sector vocabulary</h3>
<div class="card">
  <p>A <b>closed</b> set of manufacturing sectors. Manual edits pick from a
  dropdown on each Verify row; a sector outside this vocabulary is rejected by
  the checker. Extending it is a code change — add to <code>SECTORS</code> in
  <code>pipeline/settings.py</code> — so that what counts as in scope cannot
  move at runtime without a commit recording it. Current vocabulary:</p>
  <p>{sector_chips}</p>
</div>
"""
    # The Scoreboard leads. This page opened with a threshold-probing tool and
    # the sector vocabulary above the published rows, which put two reference
    # panels in front of the thing the methodology calls "the Scoreboard in its
    # final form". Both are still here, folded, because neither is what a
    # person came to this page to see.
    lede = ('<p class="pagelede">The published Scoreboard. Every row here was '
            'read against its two sources by a person; nothing reaches this '
            'table any other way.</p>')
    tools = (f'<details class="byhand"><summary>Explore and reference</summary>'
             f'{filter_panel}{sector_panel}</details>')
    body = f"{lede}<h2>Published rows ({len(rows)})</h2>{items}{tools}"
    return _page("Verify", body, msg)


@router.get("/verify/{verify_id}", response_class=HTMLResponse)
def verify_detail(verify_id: int, msg: Optional[str] = None):
    conn = _conn()
    try:
        r = verify.get_verified(conn, verify_id)
        if r is None:
            return _page("Verify", "<p>No such Verify row.</p>", "Not found")
        edits = verify.list_edits(conn, verify_id)
    finally:
        conn.close()

    # lag_years / slip_years are derived -- show them read-only (disabled inputs
    # are not submitted, so an edit never overwrites them by hand). Sentinels are
    # rendered as words ("to be completed" / "cancelled").
    def _field(c: str) -> str:
        if c in verify.DERIVED_FIELDS:
            return (f"""<div><label>{esc(c)} <small>(derived)</small></label>
        <input type="text" value="{esc(lag_label(r[c]))}" disabled></div>""")
        if c == "sector":
            # Manual sector entry is a dropdown of the live vocabulary (+ the
            # row's current value, in case it was registered elsewhere).
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
        return (f"""<div><label>{esc(c)}</label>
        <input type="text" name="{esc(c)}" value="{esc(r[c])}"></div>""")

    fields = "".join(_field(c) for c in V0_COLUMNS)
    dt_display = "".join(
        f"""<div><label>{esc(c)} <small>(derived DATETIME)</small></label>
        <input type="text" value="{esc(_cell(r, c))}" disabled></div>"""
        for c in DERIVED_DATE_COLUMNS
    )
    # Editable, not disabled. These were read-only next to the derived *_dt
    # cells, which put them in the wrong category: a *_dt is COMPUTED and must
    # never be hand-set, while a *_raw is the quote a human is most likely to
    # need to correct -- it is the evidence for the date sitting right above it.
    # Locked, a corrected date left its own source text contradicting it.
    raw_display = "".join(
        f"""<div><label>{esc(c)} <small>(verbatim source — edit if you change the date)</small></label>
        <input type="text" name="{esc(c)}" value="{esc(_cell(r, c))}"></div>"""
        for c in RAW_DATE_COLUMNS
    )
    history = "".join(
        f"<tr><td>{esc(e['datetime'])}</td><td>{esc(e['edit_description'])}</td></tr>"
        for e in edits
    ) or "<tr><td colspan='2'><small>no edits yet</small></td></tr>"

    # Same review layout as the Screen inspect page: a published row is edited
    # for exactly the reason an unpublished one is corrected -- a source says
    # something other than what the cell holds -- so the source belongs on screen
    # in both places rather than in another tab in one of them.
    body = f"""
<h2 class="rowtitle">Verify #{r['id']} \u00b7 {esc(r['project'])}</h2>
<p><small>created {esc(r['created_at'])} · last-modified {esc(r['datetime'])} ·
from screen_extracted #{esc(r['screen_extracted_id'])}</small></p>

<div class="review">
  <div class="doccol">
    {evidence.pane_html("verify", r["id"], r)}
  </div>
  <div class="formcol">
    <div class="card"><form method="post" action="/verify/{r['id']}/edit">
      <p>Edit any cell below. Only changed cells are written; every save is
      recorded in <code>verify_edits</code> with the reason you give.
      <b>lag_years / slip_years and the <code>*_dt</code> columns are derived</b>
      from the date strings — edit <code>announced</code> /
      <code>promised_first_output</code> / <code>actual_first_output</code> and
      they recompute automatically.</p>
      <div class="grid2">{fields}</div>
      <p><small>Verbatim source text — the exact page text each date came from.
      This is what the pane searches for first:</small></p>
      <div class="grid2">{raw_display}</div>
      <p><small>Derived DATETIME interpretations (read-only):</small></p>
      <div class="grid2">{dt_display}</div>
      <label>Reason for this edit (goes to <code>verify_edits</code>) — required,
      except when <code>flag</code> is the only cell you changed: that one writes
      its own reason.</label>
      <input type="text" name="edit_description">
      <p><button class="primary" type="submit">Save edit</button></p>
    </form></div>
  </div>
</div>

<h2>Ask a model to check it against the links</h2>
<div class="card">{agent_pane.picker_html("verify", r["id"], r)}</div>

<h2>Edit history</h2>
<table><tr><th>when</th><th>edit_description</th></tr>{history}</table>
"""
    return _page(f"Verify #{verify_id}", body, msg, wide=True)


@router.post("/verify/{verify_id}/edit")
async def verify_edit(verify_id: int, request: Request):
    form = await request.form()
    conn = _conn()
    try:
        current = verify.get_verified(conn, verify_id)
        if current is None:
            raise ValueError("no such verify row")
        changes = {}
        for c in list(V0_COLUMNS) + list(RAW_DATE_COLUMNS):
            new = form.get(c)
            if new is None:
                continue
            old = "" if current[c] is None else str(current[c])
            if new != old:
                changes[c] = new
        desc = (form.get("edit_description") or "").strip()
        # A change to `flag` and nothing else is its own reason -- see
        # flag_only_reason(). Any other cell, alone or alongside the flag, still
        # has to be explained, and verify.edit refuses a blank description.
        if changes and not desc:
            desc = flag_only_reason(changes, current["flag"]) or ""
        if not changes:
            msg = "No cells changed — nothing to record."
        elif not desc:
            msg = ("You changed " + ", ".join(sorted(changes))
                   + " — enter a reason before saving; it goes to verify_edits.")
        else:
            notices = verify.edit(conn, verify_id, changes, edit_description=desc)
            msg = f"Saved {len(changes)} change(s) to Verify #{verify_id}."
            if notices:
                msg += " " + " ".join(notices)
    except Exception as e:
        msg = f"Error: {e}"
    finally:
        conn.close()
    return RedirectResponse(f"/verify/{verify_id}?msg={html.escape(msg)}", status_code=303)
