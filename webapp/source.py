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
    _cell, _conn, _db_bar, _downstream_map, _keep, _lineage_pill, _page,
    _remember_show, _resolve_show, _stage_toggle, _to_int, _verdict_span, esc,
)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Source                                                                       #
# --------------------------------------------------------------------------- #

@router.get("/source", response_class=HTMLResponse)
def source_page(request: Request, msg: Optional[str] = None, show: Optional[str] = None):
    show = _resolve_show(request, "/source", show)
    conn = _conn()
    try:
        all_rows = source.list_leads(conn)
        extracted = _downstream_map(conn, "screen_extracted", "source_collected_id")
    finally:
        conn.close()

    rows = [r for r in all_rows if _keep(r["id"], extracted, show)]
    n_done = sum(1 for r in all_rows if r["id"] in extracted)
    n_pending = len(all_rows) - n_done
    toggle = _stage_toggle("/source", show, {
        "all": f"All ({len(all_rows)})",
        "pending": f"Not yet in Screen ({n_pending})",
        "done": f"Already in Screen ({n_done})",
    })

    items = "".join(
        f"""<div class="card"><b>#{r['id']}</b> {esc(r['summary'])}
        {_lineage_pill(r['id'], extracted, "Screen", "not extracted yet")}<br>
        <small>promise:</small> {esc(r['promise_source'])}<br>
        <small>status:</small> {esc(r['status_source'])}
        {"<br><small>date:</small> " + esc(r['promised_date_source']) if r['promised_date_source'] else ""}
        <div style="margin-top:.4rem">
        <a href="/screen/prompt?source_id={r['id']}"><button type="button">Claude Code: extract prompt</button></a>
        <form class="inline" method="post" action="/screen/extract">
          <input type="hidden" name="source_id" value="{r['id']}">
          <button type="submit">API: extract to Screen</button>
        </form></div></div>"""
        for r in rows
    ) or "<p>(no Source leads match this filter)</p>"

    body = f"""
<h2>Collect a lead — Claude Code (no API key)</h2>
<div class="card">
  <p>Render the Source operating prompt, run it in a web-search assistant
  (e.g. Claude Code), then paste the JSON it returns below.</p>
  <p><a href="/source/prompt"><button type="button" class="primary">Show Source prompt to run</button></a></p>
  <form method="post" action="/source/add-json">
    <label>Paste the lead JSON returned by Claude Code</label>
    <textarea name="lead_json" rows="5" placeholder='{{"promise_source": "https://…", "status_source": "https://…", "summary": "…"}}'></textarea>
    <p><button type="submit">Ingest lead JSON</button></p>
  </form>
</div>

<h2>Add a lead — manual</h2>
<div class="card"><form method="post" action="/source/add">
  <label>promise_source * (announcement URL)</label><input type="text" name="promise_source" required>
  <label>status_source * (current-status URL)</label><input type="text" name="status_source" required>
  <label>promised_date_source (optional)</label><input type="text" name="promised_date_source">
  <label>summary (optional context)</label><input type="text" name="summary">
  <p><button type="submit">Add lead</button></p>
</form></div>

<h2>Collect a lead — direct API</h2>
<div class="card"><form method="post" action="/source/collect">
  <p>Calls the Anthropic API with web search directly. Needs <code>ANTHROPIC_API_KEY</code>.</p>
  <button type="submit">Collect one new lead (API)</button>
</form></div>

<h2>Leads ({len(rows)} of {len(all_rows)})</h2>
{toggle}
{items}
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

