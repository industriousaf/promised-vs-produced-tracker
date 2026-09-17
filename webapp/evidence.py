"""
evidence.py -- the document pane: a cited page, read inside the review screen,
with the cells it is supposed to prove highlighted in it.

The problem this solves is the shape of the review itself. Verify is a human
gate, and the human's job is to confirm that each cell is what its cited page
actually says. Before this, "open a Screen row beside its two sources" meant
literally that: the form here, the article in another tab, and a person moving
between them holding a date in their head. Every row cost two full readings of
two articles, most of it spent hunting for one number.

So the pane works the way a signing flow works. The links on the row become
tabs; the tab renders the page underneath the form; the values the row claims
are highlighted *in* the page; and arrows at the bottom walk you from one to the
next. The reviewer confirms the row piece by piece instead of re-reading two
documents.

Three things happen here, in order:

  1. FETCH  -- `fetch()` gets the page, with the same ladder the collection
     prompts tell the model to use (browser user-agent first, then the Wayback
     Machine when the origin 403s or the page is gone). Saved to disk by
     `page_cache`, so a page downloads once, and a preload can download it
     before anyone opens it.
  2. SANITIZE -- `_Reader` rewrites the HTML down to an allowlist of text tags.
     Nothing executable survives: no script, style, iframe, form or event
     handler, and no remote asset. What is left is the article's words.
  3. HIGHLIGHT -- `needles_for()` turns a row's cells into the strings that
     would prove them ("2022-01" -> "January 2022"; 1_600_000_000 -> "$1.6
     billion", "$1.6B", "1,600,000,000"), and the sanitizer marks every hit.

Only the row's own stored URLs are ever fetched -- the route takes a column name
and an index, never a URL from the query string -- so this cannot be pointed at
an arbitrary address by editing the address bar.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

# webapp/ -> tracker/, so `pipeline` imports resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import APIRouter  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

from pipeline import screen, verify  # noqa: E402
from pipeline.db import db_path  # noqa: E402

from webapp import page_cache  # noqa: E402
from webapp.shared import _conn, esc  # noqa: E402

router = APIRouter()


# --------------------------------------------------------------------------- #
# Which links a row carries, and what each one is supposed to prove            #
# --------------------------------------------------------------------------- #

# Tab order. promise_source leads because that is where the review starts: the
# announcement is the half of the row that fixes `announced`, the capital, the
# jobs and the original promised date. The status side follows, because "what
# was promised" has to be settled before "did it happen" means anything.
SOURCE_FIELDS = (
    "promise_source",
    "promised_date_source",
    "status_source",
    "actual_date_source",
)

# What each link is cited FOR -- i.e. which cells get highlighted when you are
# reading it. A page is only evidence for the cells it was cited for; marking
# `actual_first_output` in the announcement would be marking a date the
# announcement cannot possibly carry.
HIGHLIGHT_FOR: dict[str, tuple[str, ...]] = {
    "promise_source": ("announced", "promised_first_output",
                       "promised_capital_usd", "promised_jobs"),
    "promised_date_source": ("promised_first_output", "announced"),
    "status_source": ("actual_first_output", "current_status"),
    "actual_date_source": ("actual_first_output", "current_status"),
}

# Display names + the colour band each cell gets in the pane, most important
# first. The order here is the order of the legend chips.
FIELD_LABELS = {
    "announced": "announced",
    "promised_first_output": "promised first output",
    "actual_first_output": "actual first output",
    "promised_capital_usd": "capital",
    "promised_jobs": "jobs",
    "current_status": "status",
}

# The tab labels name the two sides of the question the project is named
# after. "Promise" and "Status" broke that frame at the one moment a reviewer
# is actually making the comparison: they read as two unrelated documents
# rather than as the two halves of Promised vs. Produced.
#
# "Produced" is right even for a plant that produced nothing. It names the
# question the source answers, not the answer -- the same way the project's own
# title does. A source establishing that nothing has been produced is still
# the produced side of the comparison.
SHORT_SOURCE_LABEL = {
    "promise_source": "Promised",
    "promised_date_source": "Promised date",
    "status_source": "Produced",
    "actual_date_source": "Produced date",
}

# The inverse of HIGHLIGHT_FOR: which cited columns a given cell may rest on.
# HIGHLIGHT_FOR says a page is only evidence for the cells it was cited for;
# read the other way round it says where a cell's proof has to come from. A
# promised date is provable from the promise side of the row and nowhere else,
# a produced date from the produced side. That asymmetry is why the row carries
# four source columns instead of one, and it is the rule a reviewer reading the
# status page breaks when they tick `promised_first_output` because the year
# happens to appear there too.
SOURCES_FOR: dict[str, tuple[str, ...]] = {
    cell: tuple(src for src in SOURCE_FIELDS if cell in HIGHLIGHT_FOR.get(src, ()))
    for cell in FIELD_LABELS
}

_URL_TOKEN_RE = re.compile(r"https?://[^\s,;|\"'<>]+", re.IGNORECASE)


def _cell(row, col):
    try:
        v = row[col]
    except (IndexError, KeyError, TypeError):
        return None
    return v


def urls_in(value) -> list[str]:
    """Every URL in one provenance cell, in order, de-duplicated.

    A source column normally holds one link, but not always -- a reviewer who
    found a second page for the same fact pastes it in beside the first, and the
    checker's URL rule (`starts with http`) does not stop them. Those extra
    links are exactly the ones worth reading, so each becomes its own tab rather
    than being silently ignored because it was second in the cell.
    """
    if not value:
        return []
    out, seen = [], set()
    for m in _URL_TOKEN_RE.finditer(str(value)):
        u = m.group(0).rstrip(".,;:)]}”’'\"")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def tabs_for(row) -> list[dict]:
    """The row's cited pages as tabs: [{url, fields, label, field, i}].

    One tab per distinct URL, not per column. When two columns cite the same
    page -- common, and correct, when one article carries both the promise and
    the promised date -- it is one document and gets one tab, labelled with both
    columns and highlighting the union of what they are cited for.
    """
    by_url: dict[str, dict] = {}
    for field in SOURCE_FIELDS:
        for i, url in enumerate(urls_in(_cell(row, field))):
            t = by_url.get(url)
            if t is None:
                t = by_url[url] = {"url": url, "fields": [], "field": field, "i": i}
            if field not in t["fields"]:
                t["fields"].append(field)
    tabs = list(by_url.values())
    for n, t in enumerate(tabs):
        names = " · ".join(SHORT_SOURCE_LABEL.get(f, f) for f in t["fields"])
        dupes = sum(1 for x in tabs if x["field"] == t["field"])
        t["label"] = f"{names} {t['i'] + 1}" if dupes > 1 and t["i"] else names
        t["n"] = n
        t["highlight"] = [c for f in t["fields"] for c in HIGHLIGHT_FOR.get(f, ())]
        # de-dup, keeping FIELD_LABELS order so the legend is stable per tab
        t["highlight"] = [c for c in FIELD_LABELS if c in t["highlight"]]
        t["host"] = urllib.parse.urlsplit(t["url"]).netloc or t["url"]
    return tabs


# --------------------------------------------------------------------------- #
# Needles: the strings that would prove a cell                                 #
# --------------------------------------------------------------------------- #

_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]
_ORDINALS = {1: "first", 2: "second", 3: "third", 4: "fourth"}

# A gap inside a needle that may hold a day of the month. `announced` is stored
# as YYYY-MM, but almost nothing writes a month and a year with nothing between
# them: a press release says "January 21, 2022", and matching only "January
# 2022" left the pane highlighting the bare year eleven times and the actual
# announcement sentence not at all. The marker is written into the needle by
# _date_words and expanded by _compile; nothing else ever sees it.
DAY_GAP = "«D»"
_DAY_RE = r"\s+(?:\d{1,2}(?:st|nd|rd|th)?,?\s+)?"

_RAW_PARTNER = {
    "announced": "announced_raw",
    "promised_first_output": "promised_first_output_raw",
    "actual_first_output": "actual_first_output_raw",
}

# A first-output cell holding one of these is not a date at all -- it is the row
# saying "no source gives one". There is nothing to match, so instead of leaving
# the page unmarked we highlight the language that WOULD carry the answer, which
# is the reviewer's actual question: does this page say it produced, or not?
_SENTINELS = {"pending", "never", "unconfirmed", "n/a", "tbd", "open", "unknown"}

_PRODUCED_PHRASES = (
    "began production", "begin production", "started production",
    "start production", "commenced production", "first production",
    "production began", "production started", "first output", "came online",
    "came on line", "went online", "in production", "now producing",
    "began operations", "started operations", "operational", "ramping",
    "mass production", "volume production", "start of production",
    "rolled off the line", "opened its", "grand opening",
)
_PROMISE_PHRASES = (
    "expected to", "slated for", "scheduled to", "is set to", "aims to",
    "targeting", "on track", "by the end of", "due to", "will begin",
    "will start", "plans to begin", "anticipated",
)
_STATUS_PHRASES = (
    "under construction", "delayed", "postponed", "paused", "cancelled",
    "canceled", "scaled back", "shelved", "abandoned", "idled", "on hold",
    "broke ground", "groundbreaking", "topped out", "hiring",
)


def _int_or_none(v):
    try:
        return int(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _money_words(n: int) -> list[str]:
    """How a page would actually print a dollar figure.

    Nobody writes "$1,600,000,000" in an article; they write "$1.6 billion" or
    "$1.6B", and a rounding house-style may write "$1.7 billion" for
    1,650,000,000. All of those spellings are the same fact, so all of them are
    offered -- the grouped digits included, because filings and press releases
    do print them.
    """
    out = [f"{n:,}", str(n)]
    for unit, div, abbr in (("billion", 1_000_000_000, "B"),
                            ("million", 1_000_000, "M")):
        if n >= div:
            q = n / div
            forms = {f"{q:.10g}", f"{q:.1f}".rstrip("0").rstrip("."), f"{q:.1f}"}
            for s in forms:
                out += [f"${s} {unit}", f"{s} {unit}", f"${s}{abbr}",
                        f"${s}{abbr.lower()}n" if unit == "billion" else f"${s}{abbr.lower()}"]
            break
    return out


def _date_words(token: str) -> list[str]:
    """How a page would print a date token: "2022-01" -> "January 2022", "Jan.
    2022", "2022"; "2025 (first half)" -> "first half of 2025", "H1 2025",
    "2025"; "2025-Q4" -> "fourth quarter of 2025", "Q4 2025", "2025".

    The bare year is always included and always last. It is the weakest needle
    and it fires all over a page -- but it is also the one that finds the
    sentence when a publication spelled the month a way nothing here predicted,
    and the arrows make a handful of extra stops cheap.
    """
    t = (token or "").strip()
    if not t:
        return []
    low = t.lower()
    out: list[str] = []

    ym = re.match(r"^((?:19|20)\d{2})-(\d{1,2})$", t)
    if ym:
        year, mon = ym.group(1), int(ym.group(2))
        if 1 <= mon <= 12:
            name = _MONTHS[mon - 1]
            # Each of these swallows an optional day, so one needle covers both
            # "January 2022" and "January 21, 2022".
            out += [f"{name}{DAY_GAP}{year}", f"{name[:3]}.{DAY_GAP}{year}",
                    f"{name[:3]}{DAY_GAP}{year}", f"{name} of {year}", t]
            out.append(year)
            return out

    ymatch = re.search(r"(19|20)\d{2}", t)
    year = ymatch.group(0) if ymatch else ""

    q = re.search(r"q\s?([1-4])", low)
    if q:
        n = int(q.group(1))
        out += [f"Q{n} {year}", f"{year} Q{n}", f"{_ORDINALS[n]} quarter of {year}",
                f"{_ORDINALS[n]} quarter"]

    for phrase, extra in (
        ("first half", (f"H1 {year}", f"1H {year}", "first half of " + year)),
        ("second half", (f"H2 {year}", f"2H {year}", "second half of " + year)),
        ("early", (f"early {year}",)),
        ("late", (f"late {year}",)),
        ("mid", (f"mid-{year}", f"mid {year}")),
    ):
        if phrase in low:
            out += [phrase] + [e for e in extra if year]

    for name in _MONTHS:
        if name.lower() in low:
            out += [f"{name} {year}" if year else name, name]

    if year:
        out.append(year)
    return out or [t]


def field_tabs(row) -> dict:
    """{column: tab index} -- which tab highlights each cell.

    The checklist links straight to the tab that marks a given cell, so "find
    in source" lands on the right document rather than whichever one happened
    to be open.
    """
    out = {}
    for n, t in enumerate(tabs_for(row)):
        for f in t.get("highlight", ()):
            out.setdefault(f, n)
    return out


def settled_off_side(row, tab, field: str) -> bool:
    """Whether a confirmation recorded against tab index `tab` rests on the
    wrong side of the row for `field`.

    The pane only ever highlights a tab's own cells, so an uninterrupted walk of
    the checklist cannot reach a wrong answer. What can is a walk that was
    interrupted: the reviewer opens the produced page for another cell, reads
    the promised year there -- it is often in both documents -- and comes back
    and ticks. The tick is then about a page that cannot settle that cell.

    An unknown tab is NOT off-side. That is the older record written when the
    pane had never rendered the field, and the row already reports it by
    carrying no page and no count; it is a thinner claim, not a wrong one, and
    conflating the two would retitle every one of them as an error.
    """
    if tab is None or not field:
        return False
    try:
        tab = int(tab)
    except (TypeError, ValueError):
        return False
    tabs = tabs_for(row)
    if not 0 <= tab < len(tabs):
        return False
    return field not in tabs[tab].get("highlight", ())


def wanted_sources(field: str) -> str:
    """The tabs a cell has to be settled against, named as the pane names them."""
    names = [SHORT_SOURCE_LABEL.get(s, s) for s in SOURCES_FOR.get(field, ())]
    return " or ".join(names) or "a cited source"


def needles_for(row, fields: list[str]) -> list[tuple[str, str, str]]:
    """[(text, field, kind)] -- every string worth marking on a page cited for
    `fields`. `kind` is "value" (the cell's own content, marked strongly) or
    "context" (language that would carry the answer, marked faintly).

    The verbatim `*_raw` cell leads wherever there is one. It is the extractor's
    claim about what the page literally says, so if it does not appear on the
    page, the reviewer has found a real defect and the highlight's *absence* is
    itself the finding.
    """
    out: list[tuple[str, str, str]] = []

    def add(text, field, kind="value"):
        t = (text or "").strip()
        if len(t) >= 2:
            out.append((t, field, kind))

    for f in fields:
        val = _cell(row, f)
        sval = "" if val is None else str(val).strip()

        raw = _cell(row, _RAW_PARTNER.get(f, "")) if f in _RAW_PARTNER else None
        if raw and str(raw).strip():
            add(str(raw).strip(), f)

        if f in ("announced", "promised_first_output", "actual_first_output"):
            if sval.lower() in _SENTINELS or not sval:
                phrases = (_PRODUCED_PHRASES if f == "actual_first_output"
                           else _PROMISE_PHRASES)
                for p in phrases:
                    add(p, f, "context")
            else:
                for w in _date_words(sval):
                    add(w, f)
        elif f == "promised_capital_usd":
            n = _int_or_none(sval)
            if n:
                for w in _money_words(n):
                    add(w, f)
        elif f == "promised_jobs":
            n = _int_or_none(sval)
            if n:
                add(f"{n:,}", f)
                add(str(n), f)
        elif f == "current_status":
            for p in _STATUS_PHRASES + _PRODUCED_PHRASES:
                add(p, f, "context")

    # Longest first: "January 2022" must win over the bare "2022" inside it.
    seen, ordered = set(), []
    for text, field, kind in sorted(out, key=lambda x: -len(x[0])):
        key = text.lower()
        if key not in seen:
            seen.add(key)
            ordered.append((text, field, kind))
    return ordered


def _compile(needles: list[tuple[str, str, str]]) -> list[tuple[re.Pattern, str, str]]:
    """Whitespace-tolerant, case-insensitive patterns.

    Every run of whitespace becomes `\\s+`, because a verbatim quote copied off a
    rendered page and the same sentence in the HTML source almost never agree
    about line breaks -- and a quote that fails to match for that reason would
    read as "the page does not say this", which is the one wrong answer this
    pane must not give.
    """
    pats = []
    for text, field, kind in needles:
        body = _DAY_RE.join(
            r"\s+".join(re.escape(w) for w in part.split())
            for part in text.split(DAY_GAP)
        )
        # Word-boundary only where the edge is a word character, so "$1.6B" and
        # "2,000" still anchor properly.
        left = r"\b" if text[:1].isalnum() else ""
        right = r"\b" if text[-1:].isalnum() else ""
        try:
            pats.append((re.compile(left + body + right, re.IGNORECASE), field, kind))
        except re.error:  # pragma: no cover - defensive
            continue
    return pats


# --------------------------------------------------------------------------- #
# Fetch                                                                        #
# --------------------------------------------------------------------------- #

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_TIMEOUT = 25
_MAX_BYTES = 4_000_000

# {(database, stage, row_id, field): {"url", "tab", "count"}} -- what the pane
# last showed for one field, so a confirmation can record the page it was made
# against.
#
# The database is part of the key and not an afterthought. The app has a
# switcher, ids restart at 1 in every file, and without it a confirmation made
# in one database could be stamped with the URL and hit count of a different
# project that happened to share an id in another. A test caught exactly that.
#
# The pane is served through a sandboxed frame with no same-origin access, so
# nothing can be read back out of it. This is the other end of that: the server
# already resolved the URL and counted the hits while rendering, so it keeps the
# answer here instead of the page having to report it. Process-local, and a miss
# is harmless -- the attestation simply stores an unknown count rather than a
# guessed one.
_SHOWN: dict[tuple, dict] = {}
_SHOWN_MAX = 400


def _remember_shown(stage: str, row_id: int, tab: int, url: str, counts: dict) -> None:
    if len(_SHOWN) >= _SHOWN_MAX:
        _SHOWN.clear()          # a reading aid, not a record; losing it costs nothing
    for field, n in counts.items():
        _SHOWN[(str(db_path()), stage, row_id, field)] = {
            "url": url, "tab": tab, "count": n}


def last_shown(stage: str, row_id: int, field: str) -> dict | None:
    """What the pane last rendered for this field, or None if it never did."""
    return _SHOWN.get((str(db_path()), stage, row_id, field))


def _decompress(body: bytes, encoding: str) -> bytes:
    """Undo Content-Encoding, when we can.

    urllib never asks for compression, so an origin usually sends none -- but the
    Wayback Machine's `id_` form replays the ORIGINAL response headers and bytes,
    gzip and all, and a gzip stream decoded as text is 60KB of replacement
    characters that highlights nothing and looks, from the pane, exactly like an
    article that does not mention the date. Which is the one wrong answer this
    thing must never give. Brotli has no decoder in the standard library; the
    caller falls back to the archive's rendered form for that.
    """
    enc = (encoding or "").lower().strip()
    try:
        if enc == "gzip":
            import gzip
            return gzip.decompress(body)
        if enc == "deflate":
            import zlib
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
        if enc in ("br", "zstd"):
            mod = __import__("brotli" if enc == "br" else "zstandard")
            return mod.decompress(body)
    except Exception:
        return b""
    return body


def _looks_like_text(body: bytes) -> bool:
    """A cheap "did that decompress" test: real HTML has angle brackets and few
    NUL bytes; a compressed stream read as text has neither property."""
    head = body[:4096]
    return bool(head) and head.count(b"\x00") < 8 and head.count(b"<") >= 3


def _get(url: str) -> dict:
    """One HTTP GET with a browser user-agent. Never raises."""
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            ctype = resp.headers.get_content_type() or ""
            if ctype not in ("text/html", "application/xhtml+xml", "text/plain",
                             "application/json"):
                return {"ok": False, "status": resp.status,
                        "error": f"the page is {ctype}, not HTML — open it in a new tab",
                        "final_url": resp.geturl(), "html": ""}
            body = resp.read(_MAX_BYTES)
            enc = resp.headers.get("Content-Encoding", "")
            if enc:
                body = _decompress(body, enc)
            if not _looks_like_text(body):
                return {"ok": False, "status": resp.status, "html": "",
                        "final_url": resp.geturl(),
                        "error": f"the response is not readable text"
                                 + (f" (Content-Encoding: {enc})" if enc else "")}
            charset = resp.headers.get_content_charset()
            text = body.decode(charset or "utf-8", errors="replace")
            if not charset:
                m = re.search(rb'charset=["\']?([\w-]+)', body[:4096], re.IGNORECASE)
                if m:
                    try:
                        text = body.decode(m.group(1).decode("ascii"), errors="replace")
                    except (LookupError, UnicodeDecodeError):
                        pass
            return {"ok": True, "status": resp.status, "error": "",
                    "final_url": resp.geturl(), "html": text}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "html": "", "final_url": url,
                "error": f"HTTP {e.code} {e.reason}"}
    except Exception as e:
        return {"ok": False, "status": 0, "html": "", "final_url": url,
                "error": f"{type(e).__name__}: {e}"}


def explain_fetch_error(res: dict, host: str) -> tuple[str, str]:
    """(what happened, what it means) in a reviewer's words.

    The catch-all fetch path stores `TypeName: message`, which put things like
    "URLError: <urlopen error [Errno 8] nodename nor servname provided, or not
    known>" at the top of the pane as the headline. That is the interpreter
    talking to a programmer. A person checking a factory's capital figure needs
    to know whether the source is gone, refusing, or merely slow, because those
    lead to different next steps.

    The raw text is kept and shown, just not first.
    """
    raw = (res.get("error") or "").strip()
    low = raw.lower()
    status = res.get("status") or 0

    if res.get("unreadable") == "region" or status == 451:
        return (f"{host} would not show this page in this region",
                "It sent a notice that the site is not available where this "
                "machine is, instead of the page. Many US news sites do this "
                "for readers in Europe. Turn on a VPN to a US location and try "
                "again.")
    if status in (401, 403):
        return (f"{host} refused the request",
                "The site blocks automated readers. It will usually open "
                "normally in your own browser.")
    if status in (404, 410):
        return (f"The page is gone from {host}",
                "The URL no longer exists at the origin. The archive is the "
                "next place to look, then a replacement URL.")
    if 500 <= status <= 599:
        return (f"{host} returned a server error",
                "The site is broken or overloaded right now, not necessarily "
                "later. Worth retrying before treating it as unreadable.")
    if "nodename nor servname" in low or "name or service not known" in low \
            or "gaierror" in low or "getaddrinfo" in low:
        return (f"{host} did not resolve",
                "The domain does not exist, or this machine cannot look it "
                "up. Check the address, and check you are online.")
    if "timed out" in low or "timeout" in low:
        return (f"{host} did not answer in time",
                "The request was given 25 seconds. A slow site sometimes "
                "answers on a second try.")
    if "connection refused" in low:
        return (f"{host} refused the connection",
                "Nothing is listening at that address for this request.")
    if "certificate" in low or "ssl" in low:
        return (f"{host} has a certificate that did not verify",
                "The connection could not be trusted, so it was not made.")
    if "not html" in low or "not readable text" in low:
        return (f"{host} did not return a readable page",
                raw)
    return (f"{host} could not be read", raw or "No further detail.")


def _wayback(url: str) -> dict:
    """The nearest Wayback snapshot, read through the `id_` form so the archive's
    own toolbar and scripts are not part of the page. The archive step
    in `pipeline/prompts/fetching.md`, which is where a third of cited pages end
    up: governor's-office and state-agency releases rotate off within a year or
    two, and this Tracker cites a lot of them."""
    api = "https://archive.org/wayback/available?url=" + urllib.parse.quote(url, safe="")
    probe = _get(api)
    snap = ""
    if probe["html"]:
        m = re.search(r'"url":\s*"(https?://web\.archive\.org/[^"]+)"', probe["html"])
        if m:
            snap = m.group(1).replace("\\/", "/")
    if not snap:
        snap = "https://web.archive.org/web/2020/" + url
    # `id_` asks for the archived bytes rather than the archive's rendered page,
    # which is what we want -- no toolbar, no rewritten links. But it replays the
    # original response headers too, so when it comes back unreadable (a brotli
    # body with no decoder here, most often) the rendered form is the fallback:
    # its chrome is a handful of divs, and the sanitizer drops those anyway.
    raw_snap = re.sub(r"(/web/\d+)(/)", r"\1id_\2", snap, count=1)
    res = _get(raw_snap)
    if not res["ok"] and raw_snap != snap:
        res = _get(snap)
        raw_snap = snap
    res["via"] = "wayback"
    res["snapshot"] = raw_snap
    return res


def fetch(url: str, via: str = "auto", fresh: bool = False,
          retry_failed: bool = False) -> dict:
    """The page, following the same ladder the collection prompts prescribe.

    `via="wayback"` skips the origin entirely, which is the button the pane
    offers when the live page loads but is a paywall stub or a JavaScript shell.

    A saved copy is used when there is one; see page_cache. `fresh` downloads
    regardless, which is the pane's "fetch again". `retry_failed` downloads a
    page whose last attempt failed however recently, which is the preload.
    """
    key = f"{via}|{url}"
    with page_cache.lock(key):
        if not fresh:
            saved = page_cache.read(key, failures=not retry_failed)
            if saved is not None:
                return saved
        if not re.match(r"^https?://", url, re.IGNORECASE):
            res = {"ok": False, "status": 0, "html": "", "final_url": url, "via": "none",
                   "error": "not an http(s) URL — nothing to fetch"}
        elif via == "wayback":
            res = _judge(_wayback(url), url)
        else:
            res = _judge(_get(url), url)
            res["via"] = "live"
            # 403/404/410/429 and timeouts are exactly the ladder's steps 2-4, and
            # the answer to all of them is the archive. So is a region notice.
            # A page with almost no text is replaced only by an archived copy
            # that has more, because a short page may be all there is.
            thin = res["ok"] and res["words"] < NEARLY_EMPTY
            if not res["ok"] or thin:
                arch = _judge(_wayback(url), url)
                if arch["ok"] and (not thin or arch["words"] >= NEARLY_EMPTY):
                    if thin:
                        arch["origin_error"] = f"almost no text ({res['words']} words)"
                        arch["origin_unreadable"] = "empty"
                    else:
                        arch["origin_error"] = res["error"]
                        arch["origin_unreadable"] = res.get("unreadable")
                    res = arch
        res["fetched_at"] = time.time()
        page_cache.write(key, res)
        return res


# --------------------------------------------------------------------------- #
# Pages that download but are not the page                                     #
# --------------------------------------------------------------------------- #

# Fewer words than this on a page that loaded, and the page is nearly empty: a
# JavaScript shell, a cookie wall, a stub. A short press release still runs to
# a few hundred.
NEARLY_EMPTY = 100

# What a site shows a visitor from outside the country in place of the page,
# with status 200, so the download looks as if it worked. The first three are the
# notices Tribune, Lee Enterprises and the Dallas Morning News put up for readers
# in Europe when GDPR took effect, which many regional US sites still show. The
# last is Cloudflare's, for a site that blocks whole countries.
_REGION_NOTICE = re.compile(
    r"unavailable in most european countries"
    r"|from a country belonging to the european economic area"
    r"|unavailable to (?:european union|european|eu|eea) (?:visitors|users|readers)"
    r"|(?:not available|unavailable|not accessible) in your (?:region|country|location|area)"
    r"|banned the country or region your ip address is in",
    re.IGNORECASE)

# A notice is short. An article that quotes one is not a notice, so the pattern
# counts only on a page with fewer words than this.
_NOTICE_MAX_WORDS = 400


def _plain(doc: str) -> str:
    """The text of rendered pane HTML, with the markup and extra space taken out."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", doc))).strip()


def _judge(res: dict, url: str) -> dict:
    """Measure a download, and fail it if what came back is not the page.

    Adds `words`, the words of text the pane would show. A region notice
    becomes a failure marked unreadable="region", so the archive is asked, the
    pane says why, and the preload tries it again. A nearly empty page is left a
    success: it may be a short page that really is all there is, and the pane
    says what no highlight on it means rather than hiding it.
    """
    if res.get("status") == 451:
        res["unreadable"] = "region"
    if not res.get("ok"):
        return res
    doc, _title, _marks = render_document(res["html"], res.get("final_url") or url, [])
    text = _plain(doc)
    res["words"] = len(re.findall(r"\w+", text))
    if res["words"] < _NOTICE_MAX_WORDS and _REGION_NOTICE.search(text):
        res.update(ok=False, unreadable="region",
                   error="a notice that the page is not available in this region")
    return res


# --------------------------------------------------------------------------- #
# Sanitize + highlight                                                         #
# --------------------------------------------------------------------------- #

# Dropped with everything inside them. Two reasons in one list: the executable
# and embedding tags (nothing from a fetched page may run or load), and the page
# chrome that is never the article (nav bars, cookie dialogs, comment forms).
#
# `head` is NOT here even though nothing in it is article text, because <title>
# is in it and the pane's header line is the page's title. Everything in a head
# worth dropping -- script, style, link, meta, base -- is dropped by name anyway.
_DROP_TREE = {
    "script", "style", "noscript", "iframe", "object", "svg",
    "canvas", "form", "button", "select", "textarea", "video",
    "audio", "template", "picture", "map", "dialog", "nav", "figure",
}

# Dropped WITHOUT skipping a subtree, because they have none: these never carry
# a closing tag, so treating them like the set above would arm a skip that
# nothing ever disarms and silently swallow the rest of the article. That is not
# hypothetical -- a single `<img>` in a news page did exactly that, and the pane
# rendered the headline and nothing after it.
_DROP_SELF = {
    "img", "link", "meta", "base", "source", "track", "area", "input",
    "col", "param", "embed", "wbr", "keygen",
}

# Kept, with their structure. Everything not here and not dropped is unwrapped:
# its text survives, its tag does not, so no layout or class from the origin can
# reach this page.
_KEEP = {
    "p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
    "dl", "dt", "dd", "blockquote", "pre", "code", "strong", "b", "em", "i",
    "u", "sup", "sub", "small", "table", "thead", "tbody", "tfoot", "tr",
    "td", "th", "caption", "a", "div", "section", "article", "main", "aside",
    "header", "footer", "figcaption", "time", "span", "abbr", "cite", "q",
}
_VOID = {"br", "hr"}

MAX_MARKS = 400


class _Reader(HTMLParser):
    """Fetched HTML in, safe HTML with `<mark>`s out."""

    def __init__(self, base_url: str, patterns):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.patterns = patterns
        self.out: list[str] = []
        self.stack: list[str] = []
        self.skip: list[str] = []
        self.n = 0
        self.per_field: dict[str, int] = {}
        self.title = ""
        self._in_title = False

    # -- structure ---------------------------------------------------------- #
    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.append(tag)
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in _DROP_SELF:
            return
        if tag in _DROP_TREE:
            self.skip.append(tag)
            return
        if tag in _VOID:
            self.out.append(f"<{tag}>")
            return
        if tag not in _KEEP:
            return                      # unwrap: keep the words, drop the tag
        if tag == "a":
            href = dict(attrs).get("href") or ""
            try:
                href = urllib.parse.urljoin(self.base, href)
            except ValueError:
                href = ""
            if href.lower().startswith(("http://", "https://")):
                self.out.append(
                    f'<a href="{html.escape(href, quote=True)}" target="_blank" '
                    'rel="noopener noreferrer nofollow">')
            else:
                self.out.append("<a>")
        else:
            self.out.append(f"<{tag}>")
        self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if not self.skip and tag in _VOID:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
            return
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag not in self.stack:
            return                      # stray close tag: ignore it
        # Close down to it, so malformed source cannot leave this page unbalanced.
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def close(self):
        super().close()
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")

    # -- text + highlighting ------------------------------------------------ #
    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self.skip or not data:
            return
        self.out.append(self._mark(data))

    def _mark(self, text: str) -> str:
        if not text.strip() or self.n >= MAX_MARKS:
            return html.escape(text)
        spans = []
        for rx, field, kind in self.patterns:
            for m in rx.finditer(text):
                spans.append((m.start(), m.end(), field, kind))
        if not spans:
            return html.escape(text)
        # Longest match wins at a position, and no two marks may overlap:
        # "January 2022" and the "2022" inside it are one highlight, not two.
        spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
        pieces, cursor = [], 0
        for start, end, field, kind in spans:
            if start < cursor or self.n >= MAX_MARKS:
                continue
            pieces.append(html.escape(text[cursor:start]))
            self.n += 1
            self.per_field[field] = self.per_field.get(field, 0) + 1
            pieces.append(
                f'<mark class="hl k-{kind}" id="m{self.n}" data-f="{field}">'
                f"{html.escape(text[start:end])}</mark>")
            cursor = end
        pieces.append(html.escape(text[cursor:]))
        return "".join(pieces)


def render_document(raw_html: str, base_url: str,
                    needles: list[tuple[str, str, str]]) -> tuple[str, str, dict]:
    """(safe html, page title, {field: n marks})."""
    r = _Reader(base_url, _compile(needles))
    try:
        r.feed(raw_html)
        r.close()
    except Exception:  # pragma: no cover - a parser giving up mid-page
        r.out.append("</p><p><i>(the rest of this page could not be parsed)</i></p>")
    return "".join(r.out), r.title.strip(), r.per_field


# --------------------------------------------------------------------------- #
# The pane                                                                     #
# --------------------------------------------------------------------------- #

_PANE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font: 15px/1.65 Georgia, 'Iowan Old Style', serif; margin: 0;
       padding: 0 0 3.2rem; }
#bar { position: sticky; top: 0; z-index: 5; display: flex; gap: .5rem;
       align-items: baseline; flex-wrap: wrap; padding: .45rem .8rem;
       border-bottom: 1px solid #8886; background: Canvas;
       font: 12px/1.4 system-ui, sans-serif; }
#bar b { font-weight: 600; }
#bar a { color: inherit; }
#bar .warn { color: #b45309; font-weight: 600; }
#doc { padding: .8rem 1.1rem; max-width: 46rem; overflow-wrap: break-word; }
#doc h1, #doc h2, #doc h3 { font-size: 1.15rem; line-height: 1.3; }
#doc table { border-collapse: collapse; font-size: .85rem; display: block;
             overflow-x: auto; }
#doc td, #doc th { border: 1px solid #8884; padding: .2rem .4rem; }
#doc pre { white-space: pre-wrap; }
#doc a { color: #2b6cb0; }
mark.hl { background: #fde68a; color: #111; border-radius: 2px;
          padding: 0 .1em; scroll-margin: 45vh; }
mark.k-context { background: #dbeafe; }
mark.hl.cur { outline: 2px solid #d97706; background: #fbbf24; }
mark.hl.off { background: transparent; color: inherit; outline: none; }
#nav { position: fixed; bottom: 0; left: 0; right: 0; z-index: 6;
       display: flex; gap: .5rem; align-items: center; flex-wrap: wrap;
       padding: .4rem .8rem; border-top: 1px solid #8886; background: Canvas;
       font: 12px/1.5 system-ui, sans-serif; }
#nav button { font: inherit; padding: .18rem .6rem; border: 1px solid #8887;
              border-radius: 6px; background: #6663; color: inherit;
              cursor: pointer; }
#nav button:disabled { opacity: .4; cursor: default; }
#pos { font-variant-numeric: tabular-nums; min-width: 5.5rem; }
.err-h { font-size: 1.15rem; font-weight: 600; margin: 0 0 .5rem; }
.err-m { margin: 0 0 .7rem; max-width: 46rem; }
.err-do { margin: 1.2rem 0 .3rem; }
.err-do-l { margin: 0; padding-left: 1.3rem; max-width: 46rem; }
.err-do-l li { margin-bottom: .45rem; }
.err-raw { margin-top: 1.3rem; opacity: .75; }
.err-raw summary { cursor: pointer; font-size: .85rem; }
.err-raw code { word-break: break-all; }
.chip { border: 1px solid #8887; border-radius: 999px; padding: .05rem .55rem;
        cursor: pointer; user-select: none; }
.chip.on { background: #fde68a; color: #111; border-color: #d97706; }
.chip.ctx.on { background: #dbeafe; }
.chip.none { opacity: .45; cursor: default; }
.empty { padding: 2rem 1.1rem; font: 14px/1.6 system-ui, sans-serif;
         max-width: 40rem; }
.empty code { background: #8882; padding: 0 .25rem; border-radius: 3px; }
#bar .age { opacity: .7; }
#refetching { display: none; color: #b45309; font-weight: 600; }
body.refetching #refetching { display: inline; }
body.refetching #doc, body.refetching .empty { opacity: .35; }
.thin { border: 1px solid #b45309; background: #fef3c7; color: #111;
        padding: .6rem .8rem; margin: 0 0 1rem; font: 13px/1.5 system-ui, sans-serif; }
#doc .thin a { color: #92400e; }
"""

# On a link that reloads this frame. The page around the frame cannot see a
# click in here, so nothing else would show that anything is happening, and a
# download through the archive can take a minute.
_RELOADS = 'onclick="document.body.classList.add(\'refetching\')"'

# Arrow keys and n/p work too: a reviewer walking a page of highlights should
# not have to move to the mouse between each one.
_PANE_JS = """
(function () {
  var marks = Array.prototype.slice.call(document.querySelectorAll('mark.hl'));
  var pos = document.getElementById('pos');
  var prev = document.getElementById('prev'), next = document.getElementById('next');
  var active = null, list = marks, i = -1;

  function refilter() {
    list = active ? marks.filter(function (m) { return m.dataset.f === active; }) : marks;
    marks.forEach(function (m) { m.classList.toggle('off', list.indexOf(m) < 0); });
    i = -1;
    if (list.length) { go(1); } else { paint(); }
  }
  function paint() {
    pos.textContent = list.length ? (i + 1) + ' of ' + list.length
                                  : 'no highlights';
    prev.disabled = next.disabled = list.length < 2;
  }
  function go(d) {
    if (!list.length) { paint(); return; }
    i = (i + d + list.length) % list.length;
    marks.forEach(function (m) { m.classList.remove('cur'); });
    list[i].classList.add('cur');
    list[i].scrollIntoView({ block: 'center', behavior: 'smooth' });
    paint();
  }
  prev.onclick = function () { go(-1); };
  next.onclick = function () { go(1); };
  Array.prototype.forEach.call(document.querySelectorAll('.chip[data-f]'), function (c) {
    c.onclick = function () {
      active = (active === c.dataset.f) ? null : c.dataset.f;
      Array.prototype.forEach.call(document.querySelectorAll('.chip[data-f]'), function (o) {
        o.classList.toggle('on', o.dataset.f === active);
      });
      refilter();
    };
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowRight' || e.key === 'n') { go(1); }
    else if (e.key === 'ArrowLeft' || e.key === 'p') { go(-1); }
  });
  var start = (typeof START_FIELD === 'string' && START_FIELD) ? START_FIELD : null;
  if (start) {
    active = start;
    Array.prototype.forEach.call(document.querySelectorAll('.chip[data-f]'), function (o) {
      o.classList.toggle('on', o.dataset.f === active);
    });
    refilter();
  } else if (marks.length) { go(1); } else { paint(); }

})();
"""


def _pane(title: str, bar: str, main: str, nav: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title><style>{_PANE_CSS}</style></head><body>
<div id="bar">{bar} <span id="refetching">fetching the page again, which can
take a minute</span></div>{main}{nav}</body></html>"""
    )


# --------------------------------------------------------------------------- #
# The tab strip, for embedding in a review screen                              #
# --------------------------------------------------------------------------- #

# Tabs are ordinary links with a `target`, so switching documents does not
# reload the page around them and cannot cost the reviewer a half-filled form.
# The script repaints what the page says about the pane; with JS off the pane
# still changes, it just stops saying which tab it is showing.
#
# It repaints from whichever link loaded the pane. It used to repaint on tab
# strip clicks only, but the checklist's "find in source" loads a tab without
# touching the strip. Opening a produced field that way left the highlighted
# tab, the loading screen and the field list under the pane all naming the
# Promised page, while the Produced one loaded and after it arrived. Lucas
# reported seeing the second source about one time in fifteen.
#
# The loading screen also lifted after 30 seconds whether or not anything had
# arrived, uncovering the previous page, still in the frame. A site that refuses
# is retried through the Wayback Machine, which takes longer than that, so the
# page uncovered was usually the Promised one. The screen now stays up until the
# frame loads, and after 30 seconds says it is still waiting and offers the page
# in a new tab.
_PANESTATE_JS = """
(function () {
  var veil = document.getElementById('paneload');
  if (!veil) { return; }
  var host = document.getElementById('paneloadhost');
  var slow = document.getElementById('paneloadslow');
  var open = document.getElementById('paneloadopen');
  var marks = document.getElementById('panemarks');
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.tabs a[data-tab]'));
  var timer = null;

  // Nothing here may READ the frame. It is sandboxed without allow-same-origin,
  // so it has an opaque origin: contentDocument is null and touching
  // contentWindow.location throws a SecurityError. An earlier version of this
  // function did exactly that and died on the spot, taking the click handler
  // below with it. So which tab is loading comes from the link, and the frame
  // lifts the loading screen itself, with the onload attribute in pane_html.
  function loading() { return !veil.classList.contains('done'); }

  function wait() {
    clearTimeout(timer);
    if (slow) { slow.hidden = true; }
    timer = setTimeout(function () {
      if (slow && loading()) { slow.hidden = false; }
    }, 30000);
  }

  function point(n) {
    var t = tabs.filter(function (x) { return x.dataset.tab === n; })[0] || tabs[0];
    if (!t) { return; }
    tabs.forEach(function (x) { x.classList.toggle('on', x === t); });
    if (host) { host.textContent = t.dataset.label + ' \\u00b7 ' + t.dataset.host; }
    if (open) { open.href = t.dataset.url; }
    if (marks) { marks.textContent = t.dataset.marks; }
  }

  // The first page was requested before this script ran, and may have arrived.
  if (loading()) { wait(); }

  document.addEventListener('click', function (e) {
    var a = e.target.closest ? e.target.closest('a[target="evidencepane"]') : null;
    if (!a) { return; }
    // A modified click opens the link in a new browser tab. The frame stays
    // where it is, so nothing is loading.
    if (e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) { return; }
    var n = '0';
    try { n = new URL(a.href, location.href).searchParams.get('tab') || '0'; } catch (err) {}
    point(n);
    veil.classList.remove('done');
    wait();
  }, true);
})();
"""


def pane_html(stage: str, row_id: int, row, tall: bool = True) -> str:
    """The document pane for one row: a tab per cited link, and the page itself.

    This is the review, restructured. The links stop being somewhere else to go
    and become the thing you are looking at, with the row's own claims marked in
    them and arrows to walk between the marks — so a reviewer confirms a row one
    highlighted sentence at a time instead of reading two articles through and
    holding the numbers in their head.
    """
    tabs = tabs_for(row)
    if not tabs:
        return ('<div class="card"><p><b>No sources on this project.</b> There is '
                "nothing to read it against — add a <code>promise_source</code> "
                "and a <code>status_source</code> below. The deterministic check "
                "refuses a project that cites nothing, and so should you.</p></div>")

    # Each tab carries what the page says about it while it loads. The script
    # cannot read the frame, and the link that loads a tab is not always the
    # tab: "find in source" carries only the tab's number.
    marks = {t["n"]: ", ".join(FIELD_LABELS.get(c, c) for c in t["highlight"])
             for t in tabs}
    strip = "".join(
        f'<a class="{"on" if t["n"] == 0 else ""}" target="evidencepane" '
        f'href="/evidence/{esc(stage)}/{row_id}?tab={t["n"]}" '
        f'data-tab="{t["n"]}" data-label="{esc(t["label"])}" '
        f'data-host="{esc(t["host"])}" data-url="{esc(t["url"])}" '
        f'data-marks="{esc(marks[t["n"]])}" '
        f'title="{esc(t["url"])}">{esc(t["label"])} '
        f'<small>{esc(t["host"])}</small></a>'
        for t in tabs
    )
    height = "tall" if tall else "short"
    # The frame lifts the loading screen itself, from an attribute rather than a
    # listener in the script. Inline scripts wait for the font stylesheet, but
    # the frame, which comes before this one, starts loading as soon as it is
    # read. A page served from the cache could arrive before a listener existed,
    # and the screen then stayed up over it until a 30-second timer ran out.
    return f"""
<div class="tabs">{strip}</div>
<div class="panewrap">
<div class="paneload" id="paneload" aria-live="polite">
  <span class="paneload-l">Loading the cited page</span>
  <span class="paneload-h" id="paneloadhost">{esc(tabs[0]["label"])} · {esc(tabs[0]["host"])}</span>
  <span class="paneload-n">A page is saved once it has loaded, so only its first
  opening waits on the site. If the site refuses, the pane falls back to the
  Wayback Machine.</span>
  <span class="paneload-n" id="paneloadslow" hidden>Still waiting after 30
  seconds. The site is slow or refusing, and the Wayback Machine can take another
  minute. <a id="paneloadopen" href="{esc(tabs[0]["url"])}" target="_blank"
  rel="noopener noreferrer">Open the page in a new tab ↗</a></span>
</div>
<iframe class="pane {height}" name="evidencepane" title="cited page"
  src="/evidence/{esc(stage)}/{row_id}?tab=0"
  onload="document.getElementById('paneload').classList.add('done')"
  sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"></iframe>
</div>
<p class="panehint">Highlighted in this tab: <span id="panemarks">{esc(marks[0])}</span>.
Walk them with the arrows at the foot of the pane (or ← →); click a chip there to
walk one field only. A value the page does <i>not</i> carry is the finding — the
pane will show no highlight for it.</p>
<script>{_PANESTATE_JS}</script>"""


def saved_when(res: dict) -> str:
    """How long ago the page in the pane was downloaded."""
    if not res.get("fetched_at"):
        return ""
    s = time.time() - float(res["fetched_at"])
    if s < 90:
        return "downloaded just now"
    if s < 3600:
        return f"saved {int(s // 60)} min ago"
    if s < 2 * 86400:
        return f"saved {int(s // 3600)} h ago"
    return f"saved {int(s // 86400)} days ago"


def _row_for(stage: str, row_id: int):
    conn = _conn()
    try:
        if stage == "verify":
            return verify.get_verified(conn, row_id)
        # A published project's pane marks the published values, the ones the
        # checklist beside it vouches for, while every address stays keyed to
        # the Screen id. See screen.review_view.
        return screen.review_view(conn, row_id)[0]
    finally:
        conn.close()


@router.get("/evidence/{stage}/{row_id}", response_class=HTMLResponse)
def evidence_pane(stage: str, row_id: int, tab: int = 0, via: str = "auto",
                  field: str = "", fresh: int = 0):
    """One cited page, rendered with the row's claims marked in it.

    The tab is addressed by INDEX into the row's own links, never by URL, so the
    only pages this route can be made to fetch are ones already stored on the
    row it was asked about. `fresh=1` downloads the page again rather than using
    the saved copy.
    """
    stage = "verify" if stage == "verify" else "screen"
    via = "wayback" if via == "wayback" else "auto"
    row = _row_for(stage, row_id)
    if row is None:
        return _pane("no project", "", '<p class="empty">No project with that id.</p>', "")

    tabs = tabs_for(row)
    if not tabs:
        return _pane(
            "no sources", "",
            '<p class="empty">This project cites no links at all. Add a '
            "<code>promise_source</code> and a <code>status_source</code> in the "
            "form above — a project with nothing to read cannot be verified, and the "
            "deterministic check will refuse it.</p>", "")
    tab = tab if 0 <= tab < len(tabs) else 0
    t = tabs[tab]

    res = fetch(t["url"], via=via, fresh=bool(fresh))
    origin = urllib.parse.urlsplit(t["url"]).netloc
    again = (f'?tab={tab}&amp;via={via}&amp;field={urllib.parse.quote(field)}'
             f'&amp;fresh=1')

    if not res["ok"]:
        note = ""
        if res.get("origin_error"):
            note = f" (the live page said: {esc(res['origin_error'])})"
        bar = (f'<b>{esc(SHORT_SOURCE_LABEL.get(t["field"], t["field"]))}</b> '
               f'<a href="{esc(t["url"])}" target="_blank">{esc(origin)} ↗</a> '
               f'<span class="warn">could not be read</span>')
        headline, meaning = explain_fetch_error(res, origin)
        body = f"""<div class="empty">
<p class="err-h">{esc(headline)}</p>
<p class="err-m">{esc(meaning)}</p>
<p class="err-m">The Wayback Machine was tried as well and had nothing. A page
that cannot be read is a fact about the source, not about the project, so it does
not make the project wrong.</p>
<p class="err-do"><b>What to do</b></p>
<ol class="err-do-l">
<li>Open it yourself:
    <a href="{esc(t['url'])}" target="_blank">{esc(t['url'])}</a></li>
<li><a href="{again}" {_RELOADS}>Try again now</a>. This answer is kept for
    half an hour, so reopening the tab will not retry it: use this after a site
    was down, or after turning on a VPN for a site that refuses other
    countries.</li>
<li><a href="?tab={tab}&amp;via=wayback" {_RELOADS}>Ask the archive directly</a>,
    which sometimes finds a snapshot this did not.</li>
<li>If the page is genuinely gone, find a replacement source and put it in the
    project, or record what happened in <code>flag</code>. Do not leave the field
    looking checked.</li>
</ol>
<details class="err-raw"><summary>What the fetch actually returned</summary>
<p><code>{esc(res.get('error') or 'no error text')}</code>
{f"<br>HTTP status {esc(str(res.get('status')))}" if res.get('status') else ""}</p>
</details></div>"""
        return _pane("unreadable", bar, body, "")

    needles = needles_for(row, t["highlight"])
    doc, page_title, counts = render_document(res["html"], res.get("final_url") or t["url"], needles)
    # Zeroes included: "the value is not on this page" is the more interesting
    # half of the record, and render_document only reports fields it found.
    _remember_shown(stage, row_id, tab, res.get("final_url") or t["url"],
                    {c: counts.get(c, 0) for c in t["highlight"]})

    via_note = ""
    if res.get("via") == "wayback":
        why = {"region": ", because the site would not show the page in this region",
               "empty": ", because the live page had almost no text",
               }.get(res.get("origin_unreadable"), "")
        via_note = (f'<span class="warn">read via the Wayback Machine{why}</span> '
                    f'<a href="{esc(res.get("snapshot", ""))}" target="_blank">snapshot ↗</a>')
    bar = (f'<b>{esc(page_title[:90] or origin)}</b> '
           f'<a href="{esc(t["url"])}" target="_blank">{esc(origin)} ↗</a> '
           f'{via_note}')
    if res.get("via") != "wayback":
        bar += f' <a href="?tab={tab}&amp;via=wayback" {_RELOADS}>try the archive</a>'
    bar += (f' <span class="age">{esc(saved_when(res))}</span>'
            f' <a href="{again}" {_RELOADS}>fetch again</a>')

    chips = "".join(
        f'<span class="chip{" ctx" if c in ("current_status",) else ""}'
        f'{" none" if not counts.get(c) else ""}" data-f="{esc(c)}">'
        f'{esc(FIELD_LABELS.get(c, c))} {counts.get(c, 0)}</span>'
        for c in t["highlight"]
    )
    nav = f"""<div id="nav">
<button id="prev" type="button" title="previous highlight (←)">◀</button>
<span id="pos"></span>
<button id="next" type="button" title="next highlight (→)">▶</button>
<span>{chips}</span>
<span style="opacity:.6">click a chip to walk just that field · ← → to move</span>
</div><script>var START_FIELD = {json.dumps(field)};</script>
<script>{_PANE_JS}</script>"""

    # A page with almost no text and nothing marked on it cannot show that a
    # value is missing, yet it looks just like a page that does. With a mark on
    # it, a short page is simply short.
    thin = ""
    words = len(re.findall(r"\w+", _plain(doc)))
    if words < NEARLY_EMPTY and not any(counts.values()):
        thin = (f'<div class="thin"><b>This page came through with almost no text '
                f'({words} words), so a missing highlight here tells you nothing.</b> '
                f'It is probably built by JavaScript, which the pane does not run, or '
                f'it sits behind a sign-in or cookie wall. '
                f'<a href="{esc(t["url"])}" target="_blank" rel="noopener noreferrer">'
                f'Open it in your browser ↗</a> to read it.</div>')

    return _pane(page_title or origin, bar, f'<div id="doc">{thin}{doc}</div>', nav)


# --------------------------------------------------------------------------- #
# The preload                                                                  #
# --------------------------------------------------------------------------- #

def preload_targets(conn) -> list[dict]:
    """Every page a verifier is about to open, in the order they will open them.

    The review queue first, largest capital first, which is the order it is
    walked; then the published projects with a field to settle again. The pages
    are the ones the pane would show, so the published record's for a published
    project, and a page two projects cite is downloaded once.
    """
    ids = [r["id"] for r in screen.review_queue(conn)["ready"]]
    seen = set(ids)
    ids += [sid for sid in screen.needs_resettle(conn) if sid not in seen]
    pages: dict[str, dict] = {}
    for sid in ids:
        row, _published = screen.review_view(conn, sid)
        if row is None:
            continue
        for t in tabs_for(row):
            page = pages.setdefault(t["url"], {"url": t["url"], "host": t["host"],
                                               "projects": []})
            page["projects"].append({"id": sid, "project": row["project"],
                                     "tab": t["label"]})
    return list(pages.values())


def start_preload() -> page_cache.Run:
    """Download every page the waiting projects cite, on a thread.

    A page that loaded within the week is skipped. A page that failed is tried
    again however recently it failed, so running this after turning on a VPN
    retries exactly the pages that need it.
    """
    def targets() -> list[dict]:
        conn = _conn()
        try:
            return preload_targets(conn)
        finally:
            conn.close()
    return page_cache.start(targets, lambda url: fetch(url, retry_failed=True))
