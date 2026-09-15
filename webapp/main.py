"""
main.py -- the web interface to the pipeline: a small server-rendered app.

Python (FastAPI) + plain HTML forms. No build step, no framework JS -- every
action is a normal form POST that redirects back, so it works with the browser
alone.

What it is FOR is the review workflow: opening a Screen row beside its sources,
correcting cells, and promoting it to Verify with the reason recorded in
`verify_edits`. Reading the data does not need it -- the CSV exports and
`tracker.py verify-list` both do that with nothing installed.

This module creates the app and mounts the three stage modules onto it. The
pages themselves live in source.py, screen.py and verify.py; the stylesheet,
page skeleton and formatting helpers in shared.py.

Run it from the repository root:

    pip install -r pipeline/requirements.txt
    python3 tracker.py webapp --reload
    # then open http://localhost:8100

The `webapp` command runs uvicorn in process, so it does not depend on the `uvicorn` script
being on PATH. By hand it is `python3 -m uvicorn webapp.main:app --port 8100`.
"""

from __future__ import annotations

from typing import Optional

import html
import json
import os
import sys

# webapp/ -> tracker/, so `pipeline` imports resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import HTMLResponse, RedirectResponse  # noqa: E402

from pipeline import orchestrate as orch, quality, screen  # noqa: E402
from pipeline.db import (  # noqa: E402
    connect, init_db, is_read_only, set_active_db, table_counts,
)

from webapp import (  # noqa: E402
    agent as agent_pane,
    evidence as evidence_pane,
    screen as screen_pages,
    source as source_pages,
    verify as verify_pages,
)
from webapp.shared import PROJECT_NAME, _conn, _page, esc  # noqa: E402

app = FastAPI(title=f"{PROJECT_NAME} — Source → Verify Pipeline")


@app.on_event("startup")
def _startup():
    conn = connect()
    init_db(conn)          # no-ops on a legacy-vocabulary database
    conn.close()


@app.middleware("http")
async def _guard_read_only(request: Request, call_next):
    """A legacy database is browsable but not writable, so refuse every POST
    except the one that switches database."""
    if request.method == "POST" and request.url.path != "/db" and is_read_only():
        return _page(
            "Read-only",
            '<div class="card"><p>This database uses the old '
            "<b>Bronze/Silver/Gold</b> vocabulary, so it is open for reading "
            "only — writing to it would modify a file kept as the pre-rename "
            "original.</p><p>Switch to a <b>Source/Screen/Verify</b> database "
            "above to make changes.</p></div>",
            "Read-only database — nothing was written",
        )
    return await call_next(request)


def _conn():
    return connect()


@app.post("/db")
async def switch_db(request: Request):
    """Point the running app at another database. Process-local: nothing on disk
    is touched, and the choice lasts until the server restarts."""
    form = await request.form()
    path = form.get("path", "")
    set_active_db(path)
    conn = connect()
    try:
        init_db(conn)      # brings a writable database up to schema; no-op on legacy
    finally:
        conn.close()
    return RedirectResponse(f"/?msg={html.escape(f'Switched to {path}')}", status_code=303)



# --------------------------------------------------------------------------- #
# Dashboard                                                                    #
# --------------------------------------------------------------------------- #

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, msg: Optional[str] = None):
    conn = _conn()
    try:
        c = table_counts(conn)
        q = screen.review_queue(conn)
        qual = quality.measure(conn)
        # The latest verdict per Screen row. screen_check keeps history, so
        # counting the table would double-count any row checked twice.
        verdicts = dict(conn.execute("""
            SELECT k.result_status, COUNT(*) FROM screen_check k
            JOIN (SELECT screen_extracted_id, MAX(id) AS id FROM screen_check
                  GROUP BY screen_extracted_id) last ON last.id = k.id
            GROUP BY 1""").fetchall())
    finally:
        conn.close()

    n_ready, n_blocked = len(q["ready"]), len(q["blocked"])

    # Five tiles, one per TABLE, is not what the pipeline is. screen_check is
    # exactly one row per screen_extracted row by construction, so "173" next
    # to "173" printed the same number twice; verify_edits is an audit log, not
    # a stage. Five equal boxes in a row also stated that these are five equal
    # steps, when the methodology says three layers with the last one a
    # human-only gate.
    #
    # So: three stages, the drop between them visible, and every number given
    # something to be measured against. 173/173/173/0/0 could equally have
    # meant "healthy and waiting on you" or "broke after Screen", and nothing
    # on the page distinguished those.
    n_eligible = n_ready + c["verify_verified"]
    lost_at_screen = c["source_collected"] - c["screen_extracted"]

    def _gate(n, label):
        return (f'<div class="gate"><span class="gate-n">{n}</span>'
                f'<span class="gate-l">{label}</span></div>')

    verdict_bits = " \u00b7 ".join(
        f'<span class="verdict-{k}">{verdicts.get(k, 0)} {k.lower()}</span>'
        for k in ("CLEAN", "PASS", "FAIL") if verdicts.get(k))

    cta = ""
    if n_ready:
        cta = (f'<a href="/screen?show=pending" class="stage-cta">'
               f'<button class="primary" type="button">{n_ready} waiting on you \u2192</button></a>')

    body = f"""
<div class="pipe">

  <div class="stage">
    <div class="stage-name">Source</div>
    <div class="stage-body"><span class="stage-n">{c['source_collected']}</span>
      leads collected <span class="stage-note">agentic</span></div>
    <a href="/source" class="stage-link">inspect \u2192</a>
  </div>

  {_gate(c['screen_extracted'], 'extracted' + (f', {lost_at_screen} not extracted' if lost_at_screen else ''))}

  <div class="stage">
    <div class="stage-name">Screen</div>
    <div class="stage-body"><span class="stage-n">{c['screen_extracted']}</span>
      projects checked <span class="stage-note">agentic</span>
      <div class="stage-sub">{verdict_bits or 'no checks yet'}</div></div>
    <a href="/screen" class="stage-link">inspect \u2192</a>
  </div>

  {_gate(n_eligible, 'eligible to publish' + (f', {n_blocked} blocked by a failing check' if n_blocked else ''))}

  <div class="stage stage-end">
    <div class="stage-name">Verify</div>
    <div class="stage-body"><span class="stage-n">{c['verify_verified']}</span>
      of {n_eligible} published <span class="stage-note">human gate</span>
      <div class="stage-sub"><a href="/verify">the Tracker</a> \u00b7
        {c['verify_edits']} edit(s) logged</div></div>
    {cta}
  </div>

</div>
"""

    if not n_ready and not n_blocked:
        if not c["verify_verified"]:
            body += """
<div class="card"><h2>Nothing here yet</h2>
<p>This database is empty. Collect some projects first — from a terminal, in
<code>tracker/</code>:</p>
<pre>N=5 bash collect/all.sh</pre></div>"""
        else:
            body += f"""
<div class="card"><h2>Nothing waiting for you</h2>
<p>All {c['verify_verified']} project(s) have been through the human gate.
<a href="/verify">See the Tracker</a>.</p></div>"""
        return _page("Dashboard", body, msg)

    ready_bit = ""
    if n_ready:
        top = q["ready"][0]
        ready_bit = f"""
<p>Largest capital first, so wherever you stop, the Tracker above that point
is complete. First up: <a href="/screen/{top['id']}/inspect"><b>{esc(top['project'])}</b></a>.</p>
<p><small>Or work the same queue in a terminal:
<code>python3 tracker.py review</code></small></p>"""

    blocked_bit = ""
    if n_blocked:
        links = ", ".join(f'<a href="/screen/{r["id"]}/inspect">#{r["id"]}</a>'
                          for r in q["blocked"][:8])
        blocked_bit = f"""
<p><small>{n_blocked} project(s) cannot be published until a failing check is fixed:
{links}.</small></p>"""

    body += f'<div class="card">{ready_bit}{blocked_bit}</div>'
    body += _quality_card(qual)
    return _page("Dashboard", body, msg)


def _quality_card(m: dict) -> str:
    """Five bars, and no blended score.

    The counts on the tiles above cannot say whether the Tracker is any good --
    "23 screened" is a finished Tracker or a backlog depending on facts they do
    not carry. These five say it. They are shown side by side rather than
    combined because a single number invites an argument about the weights, and
    a referee will ask what is in it.
    """
    if not m["total"]:
        return ""
    rows = ""
    for b in m["bars"]:
        pct = b["pct"]
        # Red below a third, amber below two thirds, green above. The point is
        # to draw the eye to the row that needs work, not to grade anything.
        hue = "#d9534f" if pct < 34 else ("#d9a441" if pct < 67 else "#5cb85c")
        rows += f"""
  <div class="qrow">
    <div class="qlabel">{esc(b['label'])}<br><small>{esc(b['why'])}</small></div>
    <div class="qtrack"><div class="qfill" style="width:{pct:.1f}%;background:{hue}"></div></div>
    <div class="qnum">{b['n']}/{b['total']}<br><small>{pct:.0f}%</small></div>
  </div>"""

    f = m["flags"]
    n_prov, n_subst = len(f["provenance"]), len(f["substantive"])
    return f"""
<div class="card"><h2>Can this Tracker carry the claim?</h2>
{rows}
<p style="margin-top:1rem"><b>Open questions.</b> {n_prov + n_subst} of {m['total']}
projects carry an unresolved flag, of two very different kinds:</p>
<ul>
  <li><b>{n_prov}</b> — a cited page could not be read (403, 404, timeout, video-only).
      An access failure: fetch it better and it goes away.</li>
  <li><b>{n_subst}</b> — the sources disagree, or do not say. A fact about the world;
      only a person can settle it.</li>
</ul>
<p><small>Counting these together is why every project looked flagged and the warning
carried no signal. Same numbers in a terminal:
<code>python3 tracker.py quality</code></small></p></div>"""





# --------------------------------------------------------------------------- #
# The stage pages, defined in their own modules                               #
# --------------------------------------------------------------------------- #

app.include_router(source_pages.router)
app.include_router(screen_pages.router)
app.include_router(verify_pages.router)

# The two review panes. Not stages: they render no rows of their own and write
# nothing. Each serves a standalone page that the review screen embeds in an
# iframe, so a slow fetch or a half-minute model call never blocks the form the
# reviewer is typing into.
app.include_router(evidence_pane.router)
app.include_router(agent_pane.router)
