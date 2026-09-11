"""
source.py -- the Source stage pages of the web interface.

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
)
from pipeline.llm import LLMUnavailable  # noqa: E402

from webapp.shared import (  # noqa: E402
    _cell, _conn, _downstream_map, _keep, _lineage_pill, _page,
    _remember_show, _resolve_show, _stage_toggle, _to_int, _verdict_span, esc,
)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Source                                                                       #
# --------------------------------------------------------------------------- #

def _extract_actions(lead_id: int, extracted: dict) -> str:
    """Per-lead extract controls, shown only on a lead with no Screen row.

    Same rule as the Screen list's check button. These are not merely noise on
    an already-extracted lead: insert_extracted refuses a second row per lead,
    so pressing either one there produces an error. All 173 rows in this
    database were extracted by the collection loop, not by these buttons.
    """
    if lead_id in extracted:
        return ""
    return (f'<div style="margin-top:.4rem">'
            f'<a href="/screen/prompt?source_id={lead_id}">'
            f'<button type="button">Claude Code: extract prompt</button></a>'
            f'<form class="inline" method="post" action="/screen/extract">'
            f'<input type="hidden" name="source_id" value="{lead_id}">'
            f'<button type="submit">API: extract to Screen</button>'
            f'</form></div>')


@router.get("/source", response_class=HTMLResponse)
def source_page(request: Request, msg: Optional[str] = None, show: Optional[str] = None):
    conn = _conn()
    try:
        all_rows = source.list_leads(conn)
        extracted = _downstream_map(conn, "screen_extracted", "source_collected_id")
    finally:
        conn.close()

    n_done = sum(1 for r in all_rows if r["id"] in extracted)
    n_pending = len(all_rows) - n_done
    # Resolved after the counts, so an empty queue can fall back to "all".
    show = _resolve_show(request, "/source", show, n_pending)
    rows = [r for r in all_rows if _keep(r["id"], extracted, show)]
    toggle = _stage_toggle("/source", show, {
        "all": f"All ({len(all_rows)})",
        "pending": f"Not yet screened ({n_pending})",
        "done": f"Screened ({n_done})",
    })

    items = "".join(
        f"""<div class="card"><b>#{r['id']}</b> {esc(r['summary'])}
        {_lineage_pill(r['id'], extracted, "Screen", "not screened yet")}<br>
        <small>promise:</small> {esc(r['promise_source'])}<br>
        <small>status:</small> {esc(r['status_source'])}
        {"<br><small>date:</small> " + esc(r['promised_date_source']) if r['promised_date_source'] else ""}
        {_extract_actions(r['id'], extracted)}</div>"""
        for r in rows
    ) or "<p>(no Source leads match this filter)</p>"

    # Three collection forms used to open this page, above the leads they
    # produce. In 173 leads, not one arrived through any of them: every row in
    # source_collected carries collected_via='prompt1', the agent loop. The
    # methodology says the same thing in plain words -- Source and Screen are
    # extracted agentically, and the interface exists for the Verify gate.
    #
    # So the page leads with what the collector found, which is the question a
    # person actually has here, and the three by-hand routes fold into one
    # disclosure at the bottom for the day a lead arrives by email.
    body = f"""
<p class="pagelede">What the Source agent found. Collection runs from a
terminal: <code>N=5 bash collect/all.sh</code>. Nothing on this page is part of
the normal loop.</p>

<h2>Leads ({len(rows)} of {len(all_rows)})</h2>
{toggle}
{items}

<details class="byhand">
<summary>Add a lead by hand</summary>
<div class="card">
  <p>Three routes in, none of which the collection loop uses. They exist for a
  lead that arrives some other way, such as by email.</p>

  <h3>Paste JSON from a web-search assistant</h3>
  <p><a href="/source/prompt"><button type="button">Show the Source prompt</button></a></p>
  <form method="post" action="/source/add-json">
    <label>Lead JSON</label>
    <textarea name="lead_json" rows="4" placeholder='{{"promise_source": "https://…", "status_source": "https://…", "summary": "…"}}'></textarea>
    <p><button type="submit">Ingest lead JSON</button></p>
  </form>

  <h3>Type the two links</h3>
  <form method="post" action="/source/add">
    <label>promise_source * (announcement URL)</label><input type="text" name="promise_source" required>
    <label>status_source * (current-status URL)</label><input type="text" name="status_source" required>
    <label>promised_date_source (optional)</label><input type="text" name="promised_date_source">
    <label>summary (optional context)</label><input type="text" name="summary">
    <p><button type="submit">Add lead</button></p>
  </form>

  <h3>Call the API directly</h3>
  <form method="post" action="/source/collect">
    <p>One call to the Anthropic API with web search. Needs
    <code>ANTHROPIC_API_KEY</code>.</p>
    <button type="submit">Collect one lead</button>
  </form>
</div>
</details>
"""
    return _remember_show(_page("Source", body, msg), "/source", show)


@router.get("/source/prompt", response_class=HTMLResponse)
def source_prompt_page():
    conn = _conn()
    try:
        prompt = llm.render_source_prompt(
            avoid_published=orch.published_project_names(conn),
            avoid_unpublished=orch.unpublished_project_names(conn),
            year_coverage=orch.announced_year_coverage(conn),
        )
    finally:
        conn.close()
    body = f"""
<h2>Source prompt — run this in Claude Code</h2>
<div class="card">
  <p>Copy everything below, run it in a web-search-capable assistant, then bring
  the JSON it returns back to the <a href="/source">Source page</a> → "Ingest lead JSON".</p>
  <textarea rows="22" onclick="this.select()">{esc(prompt)}</textarea>
</div>
<div class="card"><form method="post" action="/source/add-json">
  <label>…or paste the lead JSON here directly</label>
  <textarea name="lead_json" rows="5"></textarea>
  <p><button class="primary" type="submit">Ingest lead JSON</button></p>
</form></div>
"""
    return _page("Source prompt", body)


@router.post("/source/add")
async def source_add(request: Request):
    form = await request.form()
    conn = _conn()
    try:
        bid = source.insert_lead(
            conn,
            promise_source=form.get("promise_source", ""),
            status_source=form.get("status_source", ""),
            promised_date_source=form.get("promised_date_source"),
            summary=form.get("summary"),
        )
        msg = f"Added Source lead #{bid}."
    except Exception as e:
        msg = f"Error: {e}"
    finally:
        conn.close()
    return RedirectResponse(f"/source?msg={html.escape(msg)}", status_code=303)


@router.post("/source/add-json")
async def source_add_json(request: Request):
    form = await request.form()
    conn = _conn()
    try:
        lead = json.loads(form.get("lead_json") or "{}")
        bid = source.insert_lead(
            conn,
            promise_source=lead.get("promise_source", ""),
            status_source=lead.get("status_source", ""),
            promised_date_source=lead.get("promised_date_source"),
            summary=lead.get("summary"),
        )
        msg = f"Ingested Source lead #{bid} from JSON."
    except Exception as e:
        msg = f"Error: {e}"
    finally:
        conn.close()
    return RedirectResponse(f"/source?msg={html.escape(msg)}", status_code=303)


@router.post("/source/collect")
def source_collect():
    conn = _conn()
    try:
        bid, lead = orch.run_source_ai(conn)
        msg = f"Collected Source lead #{bid}: {lead.get('summary', '')[:80]}"
    except LLMUnavailable as e:
        msg = f"API collect failed: {e}"
    except Exception as e:
        msg = f"Error: {e}"
    finally:
        conn.close()
    return RedirectResponse(f"/source?msg={html.escape(msg)}", status_code=303)

