"""
dates.py -- deterministic date standardization for the medallion pipeline.

The whole point (tracker/old/SPEC.md): *standardize the schema enough so that
with a different model, we'd get the same result.* Everything numeric downstream
-- the `*_dt` DATETIME interpretations and the `lag_years` / `slip_years` floats
-- is computed **here**, deterministically, so two different extractors that agree
on the date strings produce identical lag/slip.

Each date is kept as **three** columns (raw -> token -> dt), so "verbatim" and
"parseable" never fight:

  * ``*_raw``  -- the EXACT source text, copied off the page with no cleanup
    (e.g. "…first output is slated for the first half of 2025…"). Provenance
    only; nothing parses it.
  * ``announced`` / ``promised_first_output`` / ``actual_first_output`` -- the
    normalized **token** the deterministic parser consumes (e.g. "2025 (first
    half)", "2020-05", "pending"). This is what the model must hand back cleanly.
  * ``*_dt``  -- the resolved concrete ISO date, computed here from the token.

The model extracts the ``*_raw`` verbatim AND the cleaned token; the pipeline owns
everything from the token rightward. See ``DATE_TRIPLES``.

Two ideas:

1. **A `*_dt` column for every date cell.** The original cell stays a string
   (what the source said, possibly fuzzy: "2025 (first half)", "2024-Q4", a bare
   year). The `*_dt` column is that string resolved to a concrete ISO date. A
   fuzzy *range* is collapsed to its **"healthy middle"** -- exactly the trick
   `plot_promised_vs_produced.py` uses (a mid-window point): a bare year -> Jul 1,
   "first half" -> Apr 1, "late"/"second half" -> Oct 1, "fall" -> Oct 15,
   "end" -> Nov 15 (the fourth quarter's middle), "early" -> Mar 1, a quarter ->
   the quarter's midpoint, `YYYY-MM` -> the 15th. No other word is read: see
   QUALIFIERS.

2. **`lag_years` / `slip_years` are floats, computed by arithmetic on the `*_dt`
   dates** (assert the later date really is later for lag). When a value can't be
   computed (no concrete first-output date), it becomes a numeric **sentinel** so
   the column stays a clean float:
       -1.0  ==  "to be completed"  (not produced yet: pending/open/tbd/...)
       -2.0  ==  "cancelled"        (promise cancelled / never delivered)
"""

from __future__ import annotations

import re
from datetime import date

# Numeric sentinels kept in the (float) lag/slip columns -- see module docstring.
TO_BE_COMPLETED = -1.0
CANCELLED = -2.0
NO_PROMISE = -3.0
PRODUCED_UNDATED = -4.0

# Map the float sentinel back to its standardized label (schema-level vocabulary).
SENTINEL_LABELS = {
    TO_BE_COMPLETED: "to be completed",
    CANCELLED: "cancelled",
    NO_PROMISE: "no promise recorded",
    PRODUCED_UNDATED: "produced, date unknown",
}

_CANCELLED_TOKENS = ("never", "cancel")  # 'cancel' also matches cancelled/canceled
_PENDING_TOKENS = ("pending", "tbd", "n/a", "open", "unknown")

# 'unconfirmed' is NOT pending. It means the sources say the plant has produced
# or is operating but give no date for first output -- an EVENT whose date is
# missing, not a project still waiting to produce. The collector had been using
# the two words that way consistently (26 of 27 'pending' rows say "not yet
# producing"; all 6 'unconfirmed' rows say the opposite) while the parser
# collapsed both to TO_BE_COMPLETED, which is the right-censoring flag. Five of
# fifty-three rows were therefore recorded as never having produced while their
# own current_status read "IN FULL OPERATION" or "PRODUCING".
_UNDATED_EVENT_TOKENS = ("unconfirmed",)

_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
_YMD_RE = re.compile(r"((?:19|20)\d{2})-(\d{1,2})(?:-(\d{1,2}))?")
_Q_RE = re.compile(r"Q([1-4])", re.IGNORECASE)

# The "healthy middle" of each quarter (month, day).
_QUARTER_MID = {1: (2, 15), 2: (5, 15), 3: (8, 15), 4: (11, 15)}

# Every word a date token may carry beside the date itself. interpret_date reads
# each of these. Any other word used to reach the bare-year rule and come back as
# July 1 without complaint, which is how "2026 (end)" and "2024 (fall)" were
# read. The checker now refuses a token with any other word (unrecognized_words),
# so a new qualifier has to be taught here first.
QUALIFIERS = ("first half", "second half", "early", "mid", "late", "end", "fall")
_QUALIFIER_RE = re.compile(
    r"first half|second half|\b(?:early|mid|late|end|fall|autumn|h[12]|[12]h|q[1-4])\b",
    re.IGNORECASE)


def unrecognized_words(token) -> str:
    """The words in a date token that interpret_date does not read, or ''.

    Digits, dashes and brackets are the date's own shape and never count.
    """
    s = _YMD_RE.sub(" ", str(token or ""))
    s = _YEAR_RE.sub(" ", s)
    s = _QUALIFIER_RE.sub(" ", s)
    return " ".join(re.findall(r"[A-Za-z]+", s))


def interpret_date(raw) -> tuple[str | None, str]:
    """Resolve a (possibly fuzzy) date string to a concrete ISO date.

    Returns ``(iso_date | None, kind)`` where ``kind`` is one of:
      * ``"date"``            -- ``iso_date`` is ``"YYYY-MM-DD"``
      * ``"cancelled"``       -- promise cancelled / never delivered (no date)
      * ``"to_be_completed"`` -- no concrete date yet (pending/open/tbd/...)
      * ``"produced_undated"`` -- it HAS produced, but no source gives a date
      * ``"empty"``           -- blank / unparseable

    Ranges are collapsed to their healthy middle (see the module docstring).
    """
    s = str(raw or "").strip()
    if s == "":
        return None, "empty"
    low = s.lower()
    if any(t in low for t in _CANCELLED_TOKENS):
        return None, "cancelled"
    if any(t in low for t in _UNDATED_EVENT_TOKENS):
        return None, "produced_undated"
    if any(t in low for t in _PENDING_TOKENS):
        return None, "to_be_completed"

    ym = _YEAR_RE.search(s)
    if not ym:
        return None, "empty"
    year = int(ym.group(0))

    # An explicit YYYY-MM or YYYY-MM-DD is the most precise -- honour it first.
    ymd = _YMD_RE.search(s)
    if ymd:
        month = min(max(int(ymd.group(2)), 1), 12)
        day = int(ymd.group(3)) if ymd.group(3) else 15  # mid-month for YYYY-MM
        try:
            return date(year, month, day).isoformat(), "date"
        except ValueError:
            return date(year, month, 15).isoformat(), "date"

    # A quarter -> the quarter's midpoint.
    q = _Q_RE.search(s)
    if q:
        month, day = _QUARTER_MID[int(q.group(1))]
        return date(year, month, day).isoformat(), "date"

    # Half-year / early-late qualifiers -> the healthy middle of that window.
    if "first half" in low or "1h" in low or "h1" in low:
        return date(year, 4, 1).isoformat(), "date"
    if "second half" in low or "2h" in low or "h2" in low or "late" in low:
        return date(year, 10, 1).isoformat(), "date"
    # "end" is the last quarter, so it resolves where Q4 does. It used to fall
    # through to the bare-year rule: "2026 (end)" came back as 2026-07-01, and a
    # plant promised for the end of the year read as overdue from July.
    if "end" in low:
        month, day = _QUARTER_MID[4]
        return date(year, month, day).isoformat(), "date"
    if "fall" in low or "autumn" in low:
        return date(year, 10, 15).isoformat(), "date"
    if "early" in low:
        return date(year, 3, 1).isoformat(), "date"
    if "mid" in low:
        return date(year, 7, 1).isoformat(), "date"

    # A bare year -> mid-year.
    return date(year, 7, 1).isoformat(), "date"


def _span_years(start_iso: str, end_iso: str) -> float:
    d0 = date.fromisoformat(start_iso)
    d1 = date.fromisoformat(end_iso)
    return round((d1 - d0).days / 365.25, 1)


def compute_lag_slip(announced, promised, actual):
    """Return ``(announced_dt, promised_dt, actual_dt, lag_years, slip_years)``.

    * ``lag_years``  = announced -> actual   (should be >= 0: the project must
      actually have produced; we assert the later date is later).
    * ``slip_years`` = promised  -> actual   (signed; **negative == early**).

    If ``actual`` has no concrete date the pair is a sentinel: ``CANCELLED``
    (-2.0) when the promise was cancelled/never delivered, ``PRODUCED_UNDATED``
    (-4.0) when it HAS produced but no source dates it, else ``TO_BE_COMPLETED``
    (-1.0). -1.0 and -4.0 are both "no number", and the difference between them
    decides whether a row is censored -- only -1.0 belongs in the censored set.

    When ``promised`` has no date, ``slip`` is ``NO_PROMISE`` (-3.0) and NOT
    ``TO_BE_COMPLETED``. Those are different facts and collapsing them corrupts
    the estimand: -1.0 says "this project has not produced yet", which is the
    right-censoring survival analysis is built to handle. But a project can have
    produced -- a real ``actual_first_output`` -- and still carry no promised
    date, because the source never stated one. Writing -1.0 there marked four
    rows of the first N=20 batch as not-yet-produced when they had in fact
    produced, which would have counted them as censored. There is nothing to
    complete; there was never a promise to measure against.
    """
    a_iso, _a_kind = interpret_date(announced)
    p_iso, _p_kind = interpret_date(promised)
    x_iso, x_kind = interpret_date(actual)

    if x_kind == "cancelled":
        return a_iso, p_iso, x_iso, CANCELLED, CANCELLED
    if x_kind == "produced_undated":
        # An event with a missing date, not a censored observation. Neither span
        # can be computed, but it must not sit in the censored set: a survival
        # model would treat a mill in full operation as still waiting.
        return a_iso, p_iso, x_iso, PRODUCED_UNDATED, PRODUCED_UNDATED
    if x_iso is None:  # pending / tbd / empty -> not produced yet
        return a_iso, p_iso, x_iso, TO_BE_COMPLETED, TO_BE_COMPLETED

    # Assertion: first output cannot precede the announcement. If the extracted
    # dates are inconsistent, lag comes out negative -- we return that (a negative
    # lag is visibly anomalous) rather than silently hiding a bad extraction.
    lag = _span_years(a_iso, x_iso) if a_iso else TO_BE_COMPLETED
    slip = _span_years(p_iso, x_iso) if p_iso else NO_PROMISE
    return a_iso, p_iso, x_iso, lag, slip


# The three date cells, each as a (raw verbatim, normalized token, resolved *_dt)
# triple -- kept next to each other so the rest of the pipeline can iterate them
# without hard-coding the names. `enrich` computes the *_dt (and lag/slip) from
# the *token*; the *_raw is verbatim provenance the extractor supplies.
DATE_TRIPLES = [
    ("announced_raw", "announced", "announced_dt"),
    ("promised_first_output_raw", "promised_first_output", "promised_first_output_dt"),
    ("actual_first_output_raw", "actual_first_output", "actual_first_output_dt"),
]

# The (token -> *_dt) partners, derived from the triples for the enrich step.
DATE_PAIRS = [(token, dt) for _raw, token, dt in DATE_TRIPLES]


def enrich(values: dict) -> dict:
    """Return a copy of ``values`` with the derived date + lag/slip cells set.

    Reads the three date **strings** and writes ``announced_dt``,
    ``promised_first_output_dt``, ``actual_first_output_dt`` and the float
    ``lag_years`` / ``slip_years``. Idempotent and deterministic -- this is the
    single place those five cells are ever computed (Screen insert, Verify
    promote, Verify edit all funnel through here).
    """
    out = dict(values)
    a_iso, p_iso, x_iso, lag, slip = compute_lag_slip(
        out.get("announced"),
        out.get("promised_first_output"),
        out.get("actual_first_output"),
    )
    out["announced_dt"] = a_iso
    out["promised_first_output_dt"] = p_iso
    out["actual_first_output_dt"] = x_iso
    out["lag_years"] = lag
    out["slip_years"] = slip
    return out


def lag_label(value) -> str:
    """Human label for a stored lag/slip float (turns the sentinels back to words)."""
    if value is None:
        return "—"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f in SENTINEL_LABELS:
        return SENTINEL_LABELS[f]
    return f"{f:g}"
