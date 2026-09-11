"""
shared.py -- the pieces every page needs: the stylesheet, the page skeleton, the
database picker, and the small formatting helpers.

No routes and no app live here, so every page module can import it without a
circular reference. The dependency runs one way: shared <- the page modules <-
main.
"""

from __future__ import annotations

import base64
import html
from pathlib import Path
import json
import os
import sys

# webapp/ -> scoreboard/, so `pipeline` imports resolve
# whether this is run as a package or by path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.responses import HTMLResponse  # noqa: E402

from pipeline import source, screen, verify, orchestrate as orch, llm  # noqa: E402
from pipeline.db import (  # noqa: E402
    DEFAULT_DB, READ_ONLY, connect, db_path, discover_databases, init_db,
    is_read_only, set_active_db, table_counts,
)
from pipeline.dates import lag_label  # noqa: E402
from pipeline.schema_check import (  # noqa: E402
    V0_COLUMNS,
    DERIVED_DATE_COLUMNS,
    RAW_DATE_COLUMNS,
    all_sectors,
)
from pipeline.llm import LLMUnavailable  # noqa: E402


def _conn():
    return connect()


# --------------------------------------------------------------------------- #
# HTML helpers                                                                 #
# --------------------------------------------------------------------------- #

# The wordmark face, inlined rather than served. Bagnard is 7.2 KB, this
# interface mounts no static routes, and a data URI keeps the whole page
# self-contained and working with no network. Read at import, not per request.
#
# Copyright (c) 2015 Sebastien Sanfilippo, SIL Open Font License v1.1. The
# licence travels with the font in webapp/assets/OFL.txt and the OFL requires
# it to stay there. See webapp/assets/README.md.
def _bagnard_face() -> str:
    path = Path(__file__).resolve().parent / "assets" / "Bagnard.woff2"
    try:
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        # A clone missing the font still runs; the wordmark falls back to the
        # serif stack rather than the page failing to render.
        return ""
    return ("@font-face{font-family:'Bagnard';"
            f"src:url(data:font/woff2;base64,{b64}) format('woff2');"
            "font-weight:400;font-style:normal;font-display:swap}")


_BAGNARD = _bagnard_face()

# Google Fonts carries the three IAF faces. Every rule below names a real
# fallback stack, so an offline run degrades to Georgia / system-ui / the
# platform mono rather than breaking: this tool is used on a laptop that is
# not always online, and the pipeline itself has no network dependency.
_FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    'family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&'
    'family=IBM+Plex+Sans:wght@400;500&'
    'family=IBM+Plex+Mono:wght@400;500;600&display=swap">'
)

_CSS = """
/* IndustriousAF design system, restrained. Tokens from design-system.html
   Section 03, type roles from Section 04, geometry from Section 14.
   Two anchors are deliberately present rather than decorative:
   teal leads every surface (the nav band), and the mono spec-label register
   carries real metadata (row ids, timestamps, criteria, URLs, verdicts).
   The Bagnard wordmark is NOT here: it is a self-hosted woff2 this repo does
   not ship, so the nav uses the mono lockup the brand book's own masthead
   uses. That is a documented secondary treatment, not the primary mark. */
:root {
  --font-serif: 'Source Serif 4', Georgia, 'Times New Roman', serif;
  --font-sans:  'IBM Plex Sans', system-ui, -apple-system, sans-serif;
  --font-mono:  'IBM Plex Mono', ui-monospace, 'Courier New', monospace;

  --teal:        #1F4E52;
  --teal-dark:   #163C40;
  --terracotta:  #D06B45;

  --ground:      #DCE4D5;   /* paper-green: the V6.1 primary ground */
  --ground-card: #E4EADD;
  --ground-deep: #CAD3C0;
  --surface-dark:#252322;
  --cream:       #F4F1EA;

  --type-1: #2E2B2A;
  --type-2: #4F4B47;
  --type-3: #6E6962;
  --rule:      #B8C0B0;
  --rule-soft: #CFD5C5;

  --teal-plate:  #1F4E52;   /* the wordmark plate is always teal, any surface */
  --success: #2A6B3A;   /* CLEAN */
  --warning: #8B7437;   /* PASS  */
  --danger:  #8B3A24;   /* FAIL  */
}

/* Light only, deliberately. The paper-green ground with teal leading IS the
   IndustriousAF surface; there is no documented dark counterpart, and a dark
   charcoal page makes the accent the only colour in view, which Section 14
   names as the machine tell. To run dark anyway, re-add a
   @media (prefers-color-scheme: dark) block overriding --ground, --ground-card,
   --ground-deep, --type-1/2/3 and --rule; leave --teal-plate alone, because
   Section 01 fixes the wordmark plate to teal on every surface. */

* { box-sizing: border-box; }

/* The ground goes on html, not just body: body is a 1000px measure, so a
   different colour up here paints two vertical bars down the margins. The
   terracotta overscroll the design system documents is a full-bleed site
   surface; on a centred reading column it reads as decoration, and Section 03
   is explicit that terracotta's rarity is what makes it work. The accent
   appears once per page, in the wordmark's AF. */
html { background: var(--ground); }

body { font-family: var(--font-serif); font-size: 16px; line-height: 1.55;
       color: var(--type-1); background: var(--ground);
       margin: 0; padding: 0 0 4rem; }

/* The reading measure. It used to live on body, which meant the teal band was
   also capped at 1000px and floated with two gutters beside it on a wide
   screen instead of running edge to edge. Body is now full width and .wrap
   carries the measure, which is also what the design system's own container
   does (Section 06). */
.wrap { max-width: 1000px; margin-inline: auto; padding: 0 1.5rem; }
body.wide .wrap { max-width: 1560px; }

/* ---- the teal band: anchor 1, teal above the fold on every page --------- */
.band { background: var(--teal-plate); color: var(--cream);
        margin: 0 0 1.5rem; padding: .85rem 0; }
.band .wrap { display: flex; align-items: center; gap: 1.25rem;
        flex-wrap: wrap; }
.band nav { display: flex; gap: 1.1rem; margin-left: auto; }
.band nav a { font-family: var(--font-mono); font-size: 11px;
        letter-spacing: .16em; text-transform: uppercase; text-decoration: none;
        color: #B0ACA5; transition: color .15s; }
.band nav a:hover { color: var(--cream); }

/* A page lede: one sentence saying what this screen is for, in the serif,
   under the h1. */
.pagelede { font-family: var(--font-serif); font-size: 1rem; color: var(--type-2);
    max-width: 46rem; margin: 0 0 1.6rem; }

/* Routes nobody uses, kept but folded away. */
.byhand { margin-top: 2.5rem; border-top: 0.5px solid var(--rule-soft);
    padding-top: 1rem; }
.byhand > summary { cursor: pointer; font-family: var(--font-mono); font-size: 11px;
    letter-spacing: .16em; text-transform: uppercase; color: var(--type-3); }
.byhand > summary:hover { color: var(--teal); }
.byhand h3 { font-family: var(--font-mono); font-size: 11px; letter-spacing: .14em;
    text-transform: uppercase; color: var(--type-3); font-weight: 500;
    margin: 1.6rem 0 .5rem; }
.byhand h3:first-of-type { margin-top: .4rem; }

/* ---- the pipeline, as a funnel ----------------------------------------- */
/* Three stages stacked, with the gate between them shown as its own row, so
   the drop from one stage to the next is a thing you can see rather than
   arithmetic you have to do. The old five tiles sat side by side, which said
   these were five equal steps; they were five tables, and two of them were not
   stages at all. */
.pipe { margin: 1.4rem 0 2rem; }
.stage { display: flex; align-items: baseline; gap: 1rem; flex-wrap: wrap;
    border: 0.5px solid var(--rule); border-radius: 0;
    background: var(--ground-card); padding: .9rem 1.1rem; }
/* Verify is where the Scoreboard actually exists, so it gets the weight. */
.stage-end { border-color: var(--teal); }
.stage-name { font-family: var(--font-mono); font-size: 11px; letter-spacing: .2em;
    text-transform: uppercase; color: var(--teal); min-width: 5.5rem; }
.stage-body { flex: 1; min-width: 14rem; }
.stage-n { font-family: var(--font-mono); font-size: 1.5rem; font-weight: 500;
    font-variant-numeric: tabular-nums; color: var(--type-1); margin-right: .35rem; }
.stage-note { font-family: var(--font-mono); font-size: 10px; letter-spacing: .12em;
    text-transform: uppercase; color: var(--type-3); margin-left: .5rem; }
.stage-sub { font-family: var(--font-sans); font-size: .82rem; color: var(--type-2);
    margin-top: .25rem; }
.stage-link { font-family: var(--font-mono); font-size: 10px; letter-spacing: .12em;
    text-transform: uppercase; color: var(--type-3); text-decoration: none; }
.stage-link:hover { color: var(--teal); }

/* The gate. A short rule with the number that survived it. */
.gate { display: flex; align-items: baseline; gap: .55rem; padding: .4rem 0 .4rem 1.1rem;
    margin-left: 1.1rem; border-left: 0.5px solid var(--rule); }
.gate-n { font-family: var(--font-mono); font-size: .95rem; font-weight: 500;
    font-variant-numeric: tabular-nums; color: var(--type-2); }
.gate-l { font-family: var(--font-mono); font-size: 10px; letter-spacing: .12em;
    text-transform: uppercase; color: var(--type-3); }
.stage-cta { text-decoration: none; }

/* ---- the database, in the band ----------------------------------------- */
/* Quiet while you are on the canonical file. Loud the moment you are not,
   because promoting rows into a scratch copy believing it is the real one is
   the one mistake this interface cannot undo. */
.dbstatus { font-family: var(--font-mono); font-size: 10px; letter-spacing: .12em;
    color: #8FA5A2; margin-left: 1.25rem; white-space: nowrap; }
.dbstatus-alt { color: var(--cream); }
.dbflag { margin-left: .5rem; padding: .1rem .4rem; letter-spacing: .1em; }
.dbflag.ro  { background: var(--danger); color: var(--cream); }
.dbflag.alt { background: var(--terracotta); color: var(--cream); }

/* ---- the footer: the switcher lives here ------------------------------- */
/* Switching is a two-or-three-times-a-year action. It had the best space on
   every page; it now has the worst, which is the correct amount. */
.pagefoot { border-top: 0.5px solid var(--rule); margin-top: 3rem;
    padding: 1.1rem 0 2rem; }
.dbswitch { display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; }
.dbswitch label { margin: 0; }
.dbswitch select { font-family: var(--font-mono); font-size: 11px; padding: .2rem;
    width: auto; max-width: 30rem; }
.footpath { font-family: var(--font-mono); font-size: 10px; color: var(--type-3);
    margin: .5rem 0 0; word-break: break-all; }

/* ---- the wordmark: design system Section 01, SM tier ------------------- */
/* Two lines at one cap-height. Line 1 reads the brand; line 2 reveals the
   second reading, INDUSTRIO-USA-F. The double rule is two rings with a
   field-coloured gap between them, which is why it is a layered box-shadow
   and not a border. Section 14 permits the wordmark's structural rings and
   bans elevation shadows; the SM tier carries rings only. */
.iaf-logo { display: inline-block; background: var(--teal-plate);
    padding: 8px 12px 6px; position: relative;
    box-shadow: 0 0 0 1px var(--teal-plate), 0 0 0 2px var(--cream),
                0 0 0 3px var(--teal-plate), 0 0 0 4px var(--cream); }
.iaf-logo-line { font-family: 'Bagnard', Georgia, 'Times New Roman', serif;
    font-weight: 400; font-size: 17px; line-height: .95; letter-spacing: 0;
    display: block; text-align: center; white-space: nowrap; }
.iaf-logo-line + .iaf-logo-line { margin-top: 2px; }
.iaf-logo .cream  { color: var(--cream); }
.iaf-logo .accent { color: var(--terracotta); }
/* The plate is always teal, whatever the surface around it (Section 01), so
   it does not follow the dark-mode token. */
.masthead { margin: .2rem 0 1.6rem; }

h1 { font-family: var(--font-serif); font-size: 28px; line-height: 1.2;
     font-weight: 400; letter-spacing: -.01em; margin: 0 0 .15rem; }
/* The stage chain as a mono spec label rather than part of the headline.
   Section 14 bans the eyebrow-over-headline-over-buttons hero stack; this
   sits BELOW the headline and states the pipeline, which is metadata. */
.subtitle { font-family: var(--font-mono); font-size: 11px;
     letter-spacing: .18em; text-transform: uppercase; color: var(--type-3);
     margin: 0 0 1.1rem; }
/* An h2 that opens a card does not get the section rule: the card's own
   border is already the separation, and the stacked margins left a band of
   empty paper above every heading. */
.card > h2:first-child { margin-top: 0; padding-top: 0; border-top: none; }
h2 { font-family: var(--font-mono); font-size: 12px; letter-spacing: .2em;
     text-transform: uppercase; color: var(--teal); font-weight: 500;
     margin-top: 2.2rem; padding-top: .9rem;
     border-top: 0.5px solid var(--rule-soft); }
a { color: inherit; }

/* h2 does two jobs in this app. Section labels ("Review queue", "Rows (173
   of 173)") are mono eyebrows; a row title names a specific project and stays
   serif, because setting a plant's name in uppercase mono turns the subject of
   the page into a field label. */
h2.rowtitle { font-family: var(--font-serif); font-size: 22px; line-height: 1.25;
     font-weight: 400; letter-spacing: -.01em; text-transform: none;
     color: var(--type-1); border-top: none; padding-top: 0; margin-top: 1.6rem; }
h2.rowtitle small { font-family: var(--font-sans); font-size: .7em;
     color: var(--type-3); }

/* ---- the mono spec-label register: anchor 3 ---------------------------- */
.stages { display: grid; grid-template-columns: repeat(5, 1fr); gap: .5rem;
         text-align: center; margin: 1rem 0; }
.stages div { border: 0.5px solid var(--rule); border-radius: 0; padding: .6rem;
         background: var(--ground-card); }
.stages .n { font-family: var(--font-mono); font-size: 1.5rem; font-weight: 500;
         font-variant-numeric: tabular-nums; color: var(--type-1); }
.stages a { color: inherit; text-decoration: none; display: block; }
.stages a div { transition: border-color .15s, background .15s; }
.stages a:hover div { border-color: var(--teal); background: var(--ground-deep); }

.card { border: 0.5px solid var(--rule); border-radius: 0; padding: .8rem 1rem;
        margin: .6rem 0; background: var(--ground-card); }
.card small { color: var(--type-3); }

.qrow { display: grid; grid-template-columns: 15rem 1fr 5rem; gap: .75rem;
        align-items: center; margin: .5rem 0; }
.qtrack { background: var(--ground-deep); border-radius: 0; height: 1.1rem;
        overflow: hidden; }
.qfill { height: 100%; border-radius: 0; }
.qnum { text-align: right; font-family: var(--font-mono);
        font-variant-numeric: tabular-nums; font-size: .85rem; }
@media (max-width: 640px) { .qrow { grid-template-columns: 1fr; } }

form.inline { display: inline; }
label { display: block; margin: .4rem 0 .1rem; font-family: var(--font-mono);
        font-size: 10px; letter-spacing: .14em; text-transform: uppercase;
        color: var(--type-3); }
input[type=text], textarea, select { width: 100%; padding: .35rem .5rem;
    border: 0.5px solid var(--rule); border-radius: 0; background: var(--ground-card);
    color: inherit; font-family: var(--font-sans); font-size: .9rem; }
textarea { font-family: var(--font-mono); font-size: .82rem; }
button { padding: .35rem .9rem; border: 0.5px solid var(--rule); border-radius: 0;
    background: var(--ground-card); color: inherit; cursor: pointer;
    font-family: var(--font-mono); font-size: 11px; letter-spacing: .12em;
    text-transform: uppercase; transition: background .15s, border-color .15s; }
button:hover { border-color: var(--teal); }
button.primary { background: var(--teal); color: var(--cream); border-color: var(--teal); }
button.primary:hover { background: var(--teal-dark); border-color: var(--teal-dark); }

.msg { background: var(--ground-card); border: 0.5px solid var(--teal);
       padding: .6rem 1rem; border-radius: 0; margin: 1rem 0;
       font-family: var(--font-sans); font-size: .9rem; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: .3rem .8rem; }

.verdict-FAIL  { color: var(--danger);  font-family: var(--font-mono);
                 font-size: .9em; font-weight: 600; letter-spacing: .06em; }
.verdict-PASS  { color: var(--warning); font-family: var(--font-mono);
                 font-size: .9em; font-weight: 600; letter-spacing: .06em; }
.verdict-CLEAN { color: var(--success); font-family: var(--font-mono);
                 font-size: .9em; font-weight: 600; letter-spacing: .06em; }

/* ---- the pane's loading state ------------------------------------------ */
/* The pane is a server-side fetch of somebody else's site, so between click
   and paint there were several seconds of blank white with nothing said. The
   veil sits over the frame and lifts on its load event. */
.panewrap { position: relative; }
.paneload { position: absolute; inset: 0; z-index: 1; display: flex;
    flex-direction: column; justify-content: center; gap: .5rem;
    padding: 2rem; background: var(--ground-card);
    border: 0.5px solid var(--rule); }
.paneload.done { display: none; }
.paneload-l { font-family: var(--font-mono); font-size: 11px; letter-spacing: .18em;
    text-transform: uppercase; color: var(--teal); }
.paneload-h { font-family: var(--font-mono); font-size: 1rem; color: var(--type-1);
    word-break: break-all; }
.paneload-n { font-family: var(--font-serif); font-size: .85rem; color: var(--type-3);
    max-width: 34rem; }

/* ---- the per-cell checklist (scratchpad, browser-only) ------------------ */
.ck { display: flex; align-items: center; gap: .3rem; flex-wrap: wrap;
    margin: .25rem 0 .1rem; }
.ck a.ck-go, .ck button { font-family: var(--font-mono); font-size: 9px; letter-spacing: .1em;
    text-transform: uppercase; padding: .12rem .4rem; border-radius: 0;
    border: 0.5px solid var(--rule); background: transparent; color: var(--type-3); }
.ck a.ck-go { text-decoration: none; display: inline-block; }
.ck a.ck-go:hover, .ck button:hover:not(:disabled) { border-color: var(--teal); color: var(--type-1); }
.ck-nolink { font-family: var(--font-mono); font-size: 9px; letter-spacing: .08em;
    text-transform: uppercase; color: var(--type-3); opacity: .7; }
.ck button:disabled { opacity: .35; cursor: default; }
.ck .ck-ok.on { background: var(--success); border-color: var(--success); color: var(--cream); }
.ck .ck-no.on { background: var(--warning); border-color: var(--warning); color: var(--cream); }
.ck-state { font-family: var(--font-mono); font-size: 9px; letter-spacing: .08em;
    color: var(--type-3); margin-left: .15rem; }
.ck.is-ok .ck-state { color: var(--success); }
.ck.is-no .ck-state { color: var(--warning); }
/* The tally sticks to the top of the form column. It used to sit above the
   fields, so it scrolled away exactly when it was needed: six cells is one
   screen and a bit, and "how many left?" is a question you have while looking
   at cell four, not cell one. */
.cktally-wrap { position: sticky; top: 0; z-index: 2; margin: .2rem -1rem .9rem;
    padding: .5rem 1rem; background: var(--ground-card);
    border-bottom: 0.5px solid var(--rule); }
.cktally { font-family: var(--font-mono); font-size: 11px; letter-spacing: .1em;
    text-transform: uppercase; color: var(--type-3); margin-right: .5rem; }
.cktally.all { color: var(--success); }
.cktally-left { font-family: var(--font-sans); font-size: .78rem;
    color: var(--type-3); }
/* The verify button before the checking is done. Not disabled: the checklist
   is browser-only state and must never be able to stop a person publishing.
   It just stops looking like the obvious next thing. */
.verifynote { font-family: var(--font-mono); font-size: 10px; letter-spacing: .1em;
    text-transform: uppercase; color: var(--type-3); margin-left: .6rem; }
.verifynote.armed { color: var(--warning); }

/* The guided walk: one button that opens the next unchecked field. */
.ckwalk { font-family: var(--font-mono); font-size: 10px; letter-spacing: .12em;
    text-transform: uppercase; padding: .25rem .7rem; border-radius: 0;
    border: 0.5px solid var(--teal); background: var(--teal);
    color: var(--cream); cursor: pointer; margin-left: auto; }
.ckwalk:hover { background: var(--teal-dark); border-color: var(--teal-dark); }
/* The field being checked right now. A hairline, not a highlight: the reader
   is meant to be looking at the document, not at this. */
.ck.is-now { box-shadow: -3px 0 0 var(--teal); padding-left: .4rem; }

/* Settled everything: the bar stops reporting and starts pointing. */
.cktally-wrap.ready { border-bottom-color: var(--success); }
.ckgo { font-family: var(--font-mono); font-size: 10px; letter-spacing: .12em;
    text-transform: uppercase; padding: .25rem .7rem; border-radius: 0;
    border: 0.5px solid var(--success); background: var(--success);
    color: var(--cream); cursor: pointer; margin-left: auto; }
.ckgo:hover { background: var(--teal); border-color: var(--teal); }
.cktally-wrap { display: flex; align-items: center; gap: .5rem; flex-wrap: wrap; }
/* Where you landed, for a moment. Colour only, and nothing at all under
   prefers-reduced-motion, which the global rule already handles. */
#verifyrow.landed button { outline: 2px solid var(--success); outline-offset: 3px; }

/* The verdict legend: definition, distribution and filter in one row. */
.vlegend { display: flex; align-items: center; gap: .1rem .75rem; flex-wrap: wrap;
    margin: .1rem 0 1rem; padding: .5rem .7rem; border: 0.5px solid var(--rule-soft);
    background: var(--ground-card); }
.vlegend-l { font-family: var(--font-mono); font-size: 10px; letter-spacing: .14em;
    text-transform: uppercase; color: var(--type-3); margin-right: .3rem; }
.vkey { display: inline-flex; align-items: baseline; gap: .4rem;
    text-decoration: none; padding: .15rem .45rem; border: 0.5px solid transparent; }
.vkey:hover { border-color: var(--rule); }
.vkey.on { border-color: var(--teal); background: var(--ground-deep); }
.vkey-n { font-family: var(--font-mono); font-size: .95rem; font-weight: 500;
    font-variant-numeric: tabular-nums; color: var(--type-1); }
.vkey-g { font-family: var(--font-sans); font-size: .78rem; color: var(--type-3); }
.vkey-clear { font-family: var(--font-mono); font-size: 10px; letter-spacing: .12em;
    text-transform: uppercase; color: var(--type-3); margin-left: auto; }

.verdict-gloss { font-family: var(--font-sans); font-size: .8rem;
    font-weight: 400; letter-spacing: 0; color: var(--type-3);
    margin-left: .4rem; }
.verdict-none { font-family: var(--font-mono); font-size: .9em;
    color: var(--type-3); letter-spacing: .06em; }
table { border-collapse: collapse; width: 100%; font-family: var(--font-sans);
        font-size: .85rem; }
td, th { border: 0.5px solid var(--rule); padding: .3rem .45rem; text-align: left; }
th { font-family: var(--font-mono); font-size: 10px; letter-spacing: .14em;
     text-transform: uppercase; color: var(--type-3); font-weight: 500;
     background: var(--ground-card); }
code { font-family: var(--font-mono); background: var(--ground-deep);
       padding: 0 .25rem; border-radius: 2px; font-size: .9em; }

.pill { display: inline-block; font-family: var(--font-mono); font-size: 10px;
        letter-spacing: .12em; text-transform: uppercase; padding: .1rem .45rem;
        border-radius: 0; border: 0.5px solid var(--rule); vertical-align: middle; }
.pill.todo { color: var(--warning); border-color: var(--warning); }
.pill.done { color: var(--success); border-color: var(--success); }

.toggle { display: flex; flex-wrap: wrap; gap: .3rem; margin: .6rem 0; }
.toggle a { text-decoration: none; color: inherit; font-family: var(--font-mono);
    font-size: 11px; letter-spacing: .12em; text-transform: uppercase;
    padding: .25rem .7rem; border: 0.5px solid var(--rule); border-radius: 0; }
.toggle a.on { background: var(--teal); color: var(--cream); border-color: var(--teal); }

.dbbar { display: flex; align-items: center; gap: .5rem; flex-wrap: wrap;
    margin: 0 0 1.25rem; padding: .45rem .7rem; border: 0.5px solid var(--rule);
    border-radius: 0; background: var(--ground-card);
    font-family: var(--font-mono); font-size: 11px; }
.dbbar label { margin: 0; font-size: 10px; }
.dbbar select { font-family: var(--font-mono); font-size: 11px; padding: .2rem;
    max-width: 26rem; width: auto; }
.dbbar small { color: var(--type-3); font-family: var(--font-mono); }
.dbbar .ro { background: var(--danger); color: var(--cream); border-radius: 0;
    padding: .1rem .45rem; letter-spacing: .1em; }
.dbbar .rw { background: var(--success); color: var(--cream); border-radius: 0;
    padding: .1rem .45rem; letter-spacing: .1em; }

/* ---- the review screen ------------------------------------------------- */
/* Two columns: the cited page on the left, the row's cells on the right. The
   left column is sticky, because the whole point is to keep the document in
   view while you work down the fields -- scrolling the article out of sight to
   reach the cell it proves is the exact problem this layout exists to remove.
   Below 1100px they stack, document first. */
.review { display: grid; grid-template-columns: 1.15fr 1fr; gap: 1rem;
    align-items: start; margin-top: .6rem; }
@media (max-width: 1100px) { .review { grid-template-columns: 1fr; } }
.review .doccol { position: sticky; top: .5rem; }
.tabs { display: flex; flex-wrap: wrap; gap: .3rem; margin-bottom: .35rem; }
.tabs a { text-decoration: none; color: inherit; font-family: var(--font-mono);
    font-size: 10px; letter-spacing: .1em; text-transform: uppercase;
    padding: .3rem .6rem; border: 0.5px solid var(--rule); border-radius: 0;
    border-bottom-color: transparent; }
.tabs a.on { background: var(--teal); color: var(--cream); border-color: var(--teal); }
.tabs a small { opacity: .7; }
.pane { width: 100%; border: 0.5px solid var(--rule); border-radius: 0;
    background: var(--cream); display: block; }
.pane.tall { height: 74vh; min-height: 440px; }
.pane.short { height: 26rem; }
.panehint { font-family: var(--font-mono); font-size: 10px; letter-spacing: .1em;
    color: var(--type-3); margin: .3rem 0 0; }

/* The deterministic-check panel: rule, this row's value, verdict. A table
   rather than a list because the middle column is the point -- "announced is a
   real YYYY-MM anchor" says nothing until it is sitting next to 2022-01. */
table.rules td { vertical-align: top; font-size: .82rem; }
table.rules td.val { font-family: var(--font-mono); word-break: break-all; }
table.rules td.ok   { color: var(--success); font-family: var(--font-mono);
                      font-size: 11px; letter-spacing: .08em; white-space: nowrap; }
table.rules td.err  { color: var(--danger);  font-family: var(--font-mono);
                      font-size: 11px; letter-spacing: .08em; white-space: nowrap; }
table.rules td.warn { color: var(--warning); font-family: var(--font-mono);
                      font-size: 11px; letter-spacing: .08em; white-space: nowrap; }
details.explain { margin: .5rem 0 0; }
details.explain summary { cursor: pointer; font-family: var(--font-mono);
    font-size: 11px; letter-spacing: .1em; color: var(--type-3); }
details.explain[open] summary { margin-bottom: .5rem; }

/* The agentic-check picker: cells across, one line, before the pane. */
.cells { display: flex; flex-wrap: wrap; gap: .1rem .9rem; margin: .4rem 0 .6rem; }
.cells label { display: inline-flex; align-items: baseline; gap: .3rem;
    margin: 0; font-family: var(--font-sans); font-size: .85rem;
    letter-spacing: 0; text-transform: none; color: inherit; }
.cells input { width: auto; }
.cells code { font-size: .95em; }

/* Section 14: state change only, 200ms or less, and nothing at all for a
   reader who has asked for less motion. */
@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
"""


VOCABULARY = {
    "renamed": "Source / Screen / Verify",
    "legacy":  "Bronze / Silver / Gold (pre-rename)",
    "empty":   "no tables yet",
    "missing": "unreadable",
}


def _db_status() -> str:
    """One line in the band naming the database in use.

    Switching databases is something a person does two or three times a year;
    knowing which database they are looking at matters on every click, because
    promoting rows into a scratch copy while believing it is the real one is
    not recoverable. Those two jobs were one full-width card at the top of
    every page, which gave the rare control the best space on the screen and
    left the constant status as grey small print underneath it.

    So the status stays, quietly, in the spec-label register where the rest of
    the metadata lives. It raises its voice only when something is not the
    default: a scratch copy, or a read-only process. The switcher itself is in
    the footer.
    """
    current = Path(db_path())
    canonical = current.resolve() == Path(DEFAULT_DB).resolve()

    # Two different things used to share the name "read-only": a legacy-
    # vocabulary file, and $SCOREBOARD_READONLY for the whole process. The bar
    # only ever reported the first, so a read-only run displayed "writable"
    # while every write refused. Either one means you cannot write.
    ro = is_read_only() or READ_ONLY

    if ro:
        note = '<span class="dbflag ro">READ-ONLY</span>'
    elif not canonical:
        note = '<span class="dbflag alt">NOT THE CANONICAL DB</span>'
    else:
        note = ""
    cls = "dbstatus" + ("" if canonical and not ro else " dbstatus-alt")
    return (f'<span class="{cls}" title="{html.escape(str(current))}">'
            f'{html.escape(current.name)}{note}</span>')


def table_counts_safe() -> dict:
    """Row counts, or zeros if the file cannot be opened. The band renders on
    every page including error pages, so it must never be the thing that
    raises."""
    try:
        conn = _conn()
        try:
            return table_counts(conn)
        finally:
            conn.close()
    except Exception:
        return {}


def _db_switcher() -> str:
    """The switcher, in the footer. Rare control, quiet placement."""
    options = []
    for d in discover_databases():
        label = f"{d['rel']}  \u00b7  {VOCABULARY.get(d['flavour'], d['flavour'])}, {d['rows']} rows"
        sel = " selected" if d["active"] else ""
        options.append(f'<option value="{html.escape(str(d["path"]))}"{sel}>{html.escape(label)}</option>')
    return f"""<footer class="pagefoot"><div class="wrap">
<form class="dbswitch" method="post" action="/db">
<label for="dbsel">Database</label>
<select id="dbsel" name="path">{''.join(options)}</select>
<button type="submit">Switch</button>
</form>
<p class="footpath">{html.escape(str(db_path()))}</p>
</div></footer>"""



WORDMARK = (
    '<div class="iaf-logo">'
    '<span class="iaf-logo-line">'
    '<span class="cream">INDUSTRIOUS</span><span class="accent">AF</span></span>'
    '<span class="iaf-logo-line">'
    '<span class="cream">INDUSTRIO</span><span class="accent">USA</span>'
    '<span class="cream">F</span></span>'
    '</div>'
)

def _page(title: str, body: str, msg: str | None = None,
          wide: bool = False) -> HTMLResponse:
    """The page skeleton.

    Body is full width and `.wrap` carries the reading measure. That ordering
    matters: while the measure lived on body, the teal band was capped with it
    and floated in the middle of a wide screen with two gutters beside it
    instead of running edge to edge.

    `wide` lifts the measure for the review screen, which puts a cited article
    beside the row it is evidence for and needs the room; every other page
    keeps the narrow column.
    """
    banner = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""
    # Brand book Section 02: "Page name * IndustriousAF", middle dot U+00B7,
    # never the bullet. Written as an escape so the separator cannot be
    # mangled by an editor that rewrites punctuation.
    full_title = f"{title} \u00b7 IndustriousAF"
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(full_title)}</title>
{_FONTS}
<style>{_BAGNARD}{_CSS}</style></head><body class="{'wide' if wide else ''}">
<header class="band"><div class="wrap">
{WORDMARK}
<nav><a href="/">Dashboard</a><a href="/source">Source</a><a href="/screen">Screen</a><a href="/verify">Verify</a></nav>
{_db_status()}
</div></header>
<div class="wrap">
<h1>{html.escape(title)}</h1>
{banner}{body}
</div>
{_db_switcher()}
</body></html>"""
    return HTMLResponse(doc)


def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _cell(row, col):
    """Value of a column that may not exist on this row -- e.g. a database that
    predates the *_raw / *_dt date columns and hasn't been re-init'd yet. Returns
    None (rendered as blank) instead of raising IndexError on a sqlite3.Row."""
    try:
        return row[col]
    except (IndexError, KeyError):
        return None


def _verdict_span(v: str | None, chk=None) -> str:
    """The verdict, never on its own.

    PASS and CLEAN are both positive words with no visible ordering, and the
    bare tokens left a reader guessing which was better and why. The ordering
    is CLEAN, then PASS, then FAIL, which is the opposite of how the names
    sort. So the token always arrives with the thing it is actually reporting:
    a count of open flags, or nothing open, or blocked.

    The token stays because it is the stored `result_status` and the CLI and
    the docs use it. It just never appears alone.
    """
    if v not in ("FAIL", "PASS", "CLEAN"):
        return '<span class="verdict-none">not checked</span>'
    n_err = n_warn = 0
    if chk is not None:
        try:
            n_err, n_warn = chk["n_errors"], chk["n_warnings"]
        except (KeyError, IndexError, TypeError):
            pass
    if v == "FAIL":
        gloss = f"blocked, {n_err} error(s)" if n_err else "blocked from Verify"
    elif v == "PASS":
        gloss = f"{n_warn} open flag(s)" if n_warn else "a flag to settle"
    else:
        gloss = "nothing open"
    return (f'<span class="verdict-{v}">{esc(v)}</span>'
            f'<span class="verdict-gloss">{esc(gloss)}</span>')


# --------------------------------------------------------------------------- #
# "What have I not moved down yet?" -- a purely presentational filter.         #
# Both stages already carry their lineage FK (screen_extracted.source_collected_id,
# verify_verified.screen_extracted_id), so backlog is a read of existing columns:  #
# nothing here writes, and no schema/pipeline behaviour changes.               #
# --------------------------------------------------------------------------- #

SHOW_MODES = ("all", "pending", "done")

# The chosen toggle is remembered per stage in a cookie, so it survives every
# redirect the app does (add a lead, run a check, promote a row...) instead of
# snapping back to "all" and making you re-click. Purely presentational: the
# cookie only ever selects which rows are RENDERED.
SHOW_COOKIE = {"/source": "pvp_show_source", "/screen": "pvp_show_screen"}
SHOW_COOKIE_MAX_AGE = 60 * 60 * 24 * 365   # a year; it is a UI preference


def _resolve_show(request: Request, stage: str, show: str | None,
                  n_pending: int | None = None) -> str:
    """The toggle to render with.

    An explicit `?show=` in the URL wins (that's the user clicking the toggle);
    otherwise fall back to the remembered choice, then to a default.

    The default is "pending", because these pages exist to work a queue and a
    queue that opens showing finished items alongside unfinished ones makes the
    reader do filtering the page could do. With one exception: when nothing is
    pending, "pending" renders an empty list under a toggle reading (0), which
    looks like a broken page rather than a finished stage. Source hit this the
    moment every lead had been extracted. Pass `n_pending` and the default
    falls back to "all" when the queue is empty.
    """
    if show in SHOW_MODES:
        return show
    remembered = request.cookies.get(SHOW_COOKIE[stage])
    if remembered in SHOW_MODES:
        # A remembered "pending" on a drained queue has the same problem.
        if remembered == "pending" and n_pending == 0:
            return "all"
        return remembered
    return "all" if n_pending == 0 else "pending"


def _remember_show(resp: HTMLResponse, stage: str, show: str) -> HTMLResponse:
    resp.set_cookie(SHOW_COOKIE[stage], show,
                    max_age=SHOW_COOKIE_MAX_AGE, samesite="lax")
    return resp


def _downstream_map(conn, table: str, fk: str) -> dict[int, list[int]]:
    """{parent row id: [ids of the rows it produced in `table`]}.

    A parent missing from this map has never been carried to the next stage.
    Rows whose FK is NULL (seeded / hand-pasted, no lineage recorded) simply
    contribute nothing -- they can't prove any parent was promoted.
    """
    out: dict[int, list[int]] = {}
    for child_id, parent_id in conn.execute(
        f"SELECT id, {fk} FROM {table} WHERE {fk} IS NOT NULL ORDER BY id"
    ):
        out.setdefault(int(parent_id), []).append(int(child_id))
    return out


def _keep(row_id: int, downstream: dict[int, list[int]], show: str) -> bool:
    if show == "pending":
        return row_id not in downstream
    if show == "done":
        return row_id in downstream
    return True


def _stage_toggle(path: str, show: str, labels: dict[str, str]) -> str:
    return '<div class="toggle">' + "".join(
        f'<a class="{"on" if show == mode else ""}" '
        f'href="{path}?show={mode}">{lbl}</a>'
        for mode, lbl in labels.items()
    ) + "</div>"


VERDICT_MEANING = {
    # Ordered best to worst, which is NOT how the names sort. Stated once, here,
    # and rendered wherever the three appear together.
    "CLEAN": ("nothing open",
              "Shaped correctly, in range, and no flag left for a person."),
    "PASS":  ("a flag to settle",
              "Shaped correctly and in range, with a note the extraction left. "
              "Verifiable: verifying is what settles the flag."),
    "FAIL":  ("blocked",
              "A schema error. Usually figures that clear neither half of the "
              "size floor. Cannot be verified until it is fixed."),
}


def _verdict_legend(counts, active: str | None, show: str) -> str:
    """The three verdicts defined where they are used, with counts, as filters.

    A glossary on another page is read once and forgotten. This sits beside the
    rows it describes, so the definition arrives at the moment it is needed; it
    carries the distribution, which is the "is this healthy?" question; and
    each entry filters, so the vocabulary is learned by using it rather than by
    being told.
    """
    base = f"/screen?show={show}"
    cells = []
    for v, (short, long) in VERDICT_MEANING.items():
        n = counts.get(v, 0)
        on = " on" if active == v else ""
        href = base if active == v else f"{base}&verdict={v}"
        cells.append(
            f'<a class="vkey{on}" href="{href}" title="{esc(long)}">'
            f'<span class="vkey-n">{n}</span>'
            f'<span class="verdict-{v}">{v}</span>'
            f'<span class="vkey-g">{esc(short)}</span></a>')
    clear = (f'<a class="vkey-clear" href="{base}">clear</a>' if active else "")
    return ('<div class="vlegend"><span class="vlegend-l">check results</span>'
            + "".join(cells) + clear + "</div>")


def _lineage_pill(row_id: int, downstream: dict[int, list[int]],
                  next_stage: str, todo_label: str) -> str:
    kids = downstream.get(row_id)
    if kids:
        ids = ", ".join(f"#{k}" for k in kids)
        return f'<span class="pill done">→ {esc(next_stage)} {esc(ids)}</span>'
    return f'<span class="pill todo">{esc(todo_label)}</span>'


def _to_int(v) -> int | None:
    try:
        return int(str(v).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# "The flag IS the reason"                                                     #
# --------------------------------------------------------------------------- #

def flag_only_reason(changes: dict, old_flag) -> str | None:
    """The provenance note a flag-only edit writes for itself, or None.

    Every write to a Verify row needs a reason in `verify_edits`; that rule is
    what makes the published Scoreboard auditable and it is not moving. But when
    the ONLY cell that changed is `flag`, the required reason had become a tax
    on the exact case that least needs one. The flag is free text whose entire
    job is to say what is unresolved about the row — so typing "resolved the
    flag" into a second box beside it records nothing the first box does not
    already hold, and it was being typed on every pass through the queue.

    So a flag-only edit is its own reason: this returns the note, quoting both
    the old text and the new so the history reads as a change and not just as a
    new assertion.

    The narrowness matters. The moment any other cell moves — with or without
    the flag moving too — this returns None and the reviewer must say why. A
    changed date is a changed fact about the world, and nothing in the `flag`
    cell can be relied on to explain it.
    """
    if set(changes) != {"flag"}:
        return None
    was = ("" if old_flag is None else str(old_flag)).strip()
    now = (changes["flag"] or "").strip()
    if not now:
        return f"Flag cleared (was: {was})" if was else "Flag cleared"
    if not was:
        return f"Flag set: {now}"
    return f"Flag changed from “{was}” to “{now}”"

