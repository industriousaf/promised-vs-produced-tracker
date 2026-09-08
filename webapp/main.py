"""
main.py -- the web interface to the pipeline: a small server-rendered app.

Python (FastAPI) + plain HTML forms. No build step, no framework JS -- every
action is a normal form POST that redirects back, so it works with the browser
alone.

What it is FOR is the review workflow: opening a Screen row beside its sources,
correcting cells, and promoting it to Verify with the reason recorded in
`verify_edits`. Reading the data does not need it -- the CSV exports and
`scoreboard.py verify-list` both do that with nothing installed.

This module creates the app and mounts the three stage modules onto it. The
pages themselves live in source.py, screen.py and verify.py; the stylesheet,
page skeleton and formatting helpers in shared.py.

Run it from the scoreboard directory:

    pip install -r pipeline/requirements.txt
    python3 scoreboard.py webapp --reload
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

# webapp/ -> scoreboard/, so `pipeline` imports resolve.
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
from webapp.shared import _conn, _page, esc  # noqa: E402

app = FastAPI(title="Promised vs. Produced — Source → Verify Pipeline")


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
    finally:
        conn.close()

    n_ready, n_blocked = len(q["ready"]), len(q["blocked"])

    # The five tiles were the whole page: five numbers, no verb, and nothing
    # saying which of them was waiting on the person reading it. They link now,
    # and the block below says what to do -- because "23 screened" is a triumph
    # or a backlog depending on a fact the tiles do not show.
    body = f"""
<div class="stages">
  <a href="/source"><div><div class="n">{c['source_collected']}</div>Source<br><small>collected</small></div></a>
  <a href="/screen"><div><div class="n">{c['screen_extracted']}</div>Screen<br><small>extracted</small></div></a>
  <a href="/screen"><div><div class="n">{c['screen_check']}</div>Screen<br><small>checks</small></div></a>
  <a href="/verify"><div><div class="n">{c['verify_verified']}</div>Verify<br><small>verified</small></div></a>
  <a href="/verify"><div><div class="n">{c['verify_edits']}</div>Verify<br><small>edits</small></div></a>
</div>
"""

    if not n_ready and not n_blocked:
        if not c["verify_verified"]:
            body += """
<div class="card"><h2>Nothing here yet</h2>
<p>This database is empty. Collect some projects first — from a terminal, in
<code>scoreboard/</code>:</p>
<pre>N=5 bash collect/all.sh</pre></div>"""
        else:
            body += f"""
<div class="card"><h2>Nothing waiting for you</h2>
<p>All {c['verify_verified']} project(s) have been through the human gate.
<a href="/verify">See the published rows</a>.</p></div>"""
        return _page("Dashboard", body, msg)

    ready_bit = ""
    if n_ready:
        top = q["ready"][0]
        ready_bit = f"""
<p><b>{n_ready} row(s) are waiting for you</b> to check them against their sources.
Nothing reaches the published Scoreboard until you do — Verify is a human gate, by
design, and no amount of collecting will move these along.</p>
<p><a href="/screen?show=pending"><button class="primary" type="button">
Start reviewing — {n_ready} waiting →</button></a></p>
<p><small>Largest capital first. First up: {esc(top['project'])}.
Or work the same queue in a terminal with <code>python3 scoreboard.py review</code>.</small></p>"""

    blocked_bit = ""
    if n_blocked:
        links = ", ".join(f'<a href="/screen/{r["id"]}/inspect">#{r["id"]}</a>'
                          for r in q["blocked"][:8])
        blocked_bit = f"""
<p><small>{n_blocked} row(s) cannot be published until a failing check is fixed:
{links}.</small></p>"""

    body += f'<div class="card"><h2>Your move</h2>{ready_bit}{blocked_bit}</div>'
    body += _quality_card(qual)
    return _page("Dashboard", body, msg)


def _quality_card(m: dict) -> str:
    """Five bars, and no blended score.

    The counts on the tiles above cannot say whether the Scoreboard is any good --
    "23 screened" is a finished Scoreboard or a backlog depending on facts they do
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
<div class="card"><h2>Can this Scoreboard carry the claim?</h2>
{rows}
<p style="margin-top:1rem"><b>Open questions.</b> {n_prov + n_subst} of {m['total']}
rows carry an unresolved flag, of two very different kinds:</p>
<ul>
  <li><b>{n_prov}</b> — a cited page could not be read (403, 404, timeout, video-only).
      An access failure: fetch it better and it goes away.</li>
  <li><b>{n_subst}</b> — the sources disagree, or do not say. A fact about the world;
      only a person can settle it.</li>
</ul>
<p><small>Counting these together is why every row looked flagged and the warning
carried no signal. Same numbers in a terminal:
<code>python3 scoreboard.py quality</code></small></p></div>"""





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
