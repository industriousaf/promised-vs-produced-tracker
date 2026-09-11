"""
agent.py -- the agentic-check pane: "does the cited page actually say this?"

The deterministic check (`screen_check`, which is `pipeline/schema.py`) reads
SHAPE. It knows whether `announced` is a real `YYYY-MM`, whether the row clears
the size floor, whether `status_source` is URL-shaped. It cannot open the link.
So a date can be well-formed, pass CLEAN, and be a date its cited article never
printed — and the check will never say a word about it.

This pane asks the other question. It takes the cells the reviewer picks, hands
them to a model with web tools together with the links that are supposed to
prove them, and shows the reply: confirmed with a quote and where to find it, or
not confirmed with a URL where the value actually lives. It writes nothing. No
promotion depends on it, and no verdict of its is stored — a machine judgment on
a provenance question would become a thing people cite, and the whole design of
this Scoreboard is that only a person's reading promotes a row.

Two flavours, the same two the collection stages have:

  * with an `ANTHROPIC_API_KEY`, it runs the check here and renders the answer;
  * without one, it renders the same prompt for pasting into Claude Code.

The pane is served as its own page and embedded in an `<iframe>` on the review
screen, so a check that takes half a minute never blocks the form or costs the
reviewer their unsaved edits.

The check itself runs on a thread and its answer is cached on disk -- both in
`agent_cache`, with the reasoning there. What that buys the page: a reload
during a check re-attaches to it rather than abandoning it, and a question
already answered is answered instantly rather than bought twice.
"""

from __future__ import annotations

import os
import re
import sys

# webapp/ -> scoreboard/, so `pipeline` imports resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json  # noqa: E402
import time  # noqa: E402

from fastapi import APIRouter, Query  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

from pipeline import llm, screen, verify  # noqa: E402
from pipeline import settings as models  # noqa: E402
from pipeline.db import db_path  # noqa: E402
from pipeline.llm import LLMUnavailable, VERIFY_TARGETS  # noqa: E402

from webapp import agent_cache  # noqa: E402
from webapp.shared import _conn, esc  # noqa: E402

router = APIRouter()

# The cells worth asking about, in the order the review works through them. The
# two date cells lead because they are what this Scoreboard measures and what the
# checker is least able to help with: capital and jobs are at least bounded by
# the size floor, but a wrong date passes every deterministic rule there is.
CHECKABLE = [
    ("promised_first_output", "promised first output"),
    ("actual_first_output", "actual first output"),
    ("announced", "announced"),
    ("promised_capital_usd", "capital"),
    ("promised_jobs", "jobs"),
    ("current_status", "current status"),
]

_PANE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font: 14px/1.6 system-ui, sans-serif; margin: 0; padding: .8rem 1rem 2rem;
       max-width: 46rem; }
h3 { font-size: .95rem; margin: 1.2rem 0 .3rem; }
p, li { margin: .45rem 0; }
a { color: #2b6cb0; overflow-wrap: break-word; }
code { background: #8882; padding: 0 .25rem; border-radius: 3px;
       font-size: .9em; }
blockquote { margin: .5rem 0 .5rem .6rem; padding-left: .7rem;
             border-left: 3px solid #8886; color: inherit; opacity: .9; }
.v { font-weight: 700; }
.v-ok { color: #2f855a; } .v-no { color: #c0392b; } .v-hm { color: #b7791f; }
.muted { color: #8889; font-size: .85rem; }
textarea { width: 100%; font: 12px/1.45 ui-monospace, monospace; padding: .4rem;
           border: 1px solid #8887; border-radius: 6px; background: transparent;
           color: inherit; }
.note { border: 1px solid #8886; border-radius: 8px; padding: .6rem .8rem;
        margin: .6rem 0; }

/* The in-flight cue. It has to read as "this is still happening" from across
   the room, because the thing it is telling you is that a reload did NOT throw
   your check away -- the pulse is doing the same job as the label. */
.run { display: flex; align-items: center; gap: .6rem; }
.dot { width: .8rem; height: .8rem; border-radius: 50%; background: #2b6cb0;
       flex: none; animation: pulse 1.1s ease-in-out infinite; }
@keyframes pulse { 0%, 100% { opacity: .25; transform: scale(.8); }
                   50% { opacity: 1; transform: scale(1.15); } }
@media (prefers-reduced-motion: reduce) { .dot { animation: none; opacity: .9; } }
.bar { height: 3px; background: #8883; border-radius: 2px; overflow: hidden;
       margin: .7rem 0; }
.bar i { display: block; height: 100%; width: 35%; background: #2b6cb0;
         border-radius: 2px; animation: slide 1.8s ease-in-out infinite; }
@keyframes slide { 0% { margin-left: -35%; } 100% { margin-left: 100%; } }
@media (prefers-reduced-motion: reduce) { .bar i { animation: none; width: 100%; } }
.cached { border-top: 1px solid #8884; margin-top: 1rem; padding-top: .5rem;
          display: flex; justify-content: space-between; gap: .8rem;
          flex-wrap: wrap; align-items: baseline; }
"""


def _js_str(s: str) -> str:
    """A Python string as a JavaScript string literal, safe inside <script>.

    `</script>` in a URL would end the block early, and a quote would end the
    literal. json.dumps handles the quoting; the two replacements handle the
    parser, which does not care that it is inside a string.
    """
    return (json.dumps(s).replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026"))


def _pane(body: str, title: str = "Agentic check",
          poll: int | None = None, poll_url: str = "") -> HTMLResponse:
    """The pane document. `poll` re-requests `poll_url` after N seconds.

    Both a `<meta refresh>` and a timer, because neither is reliable alone here:
    the iframe is sandboxed, which blocks the meta refresh in some browsers, and
    scripting can be off, which blocks the timer. Whichever one runs, it loads a
    URL that carries the ticked cells in its query string -- so the reload
    re-attaches to the job already running rather than starting a second one.

    It polls `poll_url` rather than reloading the current address, and the
    difference matters: the current address may be the one carrying `again=1`,
    and polling *that* would re-drop the cached answer every three seconds and
    delete the very answer the job was about to write.
    """
    dest = poll_url or ""
    meta = (f'<meta http-equiv="refresh" content="{poll}; url={esc(dest)}">'
            if poll and dest else (f'<meta http-equiv="refresh" content="{poll}">'
                                   if poll else ""))
    timer = (f"<script>setTimeout(function(){{location.replace({_js_str(dest)});}},"
             f"{poll * 1000});</script>" if poll and dest else
             (f"<script>setTimeout(function(){{location.reload();}},"
              f"{poll * 1000});</script>" if poll else ""))
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{meta}
<title>{esc(title)}</title><style>{_PANE_CSS}</style></head><body>{body}{timer}</body></html>"""
    )


def _ago(seconds: float) -> str:
    """'40s' / '3m ago'-style, for a cache line and an elapsed counter."""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s" if s < 600 else f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


# --------------------------------------------------------------------------- #
# Rendering the reply                                                          #
# --------------------------------------------------------------------------- #

_URL_RE = re.compile(r"https?://[^\s<>\")\]]+")
_VERDICTS = (
    ("CONFIRMED", "v-ok"),
    ("NOT ON THIS PAGE", "v-no"),
    ("CONTRADICTED", "v-no"),
    ("NOT ON THE PAGE", "v-no"),
)


def _inline(text: str) -> str:
    """Escape, then put back the three bits of Markdown the reply actually uses.

    A full Markdown library would be a dependency for one panel. The model is
    told to answer in short prose with a bold verdict, quotes and URLs, so those
    three are what this handles — and everything it does not handle stays
    escaped and visible rather than disappearing into markup.
    """
    out = esc(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = _URL_RE.sub(
        lambda m: f'<a href="{m.group(0)}" target="_blank" rel="noopener">{m.group(0)}</a>',
        out)
    for word, cls in _VERDICTS:
        out = out.replace(f"<b>{word}</b>", f'<span class="v {cls}">{word}</span>')
    return out


def render_reply(text: str) -> str:
    """The model's Markdown-ish answer as safe HTML."""
    blocks, buf, in_list = [], [], False

    def flush():
        nonlocal buf, in_list
        if buf:
            blocks.append(("ul" if in_list else "p", buf))
            buf = []
        in_list = False

    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            flush()
        elif s.startswith("#"):
            flush()
            blocks.append(("h", [s.lstrip("#").strip()]))
        elif s.startswith((">",)):
            flush()
            blocks.append(("q", [s.lstrip(">").strip()]))
        elif re.match(r"^[-*•]\s+", s):
            if not in_list:
                flush()
                in_list = True
            buf.append(re.sub(r"^[-*•]\s+", "", s))
        else:
            if in_list:
                flush()
            buf.append(s)
    flush()

    out = []
    for kind, lines in blocks:
        if kind == "h":
            out.append(f"<h3>{_inline(lines[0])}</h3>")
        elif kind == "q":
            out.append(f"<blockquote>{_inline(' '.join(lines))}</blockquote>")
        elif kind == "ul":
            out.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in lines) + "</ul>")
        else:
            out.append(f"<p>{_inline(' '.join(lines))}</p>")
    return "".join(out) or "<p class='muted'>(the model returned nothing)</p>"


# --------------------------------------------------------------------------- #
# The picker, for embedding in a review screen                                 #
# --------------------------------------------------------------------------- #

# The form is a plain GET into the pane's iframe: the reviewer's half-typed
# corrections in the form beside it survive, and a check that takes half a
# minute happens off to one side instead of across the whole screen. The
# checkboxes ARE the query -- repeated `cell=` parameters -- so the panel works
# with scripting off; the script below only changes the button's label while the
# answer is in flight.
_PICKER_JS = """
(function () {
  var f = document.getElementById('agentform');
  if (!f) { return; }
  f.addEventListener('submit', function () {
    document.getElementById('agentgo').textContent = 'Asking\\u2026 (up to a minute)';
  });
})();
"""


def picker_html(stage: str, row_id: int, row, default: tuple[str, ...] = ()) -> str:
    """The "check this against the sources" panel: pick cells, get an answer.

    Ticked by default are the two dates, because that is what this is for: a
    date is the one thing on the row that can be wrong in every way that matters
    and still pass every deterministic rule there is. The other cells are on the
    list because someone will want them, not because they are the point.

    Unless this row has been asked about before, in which case those cells are
    ticked instead and the pane opens on that question. This is what makes a
    reload harmless. The pane is an iframe, and an iframe's `src` is rebuilt
    from nothing every time the parent page renders -- so a bare `src` would
    show the "tick some cells" placeholder over the top of a check that was
    still running, which is exactly what happened when the promote form bounced
    back asking for a reason. Carrying the last question in the `src` means the
    reload lands back on the job in flight and picks up the cue where it left
    off.
    """
    remembered = agent_cache.last_cells(stage, row_id)
    default = tuple(remembered) or default or ("promised_first_output",
                                               "actual_first_output")
    boxes = "".join(
        f'<label><input type="checkbox" name="cell" value="{esc(col)}"'
        f'{" checked" if col in default else ""}> <code>{esc(col)}</code>'
        f'</label>'
        for col, _label in CHECKABLE
    )
    src = f"/agent/{esc(stage)}/{row_id}"
    if remembered:
        src += "?" + "&".join(f"cell={esc(c)}" for c in remembered)
    return f"""
<form id="agentform" method="get" action="/agent/{esc(stage)}/{row_id}"
      target="agentpane">
  <p style="margin:.2rem 0 0"><b>Check this project against its own links.</b>
  The check reads the record's <i>shape</i> and never opens a page;
  this opens them. Pick the cells you doubt — it quotes the sentence that proves
  each one, or, when the page does not carry it, goes and finds a URL that
  does.</p>
  <div class="cells">{boxes}</div>
  <p><button id="agentgo" class="primary" type="submit">Ask Claude
    ({esc(models.agent())})</button>
  <small style="color:#8889"> — reads the cited pages and searches only if it
  has to. It keeps running if you reload, and an answer already given comes
  straight back. Nothing is written to the database.</small></p>
</form>
<iframe class="pane short" name="agentpane" title="agentic check"
  src="{src}"
  sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"></iframe>
<script>{_PICKER_JS}</script>"""


# --------------------------------------------------------------------------- #
# The route                                                                    #
# --------------------------------------------------------------------------- #

def _row_for(stage: str, row_id: int):
    conn = _conn()
    try:
        if stage == "verify":
            return verify.get_verified(conn, row_id)
        return screen.get_extracted(conn, row_id)
    finally:
        conn.close()


def _no_key_pane(row, picked: list[str], why: str) -> HTMLResponse:
    """The Claude Code flavour: the prompt the API path would have sent."""
    prompt = llm.render_verify_prompt(row, picked)
    return _pane(f"""
<div class="note"><p><b>No API key here — run it in Claude Code instead.</b></p>
<p class="muted">{esc(why)}</p>
<p>This is the same two-flavour arrangement as the collection stages: the prompt
below is exactly what the API path would have sent. Copy it into a web-search
capable assistant, read the answer there, and come back to the form. To stop
copying it, put <code>ANTHROPIC_API_KEY</code> in a <code>config.env</code>
beside <code>scoreboard.py</code>.</p>
<textarea rows="16" onclick="this.select()">{esc(prompt)}</textarea></div>""")


def _answer_pane(reply: str, picked: list[str], model: str, foot: str) -> HTMLResponse:
    names = ", ".join(f"<code>{esc(f)}</code>" for f in picked)
    return _pane(f"""
<p class="muted">{esc(model)} read this project's cited pages and was asked about
{names}. It is <b>advice, not a verdict</b> — nothing here is written to the
Scoreboard, and only your own reading of the sources verifies the project.</p>
{render_reply(reply)}
<p class="muted">Answers come from pages the model opened in this reply. Check any
quote that decides a cell — that is the whole job of this screen.</p>
<div class="cached">{foot}</div>""")


@router.get("/agent/{stage}/{row_id}", response_class=HTMLResponse)
def agent_pane(stage: str, row_id: int, cell: list[str] = Query(default=[]),
               again: int = 0):
    """Check the named cells of one row against that row's own sources.

    A GET, and read-only in every sense: it touches no table, so it is also the
    one AI call that still works on a read-only (pre-rename) database. The cells
    arrive as repeated `cell=` parameters, straight off the checkboxes, so the
    panel needs no scripting to work.

    The request never does the work. It resolves the question to a cache key and
    then reports on it: served from disk, already running, just started, or
    failed. That is what lets the page be reloaded at any moment -- including by
    the promote form bouncing back for an edit reason -- without losing a check.
    `again=1` drops the cached answer first, which is the "ask again" link.
    """
    stage = "verify" if stage == "verify" else "screen"
    row = _row_for(stage, row_id)
    if row is None:
        return _pane("<p>No project with that id.</p>")

    order = [c for c, _ in CHECKABLE]
    picked = sorted({f for f in cell if f in VERIFY_TARGETS}, key=order.index)
    if not picked:
        return _pane(
            '<p class="muted">Tick the cells you want checked against their '
            "links, then press <b>Ask Claude</b>. It opens each cited page and "
            "says whether the page really carries that value — and if it does "
            "not, where the value actually is.</p>"
        )

    model = models.agent()
    key = agent_cache.fingerprint(stage, row_id, picked, row, model,
                                  db=str(db_path()))
    qs = "&".join(f"cell={esc(c)}" for c in picked)
    here = f"/agent/{esc(stage)}/{row_id}?{qs}"

    # "Ask again": drop both the stored answer and the finished job, so the
    # question is genuinely re-asked rather than re-read. A job still RUNNING is
    # left alone -- there is nothing to re-ask yet, and killing it to start an
    # identical one would only pay twice for the same answer.
    if again:
        agent_cache.forget(stage, row_id, key)
        agent_cache.drop(key, running_too=False)

    # 1. Answered before, and nothing the answer depends on has changed since.
    if not again:
        hit = agent_cache.read(stage, row_id, key)
        if hit:
            age = _ago(time.time() - float(hit.get("answered_at") or 0))
            foot = (f'<small class="muted">Answered {esc(age)} ago in '
                    f'{esc(str(hit.get("seconds", "?")))}s, kept on disk — this '
                    f'cost no API call. Edit any cell it names and it is asked '
                    f'again automatically.</small>'
                    f'<small><a href="{here}&again=1">Ask again →</a></small>')
            return _answer_pane(hit["reply"], picked, str(hit.get("model", model)),
                                foot)

    # 2. No key: render the prompt instead of starting a job that cannot run.
    #    Checked here rather than in the thread so the common "I never set one
    #    up" case answers instantly instead of pulsing for a second first.
    if not os.getenv("ANTHROPIC_API_KEY"):
        return _no_key_pane(row, picked,
                            "ANTHROPIC_API_KEY is not set in this server's "
                            "environment.")

    # 3. Attach to the job -- starting it only if it is not already going.
    job = agent_cache.start(
        stage, row_id, key, picked, model,
        work=lambda: llm.run_verify_check(row, picked),
    )

    if job.running:
        names = ", ".join(f"<code>{esc(f)}</code>" for f in picked)
        return _pane(f"""
<div class="run"><div class="dot"></div>
  <div><b>Reading the cited pages…</b>
  <div class="muted">{esc(model)} on {names} · {esc(_ago(job.elapsed))} elapsed ·
  usually 20–60s</div></div></div>
<div class="bar"><i></i></div>
<p class="muted">This is running on the server, not in this panel. Reload the
page, fix a cell, or go and do something else — the check carries on and the
answer appears here when it lands. It will also be kept, so the same question
does not cost a second call.</p>""", poll=3, poll_url=here)

    if job.error_kind == "unavailable":
        return _no_key_pane(row, picked, job.error or "the API is not configured")
    if job.error:
        return _pane(
            f'<p class="v v-no">The check failed after {esc(_ago(job.elapsed))}.</p>'
            f'<p class="muted">{esc(job.error)}</p>'
            f'<p><a href="{here}&again=1">Try again →</a></p>')

    # Reached only when the finished job's answer did NOT make it to disk -- the
    # cache read at the top of this function serves every normal completion. So
    # this must not tell the reviewer the answer was kept, because it was not:
    # `write` swallows OSError by design, and a cache that cannot be written is
    # a cache that is not there.
    foot = (f'<small class="muted">Answered just now in {esc(_ago(job.elapsed))}. '
            f'It could not be written to <code>outputs/agent_cache/</code>, so '
            f'this one is not kept — reloading will ask again.</small>'
            f'<small><a href="{here}&again=1">Ask again →</a></small>')
    return _answer_pane(job.reply or "", picked, model, foot)
