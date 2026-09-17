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
import time
from urllib.parse import quote as urlquote, urlsplit

# webapp/ -> tracker/, so `pipeline` imports resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse  # noqa: E402

from pipeline import orchestrate as orch, quality, screen, set_config_env, settings  # noqa: E402
from pipeline.db import (  # noqa: E402
    READ_ONLY, connect, init_db, is_read_only, set_active_db, table_counts,
)

from webapp import (  # noqa: E402
    agent as agent_pane,
    evidence as evidence_pane,
    page_cache,
    screen as screen_pages,
    source as source_pages,
    verify as verify_pages,
)
from webapp.shared import PROJECT_NAME, _conn, _page, esc, preload_status  # noqa: E402

app = FastAPI(title=f"{PROJECT_NAME} — Source → Verify Pipeline")


@app.on_event("startup")
def _startup():
    conn = connect()
    init_db(conn)          # no-ops on a legacy-vocabulary database
    conn.close()
    # On a thread, so the app is usable at once. A page opened before the
    # preload reaches it downloads as it always did.
    if settings.preload_articles():
        evidence_pane.start_preload()


# POSTs that write nothing to the database, so a read-only one does not stop them.
_NOT_DATABASE_WRITES = ("/db", "/preload", "/preload/run")


@app.middleware("http")
async def _guard_read_only(request: Request, call_next):
    """A legacy database is browsable but not writable, so refuse every POST
    except the ones that do not write to it: switching database, and the preload."""
    if (request.method == "POST" and request.url.path not in _NOT_DATABASE_WRITES
            and is_read_only()):
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
# Preloading the cited pages                                                   #
# --------------------------------------------------------------------------- #

def _preload_answer(request: Request, ok: bool, message: str):
    """JSON for the footer's script, or a redirect back for a browser without it."""
    if "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 400)
    back = urlsplit(request.headers.get("referer") or "/").path or "/"
    return RedirectResponse(f"{back}?msg={urlquote(message)}", status_code=303)


@app.post("/preload")
async def preload_setting(request: Request):
    """The Preload articles checkbox, for this machine. See webapp/page_cache.py."""
    form = await request.form()
    on = form.get("on") == "1"
    if READ_ONLY:
        return _preload_answer(request, False, "$TRACKER_READONLY is set, so the "
                                               "setting was not saved.")
    try:
        set_config_env("PRELOAD_ARTICLES", "1" if on else "0")
    except OSError as exc:
        return _preload_answer(request, False, f"config.env could not be written: {exc}")
    if not on:
        return _preload_answer(request, True, "Preload off for this machine. Pages "
                                              "already saved are still used.")
    return _preload_answer(request, True, preload_status(evidence_pane.start_preload()))


@app.post("/preload/run")
async def preload_run(request: Request):
    """Run the preload now, whatever the checkbox says. It retries every page
    that failed, which is the thing to do after turning on a VPN."""
    return _preload_answer(request, True, preload_status(evidence_pane.start_preload()))


@app.get("/pages", response_class=HTMLResponse)
def saved_pages():
    """What the last preload did, page by page, with the failures first."""
    run = page_cache.last_run()
    if run is None:
        return _page("Saved pages", """<div class="card"><p>No preload has run since
the app started. Tick <b>Preload articles</b> at the foot of any page, or press
<b>Run now</b> there.</p></div>""")

    def outcome(p: dict) -> tuple[int, str]:
        if p.get("ok") is None:
            return 3, "waiting"
        if not p["ok"]:
            return 0, evidence_pane.explain_fetch_error(p, p["host"])[0]
        where = " from the Wayback Machine" if p.get("via") == "wayback" else ""
        if (p.get("words") or 0) < evidence_pane.NEARLY_EMPTY:
            return 1, f"saved{where}, but nearly empty"
        return 2, f"saved{where}"

    rows = []
    for _i, p in sorted(enumerate(run.pages), key=lambda ip: (outcome(ip[1])[0], ip[0])):
        rank, what = outcome(p)
        if rank == 0 and p.get("error"):
            what = f"{esc(what)}<br><small>{esc(p['error'])}</small>"
        else:
            what = esc(what)
        cited = "<br>".join(
            f'<a href="/screen/{c["id"]}/inspect">#{c["id"]} {esc(c["project"])}</a> '
            f'<small>({esc(c["tab"])})</small>' for c in p["projects"])
        words = "" if p.get("words") is None else str(p["words"])
        rows.append(
            f'<tr><td>{what}</td><td><a href="{esc(p["url"])}" target="_blank" '
            f'rel="noopener noreferrer">{esc(p["host"])} ↗</a></td><td>{cited}</td>'
            f'<td>{words}</td><td>{esc(evidence_pane.saved_when(p))}</td></tr>')

    minutes = int((time.time() - run.started_at) // 60)
    started = "just now" if minutes < 1 else f"{minutes} min ago"
    refresh = ("<script>setTimeout(function () { location.reload(); }, 4000);</script>"
               if run.running else "")
    body = f"""<div class="card">
<p><b>{esc(preload_status(run))}</b>. Started {started}.</p>
<p><small>Every page cited by the projects waiting for review, largest capital
first, then by the projects with a field to settle again. A page that loaded is
kept for a week in <code>scratch/pages/</code>. A page that failed is tried again
on the next run, so after turning on a VPN, press <b>Run now</b> at the foot of
the page.</small></p></div>
<table><tr><th>what happened</th><th>page</th><th>cited by</th><th>words</th>
<th>downloaded</th></tr>{''.join(rows)}</table>{refresh}"""
    return _page("Saved pages", body)



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
