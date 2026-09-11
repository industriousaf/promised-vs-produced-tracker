"""
screen.py -- the Screen stage pages of the web interface.

Routes are collected on an APIRouter here and mounted onto the app in main.py,
so this module never creates a server of its own.
"""

from __future__ import annotations

from collections import Counter
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
    _remember_show, _resolve_show, _stage_toggle, _to_int, _verdict_legend,
    _verdict_span, esc,
    flag_only_reason,
)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Screen                                                                       #
# --------------------------------------------------------------------------- #

@router.get("/screen", response_class=HTMLResponse)
def screen_page(request: Request, msg: Optional[str] = None, show: Optional[str] = None,
                verdict: Optional[str] = None):
    conn = _conn()
    try:
        all_rows = screen.list_extracted(conn)
        promoted = _downstream_map(conn, "verify_verified", "screen_extracted_id")
        queue = screen.review_queue(conn)

        n_done = sum(1 for r in all_rows if r["id"] in promoted)
        n_pending = len(all_rows) - n_done
        # Resolved after the counts, so a drained queue falls back to "all"
        # rather than rendering an empty list under a toggle reading (0).
        show = _resolve_show(request, "/screen", show, n_pending)
        staged = [r for r in all_rows if _keep(r["id"], promoted, show)]
        # Every row's verdict, before the verdict filter narrows the list, so
        # the legend below can state the whole distribution.
        all_checks = {r["id"]: screen.latest_check(conn, r["id"]) for r in staged}
    finally:
        conn.close()

    def _v(row_id):
        """A row's latest verdict, or None. latest_check returns a sqlite3.Row,
        which has no .get()."""
        chk = all_checks.get(row_id)
        return chk["result_status"] if chk is not None else None

    verdict = verdict if verdict in ("CLEAN", "PASS", "FAIL") else None
    rows = [r for r in staged if _v(r["id"]) == verdict] if verdict else staged
    checks = all_checks

    toggle = _stage_toggle("/screen", show, {
        "all": f"All ({len(all_rows)})",
        "pending": f"Not yet verified ({n_pending})",
        "done": f"Verified ({n_done})",
    })

    # The three verdicts, defined where they are used, with their counts, and
    # each one a filter. A glossary elsewhere would be read once and forgotten;
    # this sits beside the rows it describes, states the whole distribution
    # (which is the question "is this healthy?"), and teaches the vocabulary by
    # being the control you reach for.
    counts = Counter(_v(r["id"]) for r in staged)
    legend = _verdict_legend(counts, verdict, show)

    def _row_html(r):
        chk = checks[r["id"]]
        verdict = chk["result_status"] if chk else None
        # The per-row check button shows ONLY when there is no verdict yet.
        #
        # It used to sit on every row, beside a verdict that was already
        # printed, which made it read as step one of two before "Inspect &
        # promote". It is not: extraction runs the check straight afterwards,
        # so every collected row arrives with one. The button did nothing a
        # person wanted and quietly appended another screen_check row each
        # press.
        #
        # It still earns its place on a row that has no check, which is what
        # `screen-add` and the paste-JSON form below produce -- adding a row
        # deliberately does not check it.
        #
        # The two reasons to RE-check both have better homes than a per-row
        # button: a rule change regrades every row, so it belongs to "Run the
        # deterministic check on all rows" at the top of this page, and a
        # one-off re-check lives on the inspect page next to the report it
        # would change.
        check_btn = "" if verdict else (
            f'<form class="inline" method="post" action="/screen/check">'
            f'<input type="hidden" name="screen_id" value="{r["id"]}">'
            f'<button type="submit">Run check</button></form>')
        # Promotion now lives INSIDE the per-row inspect page (so you can review
        # every extracted field first) -- the list just links there.
        return f"""<div class="card"><b>#{r['id']}</b> {esc(r['project'])}
          <small>({esc(r['sector'])}, {esc(r['state'])})</small>
          {_lineage_pill(r['id'], promoted, "Verify", "not verified yet")}
          check: {_verdict_span(verdict, chk)}
          {check_btn}
          <a href="/screen/{r['id']}/inspect"><button type="button" class="primary">Inspect &amp; verify →</button></a>
          <br><small>{esc(r['current_status'])}</small>
          {"<br><small>flag: " + esc(r['flag']) + "</small>" if r['flag'] else ""}
        </div>"""

    items = "".join(_row_html(r) for r in rows) or "<p>(no projects match this filter)</p>"
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
            blocked_note = (f" A further <b>{n_blocked}</b> cannot be verified until a "
                            f"failing check is fixed: {links}.")
        lede = (f"<p><b>{n_ready} project(s) are waiting for you.</b> Open one, check "
                "every field against the two sources, then verify it. Nothing "
                "reaches the published Scoreboard until a person does this."
                + blocked_note + "</p>")
    else:
        lede = ("<p>Nothing is waiting: every project has been through the human "
                "gate. <a href=\"/verify\">See the Scoreboard</a>.</p>")

    body = f"""
<h2>Review queue</h2>
<div class="card">{lede}
<p><small>Or work the same queue in a terminal, largest capital first:
<code>python3 scoreboard.py review</code></small></p></div>

<h2>Projects ({len(rows)} of {len(all_rows)})</h2>
{toggle}
{legend}
{items}

<details class="byhand">
<summary>Re-check every project</summary>
<div class="card">
  <p>All {len(all_rows)} projects already carry a check; it runs seconds after
  each one is extracted. Re-checking is for one situation: you changed the inclusion
  rules in <code>pipeline/settings.py</code>. Each project is judged against the
  phase that admitted it, so a change there is the only thing that can make a
  stored verdict wrong, and nothing else in this interface will tell you.</p>
  <p><small>This reads the shape of every record again. It never opens a source
  link; that is the job of the check on the review screen, which asks a model
  to read the cited pages. It appends {len(all_rows)} new rows to
  <code>screen_check</code>, which keeps its history rather than overwriting.</small></p>
  <form class="inline" method="post" action="/screen/check-all">
    <button type="submit">Re-check all {len(all_rows)} projects</button></form>
</div>
</details>

<hr>
<h2>Add projects by hand</h2>
<p><small>Rarely needed — the collection loops fill this stage. Use these to
extract a specific lead, or to paste a project you built yourself.</small></p>

<div class="card"><form method="get" action="/screen/prompt">
  <label>Extract a Source lead with Claude Code — lead id</label>
  <input type="text" name="source_id" required>
  <p><button type="submit">Show Screen prompt to run</button></p>
</form></div>

<div class="card"><form method="post" action="/screen/add">
  <label>source_id (optional lineage)</label><input type="text" name="source_id">
  <label>project JSON (v0 columns; verification_tier forced to P; lag/slip + *_dt derived)</label>
  <textarea name="row_json" rows="8">{esc(example)}</textarea>
  <p><button type="submit">Add project</button></p>
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
  <label>Paste the project JSON returned by Claude Code</label>
  <textarea name="row_json" rows="8"></textarea>
  <p><button class="primary" type="submit">Ingest project JSON</button></p>
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
        msg = f"Added project #{sid} to Screen (tier forced to P)."
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
        msg = f"Extracted project #{sid} to Screen from lead #{bid}."
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
        msg = f"Re-checked {n} project(s)."
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

# What each field means, shown beside its label.
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


# --------------------------------------------------------------------------- #
# What the deterministic check is actually doing                               #
#                                                                              #
# It reported a verdict and two counts -- "PASS, 0 errors, 2 warnings" -- which  #
# is a result with no question attached. A reviewer could not tell what had been #
# tested, and so could not tell what a PASS was worth or which of their own      #
# concerns it had already settled. The rules below are the same ones            #
# schema.validate_row applies, in the order it applies them, each shown against  #
# THIS ROW'S value: "announced is a strict YYYY-MM anchor" means nothing until   #
# it is sitting next to 2022-01.                                                #
#                                                                              #
# The list is prose about code, so it can drift from the code. What keeps it    #
# honest is the third column: every verdict comes from the stored check report,  #
# never from re-deciding the rule here. A rule this list describes wrongly still #
# shows the checker's own answer.                                               #
# --------------------------------------------------------------------------- #

CHECK_RULES: list[tuple[tuple[str, ...], str]] = [
    (("project",), "a project name is present"),
    (("sector",), "sector is one of the defined manufacturing sectors"),
    (("state",), "state is a real US state or territory"),
    (("announced",), "announced is a strict <code>YYYY-MM</code> anchor — every "
                     "lag and slip figure is measured from it"),
    (("promised_capital_usd", "promised_jobs"),
     f"the size floor for phase <b>{_crit().id}</b>: {_crit().describe()} "
     f"(either figure alone puts the project in scope under OR)"),
    (("promised_first_output",),
     "promised_first_output holds a 4-digit year or a sentinel"),
    (("actual_first_output",),
     "actual_first_output holds a 4-digit year or a sentinel "
     "(<code>pending</code> / <code>never</code> / <code>unconfirmed</code>)"),
    (("current_status",), "current_status is not empty"),
    (("lag_years",), "the derived lag parses as a number or a sentinel"),
    (("verification_tier",), "the tier is a valid token (P / V1 / V2, or a pair)"),
    (("promise_source", "status_source", "promised_date_source",
      "actual_date_source"),
     "every source cell that holds anything is URL-shaped"),
    (("flag",), "an unresolved flag is surfaced as a warning"),
]

# The other half of the panel, and the reason the agentic check exists. Every
# line here is something a reviewer could reasonably think a green CLEAN had
# covered.
CHECK_BLIND_SPOTS = [
    "whether the cited page <b>says</b> any of this — it never opens a link",
    "whether the link resolves at all, or 404s",
    "whether the status is <b>current</b>, or two years stale",
    "whether the two sources are about the same facility",
    "whether the project is real",
]


_CHECKLIST_JS = """
(function () {
  var strips = Array.prototype.slice.call(document.querySelectorAll('.ck'));
  if (!strips.length) { return; }
  var tally = document.getElementById('cktally');
  var left = document.getElementById('ckleft');
  var go2 = document.getElementById('ckgo');
  var walk = document.getElementById('ckwalk');
  // Per project, per tab, gone when the tab closes. Nothing here is a record;
  // it exists so tabbing away does not lose your place.
  var KEY = 'pvp-ck-' + ROW_ID;
  var state = {}, looked = {}, walking = false, armed = false;
  try { state = JSON.parse(sessionStorage.getItem(KEY) || '{}'); } catch (e) {}
  try { looked = JSON.parse(sessionStorage.getItem(KEY + '-seen') || '{}'); } catch (e) {}

  function save() {
    try {
      sessionStorage.setItem(KEY, JSON.stringify(state));
      sessionStorage.setItem(KEY + '-seen', JSON.stringify(looked));
    } catch (e) {}
  }

  function unsettled() {
    return strips.filter(function (s) { return !state[s.dataset.cell]; });
  }

  // Open one field in the pane and put it under the reader's eye. The link is
  // an ordinary target="evidencepane" anchor, so clicking it is the whole
  // mechanism; there is no channel to the frame and none is needed.
  function openField(strip) {
    if (!strip) { return; }
    strips.forEach(function (s) { s.classList.toggle('is-now', s === strip); });
    var a = strip.querySelector('a.ck-go');
    if (a) { a.click(); }
    strip.scrollIntoView({ block: 'center' });
    var f = strip.closest('div');
    var input = f && f.querySelector('input, select');
    if (input) { input.focus({ preventScroll: true }); }
  }

  function paint() {
    var done = 0;
    strips.forEach(function (s) {
      var cell = s.dataset.cell, v = state[cell] || '';
      var ok = s.querySelector('.ck-ok'), no = s.querySelector('.ck-no');
      var lbl = s.querySelector('.ck-state');
      s.classList.toggle('is-ok', v === 'ok');
      s.classList.toggle('is-no', v === 'no');
      ok.classList.toggle('on', v === 'ok');
      no.classList.toggle('on', v === 'no');
      // Confirmable only after the field has been opened in the pane. Not
      // proof of reading, but it stops a project being ticked clean without
      // the document ever moving.
      ok.disabled = (v !== 'ok') && !looked[cell];
      lbl.textContent = v === 'ok' ? 'confirmed'
                      : v === 'no' ? 'not in this source'
                      : looked[cell] ? 'opened, not settled' : '';
      if (v) { done++; }
    });
    var all = (done === strips.length);
    if (tally) {
      tally.textContent = all ? ('all ' + strips.length + ' fields checked')
                              : (done + ' of ' + strips.length + ' fields checked');
      tally.classList.toggle('all', all);
    }
    if (left) {
      var open = unsettled().map(function (s) { return s.dataset.cell; });
      left.textContent = open.length ? ('still open: ' + open.join(', ')) : '';
    }
    if (walk) {
      walk.hidden = all;
      walk.textContent = done ? 'continue \u2192' : 'start checking \u2192';
    }
    if (go2) { go2.hidden = !all || !document.getElementById('verifyrow'); }

    // The verify button is NEVER disabled by the checklist. The checklist is
    // sessionStorage: clearing it, or opening the project in another browser,
    // must not be able to stop a person publishing. The command line has no
    // such gate either, so disabling here would guard one of two doors while
    // implying a stronger claim than the evidence supports -- a published
    // project would read as "all six confirmed" when what happened is "six
    // buttons were pressed in a browser that stored nothing".
    //
    // What it does instead is stop looking like the obvious next thing until
    // the checking is done, and ask once if you go early.
    var vb = document.getElementById('verifybtn');
    var vn = document.getElementById('verifynote');
    if (vb) {
      vb.classList.toggle('primary', all);
      armed = armed && !all;
      if (vn) {
        var open2 = unsettled().map(function (s) { return s.dataset.cell; });
        vn.textContent = all ? ''
          : armed ? ('press again to verify with ' + open2.length + ' unchecked')
          : (open2.length + ' field(s) not checked yet');
        vn.classList.toggle('armed', armed);
      }
    }
    var wrap = document.querySelector('.cktally-wrap');
    if (wrap) { wrap.classList.toggle('ready', all); }
    if (all) { strips.forEach(function (s) { s.classList.remove('is-now'); }); }
  }

  // Settling a field during a walk moves to the next one. Outside a walk it
  // does not, because yanking the page after a stray click is worse than
  // leaving the reader where they were.
  function settle(strip, value) {
    var cell = strip.dataset.cell;
    state[cell] = (state[cell] === value) ? '' : value;
    save(); paint();
    if (walking && state[cell]) {
      var next = unsettled()[0];
      if (next) { openField(next); }
      else { walking = false; if (go2 && !go2.hidden) { go2.focus(); } }
    }
  }

  strips.forEach(function (s) {
    var cell = s.dataset.cell;
    var go = s.querySelector('a.ck-go');
    if (go) {
      go.addEventListener('click', function () {
        looked[cell] = 1; save(); setTimeout(paint, 0);
      });
    }
    s.querySelector('.ck-ok').onclick = function () { settle(s, 'ok'); };
    s.querySelector('.ck-no').onclick = function () { settle(s, 'no'); };
  });

  if (walk) {
    walk.onclick = function () {
      walking = true;
      openField(unsettled()[0]);
    };
  }

  // Going early asks once, in the page, naming what is unchecked. A second
  // press goes through. No dialog: this is a decision about the reviewer's own
  // notes, not a destructive action needing browser chrome.
  if (vb0()) {
    vb0().form.addEventListener('submit', function (e) {
      if (unsettled().length === 0 || armed) { return; }
      e.preventDefault();
      armed = true; paint();
      vb0().focus();
    });
  }
  function vb0() { return document.getElementById('verifybtn'); }

  if (go2) {
    go2.onclick = function () {
      var t = document.getElementById('verifyrow');
      if (!t) { return; }
      t.scrollIntoView({ block: 'center' });
      t.classList.add('landed');
      setTimeout(function () { t.classList.remove('landed'); }, 1400);
      var b = t.querySelector('button');
      if (b) { b.focus(); }
    };
  }

  paint();
})();
"""

# The per-cell checklist. A scratchpad, not a record: it lives in the browser
# for this tab only and nothing about it reaches the database.
#
# The job it does is small and real. Verifying a row means confirming six cells
# against a document, and across 162 rows that is roughly a thousand
# confirmations. Nothing tracked which cells you had already done inside a row,
# so tabbing away at cell five meant starting the row again.
#
# Three states, not a checkbox, because a binary tick throws away the most
# interesting answer. A cell the page does not carry is not "unchecked": its
# absence is the finding, and the pane hint has always said so. So: not looked
# at, confirmed here, or not in this source.
#
# `confirm` stays disabled until the pane reports that it scrolled to this
# cell's marks. That is not proof of reading, but it stops a row being ticked
# clean without the document ever moving, which is the failure mode that would
# make the checklist worth less than no checklist.
CHECKLIST_CELLS = ("announced", "promised_first_output", "actual_first_output",
                   "promised_capital_usd", "promised_jobs", "current_status")


def _check_strip(cell: str, row_id: int, ftabs: dict) -> str:
    """One cell's three-state control.

    "find in source" is an ordinary link with target="evidencepane", the same
    mechanism the tab strip above the pane already uses. postMessage was the
    first attempt and does not survive this iframe's sandbox: it carries no
    allow-same-origin, so the frame has an opaque origin and nothing crosses
    in either direction. A link needs no channel at all.
    """
    if cell not in CHECKLIST_CELLS:
        return ""
    tab = ftabs.get(cell)
    if tab is None:
        # No cited page marks this cell, so there is nothing to jump to. The
        # only honest states left are "not in this source" and unset.
        go = '<span class="ck-nolink">no source cites this field</span>'
    else:
        go = (f'<a class="ck-go" target="evidencepane" '
              f'href="/evidence/screen/{row_id}?tab={tab}&amp;field={esc(cell)}">'
              f'find in source \u2192</a>')
    return (f'<div class="ck" data-cell="{esc(cell)}">{go}'
            f'<button type="button" class="ck-ok" disabled>confirmed</button>'
            f'<button type="button" class="ck-no">not in this source</button>'
            f'<span class="ck-state"></span></div>')


def _check_panel(r, chk) -> str:
    """The check, as rules-with-values rather than a verdict and two counts."""
    report = []
    if chk is not None:
        try:
            report = json.loads(chk["report"] or "[]")
        except (json.JSONDecodeError, TypeError):
            report = []
    worst: dict[str, tuple[str, str]] = {}
    for issue in report:
        col, level = issue.get("column", ""), issue.get("level", "")
        if col not in worst or level == "ERROR":
            worst[col] = (level, issue.get("message", ""))

    rows = ""
    for cols, text in CHECK_RULES:
        vals = " · ".join(
            f"{c}={_cell(r, c) if _cell(r, c) not in (None, '') else '—'}"
            for c in cols
        ) if len(cols) > 1 else str(_cell(r, cols[0]) or "—")
        hits = [worst[c] for c in cols if c in worst]
        if chk is None:
            cls, mark, why = "", "not run", ""
        elif not hits:
            cls, mark, why = "ok", "✓", ""
        else:
            level = "ERROR" if any(h[0] == "ERROR" for h in hits) else "WARN"
            cls = "err" if level == "ERROR" else "warn"
            mark = "✗ error" if level == "ERROR" else "! warning"
            why = "<br><small>" + esc("; ".join(h[1] for h in hits)) + "</small>"
        rows += (f"<tr><td>{text}{why}</td>"
                 f'<td class="val">{esc(vals[:120])}</td>'
                 f'<td class="{cls}">{mark}</td></tr>')

    blind = "".join(f"<li>{b}</li>" for b in CHECK_BLIND_SPOTS)
    return f"""<details class="explain">
<summary>What this check tested, and what it cannot test</summary>
<p><small>It reads the <b>shape</b> of the record. Every rule below is
<code>pipeline/schema.py</code> applied to the fields as they stand, and each
verdict is the checker's own, read back from the stored report — not re-decided
here.</small></p>
<table class="rules"><tr><th>rule</th><th>this project</th><th></th></tr>{rows}</table>
<p style="margin-top:.7rem"><b>It cannot test:</b></p>
<ul>{blind}</ul>
<p><small><b>PASS / CLEAN means well-formed, not true.</b> Everything in that
list is the human gate's job — and the pane on the left, plus the agentic check
below, are the tools for it.</small></p>
</details>"""


@router.get("/screen/{screen_id}/inspect", response_class=HTMLResponse)
def screen_inspect(screen_id: int, msg: Optional[str] = None):
    conn = _conn()
    try:
        r = screen.get_extracted(conn, screen_id)
        if r is None:
            return _page("Screen inspect", "<p>No project with that id.</p>", "Not found")
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
        <input type="text" name="{esc(c)}" value="{esc(r[c])}">{_check_strip(c, r['id'], ftabs)}</div>""")

    ftabs = evidence.field_tabs(r)
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
        # shows the promised side and the produced side, two halves of one
        # not two readings of one claim, so V2 was never answerable from what
        # is on screen. The CLI queue stamps V1 too; a picker here would be the
        # same non-choice in the other interface. Deliberate V2 goes through
        # `verify-promote --tier V2` once a second source has actually been found.
        promote_controls = """
    <input type="hidden" name="tier" value="V1">
    <p class="tiernote">Publishes as <b>V1</b>, one source read.
    <span>V2 needs a second independent source and the command line.</span></p>
    <label>Reason — required if you changed a field (recorded in
      <code>verify_edits</code>). A change to <code>flag</code> and nothing else
      writes its own reason, so leave this empty for that.</label>
    <input type="text" name="edit_description"
           placeholder="e.g. corrected announced date to match the filing">
    <p id="verifyrow"><button id="verifybtn" type="submit">Verify this project \u2192</button>
    <span id="verifynote" class="verifynote"></span></p>"""
    else:
        promote_controls = (
            '<p class="msg">Re-check and reach '
            "<b>PASS</b> or <b>CLEAN</b> before it can be verified.</p>"
        )

    check_note = (
        f"<small>last run: {chk['n_errors']} error(s), "
        f"{chk['n_warnings']} warning(s)</small>"
        if chk else "<small> — not checked yet</small>"
    )

    # The document pane leads and the form follows it, in that order, because
    # that is the order of the work: you read the page, then you say what the row
    # should hold. The two sit in one sticky two-column grid so neither ever
    # scrolls the other off the screen.
    body = f"""
<p><a href="/screen">← back to Screen</a></p>
<h2 class="rowtitle">Screen #{r['id']} \u00b7 {esc(r['project'])}
  <small>({esc(r['sector'])}, {esc(r['state'])})</small></h2>
<p><small>from source_collected #{esc(r['source_collected_id'])} ·
  extracted {esc(r['datetime'])} · check: {_verdict_span(verdict, chk)}</small></p>

<div class="card">
  <form class="inline" method="post" action="/screen/check">
    <input type="hidden" name="screen_id" value="{r['id']}">
    <button type="submit">Re-check</button></form>
  {check_note}
  {_check_panel(r, chk)}
</div>

<div class="review">
  <div class="doccol">
    {evidence.pane_html("screen", r["id"], r)}
  </div>

  <div class="formcol">
    <div class="card">
      <p>Confirm every field against the pane, then verify. Any field you change
      is applied to the project's new Verify row and logged in <code>verify_edits</code>.
      <b>lag_years / slip_years and the <code>*_dt</code> columns are derived</b>
      from the date strings and recompute when you edit a date.</p>
      <div class="cktally-wrap"><span id="cktally" class="cktally"></span>
      <span id="ckleft" class="cktally-left"></span><button type="button" id="ckwalk" class="ckwalk" hidden>start checking \u2192</button><button type="button" id="ckgo" class="ckgo" hidden>verify this project \u2192</button></div>
      <p><small>The strip under each field is a scratchpad for your own place in
      this project. It is not stored and does not gate verifying.</small></p>
      <form method="post" action="/screen/{r['id']}/promote">
        <div class="grid2">{fields}</div>
        <p><small>Verbatim source text — the exact page text each date came
        from. This is what the pane searches for first, so a quote that lights
        up nothing is worth looking at:</small></p>
        <div class="grid2">{raw_display}</div>
        <p><small>Derived fields (read-only):</small></p>
        <div class="grid2">{derived}{dt_display}</div>
        {promote_controls}
      </form>
    </div>
  </div>
</div>

<script>var ROW_ID = {r['id']};</script>
<script>{_CHECKLIST_JS}</script>

<h2>Ask a model to check it against the links</h2>
<div class="card">{agent_pane.picker_html("screen", r["id"], r)}</div>
"""
    return _page(f"Screen #{screen_id}", body, msg, wide=True)


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

        # An edit needs its provenance reason before we touch Verify -- unless
        # the only cell that moved is `flag`, which is already a written
        # statement of what is unresolved about the row and so is its own reason.
        # See flag_only_reason() for why that exemption stops exactly there.
        if changes and not desc:
            desc = flag_only_reason(changes, src["flag"]) or ""
        if changes and not desc:
            msg = ("You changed " + ", ".join(sorted(changes))
                   + ", enter a reason (it goes to verify_edits) before verifying.")
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

