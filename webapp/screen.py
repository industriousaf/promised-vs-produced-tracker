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
from urllib.parse import quote as urlquote

# webapp/ -> tracker/, so `pipeline` imports resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import APIRouter, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse  # noqa: E402

from pipeline import source, screen, verify, orchestrate as orch, llm, settings  # noqa: E402
from pipeline.db import (  # noqa: E402
    READ_ONLY, connect, db_path, discover_databases, init_db, is_read_only,
    set_active_db, table_counts,
)
from pipeline.settings import active as _crit  # noqa: E402
from pipeline.dates import lag_label, measured_spans  # noqa: E402
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
    flag_only_reason, date_kind_select, status_select, DATEKIND_JS,
)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Screen                                                                       #
# --------------------------------------------------------------------------- #

@router.get("/screen", response_class=HTMLResponse)
def screen_page(request: Request, msg: Optional[str] = None, show: Optional[str] = None,
                verdict: Optional[str] = None, resettle: Optional[str] = None):
    conn = _conn()
    try:
        # Largest capital first, the order review_queue uses, because the
        # dashboard sends people here under the words "largest capital first".
        # This list used to come back by id -- collection order, which means
        # nothing to a reviewer -- and the first thirty projects were verified
        # as ids 1 to 30 while three projects over $10B waited below them. The
        # order is the claim: wherever review stops, the Tracker above that
        # point is complete, and that only holds if the walk is top-down.
        all_rows = screen.list_extracted(conn, by_capital=True)
        promoted = _downstream_map(conn, "verify_verified", "screen_extracted_id")
        queue = screen.review_queue(conn)
        # Blocked projects are left out. A failing check means they cannot be
        # verified, so re-settling one changes nothing a reader sees. Once a fix
        # makes one verifiable it leaves `blocked`, and its fields reappear here.
        # Out-of-scope projects are left out for the same reason and permanently:
        # no fix returns them, because nothing about them is broken.
        blocked = {b["id"] for b in queue["blocked"] + queue["out_of_scope"]}
        flagged = {sid: fields for sid, fields in screen.needs_resettle(conn).items()
                   if sid not in blocked}
        waiting = agent_pane.waiting_checks(conn, current_verifier(request))

        n_done = sum(1 for r in all_rows if r["id"] in promoted)
        n_pending = len(all_rows) - n_done
        # Resolved after the counts, so a drained queue falls back to "all"
        # rather than rendering an empty list under a toggle reading (0).
        show = _resolve_show(request, "/screen", show, n_pending)
        # ?resettle= narrows to projects with a field to settle again, whatever
        # the stage toggle remembers. Almost all of them are already verified, so
        # a remembered "not yet verified" would otherwise hide every one.
        staged = ([r for r in all_rows if r["id"] in flagged] if resettle
                  else [r for r in all_rows if _keep(r["id"], promoted, show)])
        # Every row's verdict, before the verdict filter narrows the list, so
        # the legend below can state the whole distribution.
        all_checks = {r["id"]: screen.latest_check(conn, r["id"]) for r in staged}
        # Blocked projects to the bottom, stable, so capital order survives on
        # either side. A failing check cannot be verified, and on capital order
        # alone a large blocked project could head the list -- making the first
        # card something other than the "first up" the dashboard names, and the
        # first click a dead end.
        staged.sort(key=lambda r: all_checks[r["id"]] is not None
                    and all_checks[r["id"]]["result_status"] == "FAIL")
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

    toggle = _stage_toggle("/screen", "" if resettle else show, {
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
        fix = flagged.get(r["id"])
        # A flagged card says which fields and offers the re-settle, so the list
        # is itself the worklist rather than a count pointing somewhere else.
        action = "Inspect &amp; re-settle →" if fix else "Inspect &amp; verify →"
        fix_note = (f'<br><small class="resettle">re-settle: {esc(", ".join(fix))}</small>'
                    if fix else "")
        return f"""<div class="card"><b>#{r['id']}</b> {esc(r['project'])}
          <small>({esc(r['sector'])}, {esc(r['state'])})</small>
          {_lineage_pill(r['id'], promoted, "Verify", "not verified yet")}
          check: {_verdict_span(verdict, chk)}
          {check_btn}
          <a href="/screen/{r['id']}/inspect"><button type="button" class="primary">{action}</button></a>{fix_note}
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
    n_out = len(q["out_of_scope"])
    if n_ready or n_blocked or n_out:
        blocked_note = ""
        if n_blocked:
            links = ", ".join(f'<a href="/screen/{r["id"]}/inspect">#{r["id"]}</a>'
                              for r in q["blocked"][:8])
            blocked_note = (f" A further <b>{n_blocked}</b> cannot be verified until a "
                            f"failing check is fixed: {links}.")
        # Kept apart from the blocked note on purpose. Both are unverifiable, but
        # one is a queue of work and the other is a decision already made.
        if n_out:
            links = ", ".join(f'<a href="/screen/{r["id"]}/inspect">#{r["id"]}</a>'
                              for r in q["out_of_scope"][:8])
            blocked_note += (f" <b>{n_out}</b> more are out of scope for this phase "
                             f"and will not publish: {links}.")
        lede = (f"<p><b>{n_ready} project(s) are waiting for you.</b> Open one, check "
                "every field against the two sources, then verify it. Nothing "
                "reaches the published Tracker until a person does this."
                + blocked_note + "</p>")
    else:
        lede = ("<p>Nothing is waiting: every project has been through the human "
                "gate. <a href=\"/verify\">See the Tracker</a>.</p>")

    if flagged:
        n_fields = sum(len(v) for v in flagged.values())
        lede += (f'<p><b>{n_fields} field(s) on {len(flagged)} project(s) need a '
                 f're-settle.</b> Each was settled against a value the project no '
                 f'longer holds, or confirmed a field that has no value. '
                 f'<a href="/screen?resettle=1">Show them</a>.</p>')
    resettle_note = ""
    if resettle:
        resettle_note = (
            '<p class="msg">Showing only projects with a field to settle again. '
            'Each page marks the field, and settling it again fixes the record. '
            '<a href="/screen">Show all projects</a>.</p>'
            if flagged else
            '<p class="msg">Nothing needs a re-settle. '
            '<a href="/screen">Show all projects</a>.</p>')

    body = f"""
{agent_pane.checks_html(waiting, "/screen/checks")}
<script>{agent_pane.CHECKS_JS}</script>
<h2>Review queue</h2>
<div class="card">{lede}
<p><small>Or work the same queue in a terminal, largest capital first:
<code>python3 tracker.py review</code></small></p></div>

<h2>Projects ({len(rows)} of {len(all_rows)})</h2>
{resettle_note}
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


@router.get("/screen/checks", response_class=HTMLResponse)
def screen_checks(request: Request, exclude: Optional[int] = None):
    """The waiting model checks alone, for a page to refresh its list while one
    runs. `exclude` is the project that page is showing. See agent.waiting_checks."""
    conn = _conn()
    try:
        items = [i for i in agent_pane.waiting_checks(conn, current_verifier(request))
                 if i["id"] != exclude]
    finally:
        conn.close()
    src = "/screen/checks" + (f"?exclude={exclude}" if exclude else "")
    return HTMLResponse(agent_pane.checks_html(items, src))


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
        "“2025 (second half)”). <code>n/a</code> if no source states one."
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
    "status": (
        "the same fact as one word, the one that gets counted. It has to agree "
        "with actual_first_output: <code>pending</code> → announced, under "
        "construction or paused; a date or <code>unconfirmed</code> → producing "
        "or closed; <code>never</code> → cancelled. A delay is not a status."
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
    "size_source": (
        "only if the capital or jobs figure came from a page outside the "
        "lead's own links. Leave empty otherwise — it usually is."
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
     "promised_first_output holds a 4-digit year or a sentinel, and any word "
     "beside the year is one the date reader knows"),
    (("actual_first_output",),
     "actual_first_output holds a 4-digit year or a sentinel "
     "(<code>pending</code> / <code>never</code> / <code>unconfirmed</code>)"),
    (("current_status",), "current_status is not empty"),
    (("status",), "status is one of the six words and agrees with actual_first_output"),
    (("lag_years",), "the derived lag parses as a number or a sentinel"),
    (("verification_tier",), "the tier is a valid token (P / V1 / V2, or a pair)"),
    (("promise_source", "status_source", "promised_date_source",
      "actual_date_source", "size_source"),
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
  // The sessionStorage key. It holds one thing now -- which fields you have
  // opened in the pane during this visit -- because everything that IS a record
  // lives in screen_attested and arrives in ATTESTED.
  var KEY = 'pvp-ck-' + ROW_ID;
  var offside = {};
  // Settles someone else made, by field. Shown beside the field, not counted.
  var other = {};
  var absenceTick = {}, settledValue = {};
  var state = {}, looked = {}, stale = {}, walking = false, armed = false;

  // What is settled comes from the database, not this browser. That is the
  // whole reason closing the tab no longer loses your place, and the reason a
  // confirmation means something a month from now.
  //
  // A stale one -- the field was edited after it was confirmed -- hydrates as
  // unsettled, because the person confirmed a value the project no longer
  // holds. The label says so rather than silently emptying the tick.
  Object.keys(ATTESTED || {}).forEach(function (f) {
    var a = ATTESTED[f];
    // A settle counts here only if it is yours. Someone else's shows their name
    // and leaves the field open, so whoever verifies a project settles every
    // field of it themselves. Ashwin's settles from 11 September showed as done
    // when Lucas opened #1 and #5, so he published seven fields he never
    // settled, and pressing "confirmed" on a field already showing it did nothing.
    if (a.by !== VERIFIER) { other[f] = a; return; }
    if (a.stale) { stale[f] = 1; return; }
    // Settled against a page this cell cannot be proved from -- the produced
    // side of the row for a promised value, or the other way about. The tick is
    // not shown: the reviewer read the value somewhere that does not settle it.
    if (a.offside) { offside[f] = 1; return; }
    // Confirmed a field that records nothing was stated, before the rule said
    // not to. The tick is not shown; "not in this source" settles it.
    if (a.confirmed_absence) { absenceTick[f] = 1; return; }
    state[f] = (a.state === 'confirmed') ? 'ok' : 'no';
    settledValue[f] = a.value;
    looked[f] = 1;
  });

  // Still the browser's, because it is not a record: it only remembers that you
  // opened a field in the pane during this visit, which is what un-greys the
  // confirm button.
  try {
    var seen = JSON.parse(sessionStorage.getItem(KEY + '-seen') || '{}');
    Object.keys(seen).forEach(function (k) { looked[k] = 1; });
  } catch (e) {}

  // A stale field has to be opened again, even if you opened it earlier in this
  // same visit. The value moved after someone vouched for it, so whatever was
  // on screen before the edit is not what is being asked about now.
  Object.keys(stale).forEach(function (k) { delete looked[k]; });
  // Same for an off-side one, and for the same reason: what was on screen was
  // the wrong document, so the field has to be opened again on the right one.
  Object.keys(offside).forEach(function (k) { delete looked[k]; });

  function save() {
    try { sessionStorage.setItem(KEY + '-seen', JSON.stringify(looked)); } catch (e) {}
  }

  function note(msg) {
    var el = document.getElementById('ckerr');
    if (el) { el.textContent = msg || ''; el.classList.toggle('on', !!msg); }
  }

  // One settle, one row in screen_attested. Optimistic: the tick appears at
  // once and is taken back if the write is refused, because a checklist that
  // waits on the network between every field is a checklist nobody finishes.
  function record(cell, value, previous) {
    var body = new FormData();
    // The name this page shows. The server refuses the settle if the browser
    // has been set to someone else since, in another tab. See screen_attest.
    body.append('as', VERIFIER);
    body.append('field', cell);
    body.append('state', value === 'ok' ? 'confirmed' : 'not_in_source');
    // What is vouched for is what is in the field now, including a correction
    // just typed. See attest() in pipeline/screen.py for why.
    var inp = fieldInput(cell), sent = inp ? inp.value.trim() : null;
    if (sent !== null) { body.append('value', sent); }
    function undo(msg) { state[cell] = previous; paint(); note(msg); }
    fetch('/screen/' + ROW_ID + '/attest', { method: 'POST', body: body })
      .then(function (r) {
        // A refused POST answers with the read-only page, not JSON.
        return r.json().catch(function () {
          return { ok: false, error: 'The server answered with a page instead '
                                   + 'of an answer, so nothing was recorded.' };
        });
      })
      .then(function (d) {
        if (d && d.ok) { delete stale[cell]; delete offside[cell]; delete absenceTick[cell]; delete other[cell]; settledValue[cell] = sent; note(''); paint(); return; }
        undo((d && d.error) || 'That confirmation was not recorded.');
      })
      .catch(function () {
        undo('Could not reach the server, so nothing was recorded.');
      });
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

  // The input a strip settles, and whether what is in it records that nothing
  // was stated. ABSENT comes from the server, so the page and the writer agree
  // on which values those are. parentElement, not closest('div'): the strip is
  // itself a div, and closest() starts at the element it is called on.
  function fieldInput(cell) {
    var s = strips.filter(function (x) { return x.dataset.cell === cell; })[0];
    return (s && s.parentElement) ? s.parentElement.querySelector('[name="' + cell + '"]') : null;
  }
  function isAbsent(cell) {
    var inp = fieldInput(cell);
    return !!inp && ABSENT.indexOf(inp.value.trim().toLowerCase()) >= 0;
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
      // proof of reading, and not what makes the stored record defensible --
      // match_count is. But it stops a project being ticked clean without the
      // document ever moving.
      // A field that records nothing was stated has nothing to confirm, and
      // "not in this source" is what settles it. See ABSENCE_VALUES.
      var absent = isAbsent(cell);
      ok.disabled = !VERIFIER || absent || ((v !== 'ok') && !looked[cell]);
      no.disabled = !VERIFIER;
      lbl.textContent = v === 'ok' ? 'confirmed'
                      : v === 'no' ? 'not in this source'
                      : stale[cell] ? 'changed since it was confirmed \u2014 look again'
                      : offside[cell] ? 'confirmed against the wrong source — read '
                                        + ((ATTESTED[cell] || {}).wanted || 'its own source')
                      : absenceTick[cell] ? 'confirmed, but it holds no value: settle it as not in this source'
                      : other[cell] ? ('settled by ' + other[cell].by + ' on '
                                       + String(other[cell].when || '').slice(0, 10)
                                       + (other[cell].stale ? ', since changed' : '')
                                       + ' — settle it yourself')
                      : (absent && looked[cell]) ? 'holds no value, so there is nothing to confirm' : looked[cell] ? 'opened, not settled' : '';
      s.classList.toggle('is-other', !v && !!other[cell]);
      s.classList.toggle('is-stale', !v && !!stale[cell]);
      s.classList.toggle('is-offside', !v && !!offside[cell]);
      s.classList.toggle('is-absence', !v && !!absenceTick[cell]);
      // "Not in this source" is the one moment the model check earns its
      // place: the pane is a string matcher, so a value phrased differently
      // looks identical to one that is genuinely absent. Reading the page to
      // tell those apart, and finding a URL that does carry it, is the part
      // that is slow by hand.
      var ask = s.querySelector('.ck-ask');
      if (ask) { ask.hidden = (v !== 'no'); }
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
    // No un-settling. The record is append-only and there is no row meaning
    // "never mind": you can change a confirmation to the other answer, and you
    // cannot take it back to blank.
    if (state[cell] === value) { return; }
    var previous = state[cell] || '';
    state[cell] = value;
    save(); paint();
    record(cell, value, previous);
    if (walking && state[cell]) {
      var next = unsettled()[0];
      if (next) { openField(next); }
      else { walking = false; if (go2 && !go2.hidden) { go2.focus(); } }
    }
  }

  strips.forEach(function (s) {
    var cell = s.dataset.cell;
    // Editing a field after settling it means the settle is about a value the
    // project will no longer publish -- how seven of the first eight corrections
    // went out unconfirmed. So the tick comes off and the field needs another
    // look. Repaint on every keystroke either way, so "confirmed" greys out the
    // moment a field is cleared to nothing.
    var inp = s.parentElement && s.parentElement.querySelector('[name="' + cell + '"]');
    if (inp) {
      inp.addEventListener('input', function () {
        if (state[cell] && inp.value.trim() !== (settledValue[cell] || '')) {
          state[cell] = ''; stale[cell] = 1; delete looked[cell]; save();
        }
        paint();
      });
    }
    var go = s.querySelector('a.ck-go');
    if (go) {
      go.addEventListener('click', function () {
        looked[cell] = 1; save(); setTimeout(paint, 0);
      });
    }
    s.querySelector('.ck-ok').onclick = function () { settle(s, 'ok'); };
    s.querySelector('.ck-no').onclick = function () { settle(s, 'no'); };
    var ask = s.querySelector('.ck-ask');
    if (ask) {
      ask.onclick = function () {
        var box = document.getElementById('agentbox');
        var form = document.getElementById('agentform');
        if (!box || !form) { return; }
        box.open = true;
        // This field only. The escalation is about one value, and a picker
        // arriving pre-ticked with six is the panel this replaced.
        Array.prototype.forEach.call(
          form.querySelectorAll('input[name="cell"]'), function (b) {
            b.checked = (b.value === cell);
          });
        form.submit();
        box.scrollIntoView({ block: 'start' });
      };
    }
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

# The per-field checklist. This used to be a scratchpad in the browser and is
# now a record: every settle writes a row to `screen_attested` naming the
# person, the field, the page they had open, and how many times the pane found
# the value on it.
#
# Two jobs, and the second is why it is stored. Verifying a project means
# settling six fields against a document, and across the remaining projects that
# is near a thousand confirmations -- sessionStorage lost your place the moment
# the tab closed, and the database does not. And the published claim stops being
# "a human looked at this project" and becomes "this person settled these fields
# against these pages on these dates", which is a much harder thing to wave away.
#
# Three states, not a checkbox, because a binary tick throws away the most
# interesting answer. A field the page does not carry is not "unchecked": its
# absence is the finding. So: not looked at, confirmed here, or not in this
# source.
#
# `confirm` stays disabled until the field has been opened in the pane. That is
# not proof of reading, and it is not what makes the record defensible --
# `match_count` is. A confirmation stored against a page where the value never
# appeared is visible to anyone reading the table later.
#
# One list, in pipeline/screen.py, because which fields need a human eye is a
# methodology fact and the writer has to reject anything else.
CHECKLIST_CELLS = screen.ATTESTABLE_FIELDS


# Who is verifying. A cookie, the same mechanism the list toggles already use at
# shared.py, so the choice is made once per browser instead of typed once per
# project. The value is only ever an address from settings.VERIFIERS: a list and
# not a text box, because a box accepts "asdf@asdf.com" as readily as a real
# address and a typed identity is exactly as forgeable as a typed name. Remove
# someone from the list and their cookie stops being accepted, which is the
# behaviour you want.
#
# The choice is made with a button on the inspect page and nowhere else. It used
# to be a link, ?verifier=, and a link can be bookmarked or shared: opening one
# changed the name on every settle after it. The page also offered a one-click
# "switch to" the other address. 142 of Lucas's settles were stored under
# Ashwin's address through one of those two paths.
#
# The cookie was renamed with that change, so every browser chooses again with
# the new buttons. The last settles from Lucas's browser were stored under
# Ashwin's address, so it may still have been set that way.
VERIFIER_COOKIE = "pvp_verifier_v2"
VERIFIER_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def current_verifier(request: Request) -> str:
    """The address this browser is verifying as, or "" if none is chosen."""
    v = (request.cookies.get(VERIFIER_COOKIE) or "").strip()
    return v if settings.may_verify(v) else ""


def _verifier_bar(request: Request, who: str, screen_id: int) -> str:
    """Who you are attesting as, and how to say otherwise.

    Always rendered, never collapsed. A record naming the wrong person is worse
    than no record, and the only defence available here is that the name is in
    front of you the whole time you are ticking.

    With nobody chosen it asks, and each address is a button that says "I am".
    It used to read "You are verifying as:" followed by both addresses, which
    made the first one look already chosen. Once someone is chosen, "Not you?"
    clears the choice and asks again. Nothing switches straight to another
    address, so no single click moves one person's settles to someone else.
    """
    people = settings.verifiers()
    if not people:
        return ('<p class="ckwho none">No verifiers are configured, so nothing '
                f'can be confirmed. Add an address to <code>VERIFIERS</code> in '
                f'<code>{esc(settings.where("VERIFIERS"))}</code>.</p>')
    action = f"/screen/{screen_id}/verifier"
    if not who:
        picks = " ".join(
            f'<button type="submit" name="verifier" value="{esc(p)}" '
            f'class="ckwho-pick">I am {esc(p)}</button>'
            for p in people)
        return (f'<form class="ckwho none" method="post" action="{action}">'
                f'<b>Who is verifying?</b> Confirmations are stored under the '
                f'address you choose and published with the data. {picks}</form>')
    return (f'<form class="ckwho" method="post" action="{action}">'
            f'Verifying as <b class="ckwho-name">{esc(who)}</b>. Every confirmation '
            f'below is stored under this address and published with the data. '
            f'<button type="submit" name="verifier" value="" class="ckwho-pick">'
            f'Not you?</button></form>')


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
            f'<button type="button" class="ck-ask" hidden>ask a model where it is</button>'
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
def screen_inspect(request: Request, screen_id: int, msg: Optional[str] = None,
                   verified: Optional[int] = None, answer: Optional[int] = None):
    conn = _conn()
    try:
        screen_row = screen.get_extracted(conn, screen_id)
        if screen_row is None:
            return _page("Screen inspect", "<p>No project with that id.</p>", "Not found")
        # Checks on other projects, because this is the page a reviewer is on
        # when one comes back: verifying lands on the next project, not the list.
        waiting = [i for i in agent_pane.waiting_checks(conn, current_verifier(request))
                   if i["id"] != screen_id]
        # For a published project `r` is the published record under the Screen
        # id, so the boxes, the pane and the checklist all show what went out.
        # The check panel keeps `screen_row`: it reports what the checker tested,
        # and that was the Screen record.
        r, published_id = screen.review_view(conn, screen_id)
        chk = screen.latest_check(conn, screen_id)
        attested = screen.attestation_state(conn, screen_id)
        # The tab each confirmation was settled against. attestation_state
        # reports the URL and not the tab, and the tab is the dependable half:
        # the stored URL is the pane's *final* url, so a promise page read
        # through the Wayback Machine does not match the cited link at all.
        settled_tabs = {f: a["tab_index"]
                        for f, a in screen.attestations(conn, screen_id).items()}
    finally:
        conn.close()

    # A confirmation settled against a page the cell may not be proved from is
    # not a confirmation. Showing it as a plain green tick is how
    # `promised_first_output` came to read as confirmed on the strength of a
    # status page that happens to mention the year -- the promise side of the
    # row is the only place that cell can be settled. See evidence.SOURCES_FOR.
    for f, a in attested.items():
        a["offside"] = (a["state"] == "confirmed"
                        and evidence.settled_off_side(r, settled_tabs.get(f), f))
        a["wanted"] = evidence.wanted_sources(f)

    # Who is attesting is this browser's choice, made with a button on this page
    # and nowhere else. A ?verifier= in the address is ignored. See _verifier_bar.
    who = current_verifier(request)

    # ?verified= is set by the redirect after a verification, which lands on the
    # next project in the queue rather than the record just published. The
    # record stays one click away, because the moment after publishing is when
    # a mistake is cheapest to catch.
    just_published = ""
    if verified:
        just_published = (
            f'<p><small>Published. <a href="/verify/{int(verified)}">Open the '
            f'record you just verified</a>, or carry on: this is the next project '
            f'by capital.</small></p>')

    verdict = chk["result_status"] if chk else None
    promotable = verdict in ("PASS", "CLEAN")

    # Published: the fields hold the published values and are read-only here.
    # A published record has one place to be corrected, its Verify page, which
    # logs every change with a reason. A second edit path on this page would be
    # a second set of rules to keep in step with the first.
    lock = " readonly" if published_id else ""
    sel_lock = " disabled" if published_id else ""
    if published_id:
        intro = (f'<p>Published as <a href="/verify/{published_id}">Verify '
                 f'#{published_id}</a>. These are the published values, so settling '
                 f'a field vouches for what went out. To correct one, edit it on its '
                 f'<a href="/verify/{published_id}">Verify page</a>, then settle it '
                 f'again here.</p>')
    else:
        intro = ("<p>Confirm every field against the pane, then verify. Any field "
                 "you change is applied to the project's new Verify row and logged "
                 "in <code>verify_edits</code>. <b>lag_years / slip_years and the "
                 "<code>*_dt</code> columns are derived</b> from the date strings "
                 "and recompute when you edit a date.</p>")

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
        <select name="sector"{sel_lock}>{options}</select></div>""")
        if c == "status":
            return (f"""<div><label>status{hint_html}</label>
        {status_select(r.get("status"), sel_lock)}</div>""")
        # No picker on a published project: there is nothing to choose here.
        picker = "" if published_id else date_kind_select(c, r[c])
        return (f"""<div><label>{esc(c)}{hint_html}</label>
        {picker}<input type="text" name="{esc(c)}" value="{esc(r[c])}"{lock}>{_check_strip(c, r['id'], ftabs)}</div>""")

    ftabs = evidence.field_tabs(r)
    fields = "".join(_field(c) for c in INSPECT_COLUMNS)
    # measured_spans, not the bare number: a slip of exactly -1.0 is both a real
    # measurement (one year early) and the "to be completed" sentinel.
    spans = measured_spans(r)
    derived = "".join(
        f"""<div><label>{esc(c)} <small>(derived)</small></label>
        <input type="text" value="{esc(lag_label(r[c], spans[c]))}" disabled></div>"""
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

    if published_id:
        # Already published, so the verify button could only fail on a second
        # write. Corrections go to the Verify page, as `intro` says.
        promote_controls = (f'<p class="tiernote">Already published. '
                            f'<a href="/verify/{published_id}">Open Verify '
                            f'#{published_id}</a> to correct a value.</p>')
    elif promotable:
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
  {_check_panel(screen_row, chk)}
</div>

<div class="review">
  <div class="doccol">
    {evidence.pane_html("screen", r["id"], r)}
  </div>

  <div class="formcol">
    <div class="card">
      {agent_pane.checks_html(waiting, f"/screen/checks?exclude={r['id']}")}
      {just_published}
      {intro}
      <div class="cktally-wrap"><span id="cktally" class="cktally"></span>
      <span id="ckleft" class="cktally-left"></span><span id="ckerr" class="ckerr"></span><button type="button" id="ckwalk" class="ckwalk" hidden>start checking \u2192</button><button type="button" id="ckgo" class="ckgo" hidden>verify this project \u2192</button></div>
      {_verifier_bar(request, who, r['id'])}
      <p><small>Settling a field records who confirmed it, which page was open,
      and how many times the pane found the value on that page. It does not gate
      verifying.</small></p>
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

<script>var ROW_ID = {r['id']};
var ATTESTED = {json.dumps(attested)};
var VERIFIER = {json.dumps(who)};
var ABSENT = {json.dumps(sorted(screen.ABSENCE_VALUES))};</script>
<script>{_CHECKLIST_JS}</script>
<script>{DATEKIND_JS}</script>
<script>{agent_pane.CHECKS_JS}</script>

<details class="byhand" id="agentbox"{" open" if answer else ""}>
<summary>Ask a model to read the cited pages</summary>
<div class="card">{agent_pane.picker_html("screen", r["id"], r)}</div>
</details>
"""
    return _page(f"Screen #{screen_id}", body, msg, wide=True)


@router.post("/screen/{screen_id}/verifier")
async def screen_choose_verifier(screen_id: int, request: Request):
    """Choose who this browser is verifying as, or clear the choice.

    Only the buttons in the verifier line post here. The answer is a redirect
    back to the project, so reloading the page never chooses again.
    """
    form = await request.form()
    chosen = (form.get("verifier") or "").strip()
    back = f"/screen/{screen_id}/inspect"
    if chosen and not settings.may_verify(chosen):
        msg = "That address is not on the verifier list, so nothing was changed."
        return RedirectResponse(f"{back}?msg={urlquote(msg)}", status_code=303)
    resp = RedirectResponse(back, status_code=303)
    if chosen:
        resp.set_cookie(VERIFIER_COOKIE, chosen,
                        max_age=VERIFIER_COOKIE_MAX_AGE, samesite="lax")
    else:
        resp.delete_cookie(VERIFIER_COOKIE)
    return resp


@router.post("/screen/{screen_id}/attest")
async def screen_attest(screen_id: int, request: Request):
    """Record one field settled by one person. Answers JSON, so the page stays put.

    The URL and the hit count are not taken from the browser. They come from
    what the pane itself resolved and counted when it last rendered this field,
    which is the only end of that exchange the server can trust: the frame is
    sandboxed with no same-origin access, so nothing can be read out of it, and
    anything the page sent instead would be a value the reviewer could set.
    A miss stores an unknown count rather than a guessed one.
    """
    def bad(msg: str, code: int = 400):
        return JSONResponse({"ok": False, "error": msg}, status_code=code)

    if READ_ONLY:
        return bad("$TRACKER_READONLY is set, so nothing can be written.")
    who = current_verifier(request)
    if not who:
        return bad("Choose who you are verifying as before confirming a field.")

    form = await request.form()
    # The page sends the name it shows. If this browser has been set to someone
    # else since, in another tab, the settle would be stored under a name that
    # page never showed, so it is refused.
    shown_as = (form.get("as") or "").strip()
    if shown_as != who:
        return bad(f"This page shows {shown_as or 'no one'} verifying, but this "
                   f"browser has since been set to {who}. Reload the page. "
                   f"Nothing was recorded.")
    field = (form.get("field") or "").strip()
    state = (form.get("state") or "").strip()
    shown = evidence.last_shown("screen", screen_id, field) or {}

    conn = _conn()
    try:
        # Confirming says "this page carries this value", so it has to be a page
        # the cell may be proved from: the promise side of the row for a promised
        # value, the produced side for a produced one. Until now the pane's own
        # partitioning was the only thing keeping those apart, and it is not a
        # guard -- it holds during a walk and not after a restart, when the shown
        # map is empty and the browser still remembers opening the field.
        # Tabs come from the record the pane showed: the published one, once
        # there is one. See screen.review_view.
        src = screen.review_view(conn, screen_id)[0]
        if state == "confirmed" and evidence.settled_off_side(src, shown.get("tab"), field):
            return bad(f"{field} can only be confirmed against its own source. "
                       f"Open “find in source” and read the "
                       f"{evidence.wanted_sources(field)} tab — nothing was recorded.")
        # The value comes from the page because it is the reviewer's claim, not
        # evidence: a correction they typed is what they are vouching for.
        screen.attest(conn, screen_id, field, state, who,
                      source_url=shown.get("url"), tab_index=shown.get("tab"),
                      match_count=shown.get("count"), value=form.get("value"))
        settled = screen.attestation_state(conn, screen_id).get(field, {})
    except ValueError as exc:
        return bad(str(exc))
    finally:
        conn.close()
    return JSONResponse({"ok": True, "field": field, **settled})


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

        # Check the corrections BEFORE publishing. Promotion writes a faithful
        # copy and the corrections are applied after it, so a refusal at that
        # point would leave the project live with the value being replaced.
        if changes:
            verify.prepare_changes(src, changes)

        # Promote a faithful copy first (the human gate), then apply the human's
        # edits through the ordinary verify.edit path so each one lands in verify_edits.
        gid = verify.promote(conn, screen_id, verification_tier=tier)
        if changes:
            verify.edit(conn, gid, changes, edit_description=desc)
        msg = f"Verified {src['project']} at tier {tier}."
        if changes:
            msg += f" Recorded {len(changes)} change(s) in verify_edits."

        # Land on the next project in the queue, not on the record just made.
        # There used to be no next step: after each verification the way back to
        # work was the Screen list, so the walk followed that list's order, and
        # it was id order. review_queue is the one definition of "next" -- the
        # dashboard, `status` and `review` all ask it -- so this cannot drift.
        nxt = screen.review_queue(conn)["ready"]
        if nxt:
            dest = f"/screen/{nxt[0]['id']}/inspect?verified={gid}"
        else:
            msg += " That was the last project waiting."
            dest = f"/verify/{gid}"
    except verify.CorrectionRefused as e:
        msg = f"Not verified, and nothing was published. {e}"
        dest = f"/screen/{screen_id}/inspect"
    except verify.PromotionBlocked as e:
        msg = f"Promotion blocked: {e}"
        dest = f"/screen/{screen_id}/inspect"
    except Exception as e:
        msg = f"Error: {e}"
        dest = f"/screen/{screen_id}/inspect"
    finally:
        conn.close()
    # dest may already carry a query string, and msg now names a project, which
    # can hold characters a URL cannot carry raw. So: encode it, and join with
    # "&" when there is already a "?".
    sep = "&" if "?" in dest else "?"
    return RedirectResponse(f"{dest}{sep}msg={urlquote(msg)}", status_code=303)

