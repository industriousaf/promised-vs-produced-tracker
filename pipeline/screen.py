"""
screen.py -- Screen stage operations (`screen_extracted` + `screen_check`).

Screen pt 1 (`screen_extracted`): the extraction result -- one project row in
the 20-column v0_out shape, always at verification_tier 'P'. Extraction problems
belong in the `flag` cell, never dropped or guessed.

Screen pt 2 (`screen_check`): the deterministic, computer-based verification.
Running it is just calling schema_check.check_row() (which *is*
schema.py) against a stored row and persisting the verdict
plus a pointer back to the row it judged.
"""

from __future__ import annotations

import json
import sqlite3

from pipeline import settings as criteria
from pipeline.db import now_iso
from pipeline.dates import (
    enrich as enrich_dates, interpret_date, DATE_TRIPLES, PRODUCED_UNDATED,
)
from pipeline.schema_check import (
    V0_COLUMNS,
    INT_COLUMNS,
    NULL_STRINGS,
    DATE_COLUMN_NULL_STRINGS,
    DERIVED_DATE_COLUMNS,
    RAW_DATE_COLUMNS,
    check_row,
    check_url,
    PROMISED_SENTINELS,
)


def _coerce(col: str, value) -> object:
    """Normalise a cell for storage: '' -> NULL; the two int columns -> int.

    A missing value written as text ('None', 'null', 'undefined') is blanked
    here, at the door, rather than stored and flagged later. It is not data --
    it is what a serializer emits when handed nothing -- and stored as-is it
    reads as a populated cell to everything downstream. The two first-output
    columns exempt only the overlap with the date sentinels: there 'n/a' is a
    documented DATE_SENTINEL meaning "no calendar date", a real answer -- but
    'None' is not, and must be blanked there too.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            return None
        bad = (DATE_COLUMN_NULL_STRINGS
               if col in ("promised_first_output", "actual_first_output")
               else NULL_STRINGS)
        if value.lower() in bad:
            return None
    if col == "status" and isinstance(value, str):
        # One spelling per word, so "Under Construction" is counted with the rest.
        return " ".join(value.lower().split())
    if col in INT_COLUMNS and value is not None:
        s = str(value).replace(",", "").replace("_", "").replace("$", "").strip()
        if s == "":
            return None
        try:
            return int(float(s)) if "." in s else int(s)
        except ValueError:
            # Leave un-parseable numerics as text so the checker flags them.
            return str(value)
    return value


class DuplicateExtraction(Exception):
    """A Screen row already exists for this Source lead."""


class RemovalBlocked(Exception):
    """The Screen row cannot be removed, because something downstream cites it."""


def extracted_for_source(conn: sqlite3.Connection, source_collected_id: int) -> list[int]:
    """Screen row ids already extracted from a given Source lead."""
    return [r["id"] for r in conn.execute(
        "SELECT id FROM screen_extracted WHERE source_collected_id = ? ORDER BY id",
        (source_collected_id,),
    ).fetchall()]


def distinct_project_count(conn: sqlite3.Connection) -> int:
    """How many distinct projects Screen holds, not how many rows.

    A lead extracted twice is one project, and the collection loop stops when it
    has added enough PROJECTS -- so this, not COUNT(*), is what it must count.
    Rows with no lineage (added by hand, source_collected_id NULL) cannot be
    compared to anything, so each counts once: -id gives every one of them a key
    of its own that can never collide with a real lead id.
    """
    return int(conn.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM screen_extracted "
        "GROUP BY COALESCE(source_collected_id, -id))"
    ).fetchone()[0])


def remove_extracted(conn: sqlite3.Connection, screen_id: int) -> dict:
    """Delete one Screen row and the checks that judged it. Returns what went.

    Refuses when the row has been published: a Verify row names the Screen row
    it came from, and deleting it would leave published research data pointing
    at nothing. Retract the Verify row first if that is really the intent.
    """
    row = get_extracted(conn, screen_id)
    if row is None:
        raise ValueError(f"no screen_extracted row with id {screen_id}")
    published = [r["id"] for r in conn.execute(
        "SELECT id FROM verify_verified WHERE screen_extracted_id = ?", (screen_id,)
    ).fetchall()]
    if published:
        raise RemovalBlocked(
            f"screen #{screen_id} was published as verify "
            f"#{', #'.join(str(v) for v in published)}. Removing it would leave "
            "that published row citing a source row that no longer exists."
        )
    n_checks = conn.execute(
        "DELETE FROM screen_check WHERE screen_extracted_id = ?", (screen_id,)
    ).rowcount
    conn.execute("DELETE FROM screen_extracted WHERE id = ?", (screen_id,))
    conn.commit()
    return {"id": screen_id, "project": row["project"], "checks": n_checks}


def insert_extracted(
    conn: sqlite3.Connection,
    row: dict,
    source_collected_id: int | None = None,
    replace: bool = False,
) -> int:
    """Insert one `screen_extracted` row. Returns its id.

    `row` is a mapping of (some of) the 20 v0 columns. Missing columns become
    NULL. verification_tier is forced to 'P' -- Screen is provisional by
    construction, so whatever the extractor claimed is overridden here.

    One Source lead extracts to one Screen row. A second attempt on the same
    lead raises DuplicateExtraction unless `replace` is set, in which case the
    earlier row (and its checks) are removed first. The N=20 run is why: a row
    failed its check over a bad cell, the extractor corrected the JSON and added
    it again, and both copies stayed -- one project counted twice, with no way
    to delete either. Refusing here is what stops that from being possible.
    """
    if source_collected_id is not None:
        existing = extracted_for_source(conn, source_collected_id)
        if existing and not replace:
            raise DuplicateExtraction(
                f"source #{source_collected_id} is already extracted as screen "
                f"#{', #'.join(str(e) for e in existing)}. To correct that row, "
                f"re-add with --replace; to keep both, they are not the same "
                f"project and one of them has the wrong --source-id."
            )
    else:
        existing = []

    values = {c: _coerce(c, row.get(c)) for c in V0_COLUMNS}
    values["verification_tier"] = "P"  # invariant at this stage

    # Stamp the phase that admitted this row, and default the country to the
    # phase's own when the extractor did not give one. Both are forced rather
    # than trusted: the extractor is reporting on a project, not on which sweep
    # it belongs to, and a row that lies about its phase makes every later
    # comparison between sweeps wrong.
    crit = criteria.active()
    values["criteria_id"] = crit.id
    if not values.get("country") and len(crit.countries) == 1:
        values["country"] = crit.countries[0]
    # Deterministically derive the *_dt columns and the float lag/slip from the
    # normalized date tokens -- whatever the extractor put in lag_years/slip_years
    # is overwritten here so two models that agree on the dates agree on lag/slip.
    values = enrich_dates(values)

    # Store the verbatim source text of each date (the raw -> token -> dt chain).
    # If the caller supplied no distinct verbatim capture, fall back to the
    # normalized token so the raw cell still reflects what was extracted: the API
    # path provides real verbatim; seed/manual paths reuse the token string.
    for raw_col, token_col, _dt_col in DATE_TRIPLES:
        raw_val = _coerce(raw_col, row.get(raw_col))
        values[raw_col] = raw_val if raw_val is not None else values.get(token_col)

    all_cols = list(V0_COLUMNS) + list(DERIVED_DATE_COLUMNS) + list(RAW_DATE_COLUMNS)
    cols = ["datetime", "source_collected_id"] + all_cols
    placeholders = ", ".join("?" for _ in cols)
    params = [now_iso(), source_collected_id] + [values[c] for c in all_cols]

    cur = conn.execute(
        f"INSERT INTO screen_extracted ({', '.join(cols)}) VALUES ({placeholders})",
        params,
    )
    conn.commit()

    # Supersede only once the replacement is safely in. Removing first looked
    # tidier and was wrong: this INSERT can still fail (a NOT NULL column the
    # corrected JSON forgot, say), and by then the row being corrected would
    # already be deleted -- a bad row replaced by no row at all. Caught in
    # testing, on exactly that failure.
    for old_id in existing:
        remove_extracted(conn, old_id)

    return int(cur.lastrowid)


# What `mark_first_output_unresolved` writes into `flag`. A fixed phrase rather
# than free prose, so "we looked and there is no dated source" is greppable and
# can never be mistaken for "nobody has looked yet". Both are undated rows; only
# one of them is worth spending another search on.
UNRESOLVED_MARKER = "no dated first-output source found"


class DateOverwriteBlocked(Exception):
    """The Screen row already carries a dated actual first output."""


def published_as(conn: sqlite3.Connection, screen_id: int) -> list[int]:
    """Verify row ids that were promoted from this Screen row."""
    return [r["id"] for r in conn.execute(
        "SELECT id FROM verify_verified WHERE screen_extracted_id = ?", (screen_id,)
    ).fetchall()]


def _refuse_if_published(conn: sqlite3.Connection, screen_id: int, project: str) -> None:
    """A published row is frozen at Screen. `remove_extracted` already refuses
    on the same ground: `verify_verified` holds a COPY of the cells, not a live
    reference, so a Screen write under a published row silently leaves the
    published copy saying something else. Verify is a human-only gate, so the
    fix is a person running `verify-edit`, not this command reaching past it."""
    published = published_as(conn, screen_id)
    if not published:
        return
    ids = ", #".join(str(v) for v in published)
    raise RemovalBlocked(
        f"screen #{screen_id} ({project}) was published as verify #{ids}. "
        f"Writing the date here would fix the Screen row and leave the "
        f"published one reading 'unconfirmed'. Verify is a human gate:\n"
        f"    tracker.py verify-edit --id {published[0]} "
        f"--set actual_first_output=YYYY-MM --set actual_date_source=URL "
        f"--desc \"first output dated from <source>\""
    )


def _append_flag(existing, addition: str) -> str:
    """Add a sentence to `flag` without destroying what is already there.

    Twenty-one of the twenty-two undated rows carry an explanation the extractor
    wrote about why the date is missing. That text is the reason the cell is
    empty; overwriting it to record the fix would delete the evidence that the
    fix was needed.
    """
    prior = (existing or "").strip()
    if not prior:
        return addition
    return f"{prior} {addition}" if prior.endswith((".", ";", "!")) else f"{prior}. {addition}"


def _first_output_update(conn: sqlite3.Connection, screen_id: int,
                         changes: dict) -> dict:
    """Apply `changes` to one Screen row, re-deriving every computed date cell.

    `enrich` is the single place lag/slip and the *_dt cells are ever computed,
    so this reads the stored row, overlays the changes, and hands the whole thing
    back through it -- rather than writing a date and computing the arithmetic
    a second way here.
    """
    row = get_extracted(conn, screen_id)
    if row is None:
        raise ValueError(f"no screen_extracted row with id {screen_id}")

    before = {"actual_first_output": row["actual_first_output"],
              "lag_years": row["lag_years"], "slip_years": row["slip_years"]}

    values = row_to_v0_dict(row)
    for col, val in changes.items():
        values[col] = _coerce(col, val)
    values = enrich_dates(values)

    cols = ["actual_first_output", "actual_first_output_raw", "actual_date_source",
            "flag", "actual_first_output_dt", "lag_years", "slip_years"]
    conn.execute(
        f"UPDATE screen_extracted SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
        [values.get(c) for c in cols] + [screen_id],
    )
    conn.commit()
    return {"id": screen_id, "project": row["project"], "before": before,
            "after": {"actual_first_output": values["actual_first_output"],
                      "lag_years": values["lag_years"],
                      "slip_years": values["slip_years"]}}


def set_first_output(conn: sqlite3.Connection, screen_id: int, date: str,
                     source: str, raw: str | None = None,
                     note: str | None = None, force: bool = False) -> dict:
    """Put a dated first output, and the URL that dates it, on one Screen row.

    Touches four cells and no others: `actual_first_output`, its `*_raw`
    partner, `actual_date_source`, and `flag` -- plus the three the pipeline
    derives from them (`actual_first_output_dt`, `lag_years`, `slip_years`).

    This exists because the only other way to change a stored row is
    `screen-add --replace`, which takes the whole row. A row in the backfill
    population has ~20 correct cells and one wrong one, and restating all 20 to
    correct 1 is how the other 19 get damaged.

    Three refusals, all of them the backfill's own failure modes:
      * a `date` that does not resolve to a calendar date -- writing a sentinel
        here would record "we found the date" while leaving the row undated,
      * a `source` that is not URL-shaped -- the citation is the entire point,
      * a row that already has a real date, unless `force`. Every row in the
        population has `actual_first_output_dt IS NULL`, so landing on a dated
        one means the id is wrong.
    """
    iso, kind = interpret_date(date)
    if kind != "date":
        raise ValueError(
            f"--date {date!r} resolves to {kind!r}, not a calendar date. This "
            f"command records a date that was found; to record that none was "
            f"found, use --unresolved."
        )
    if msg := check_url(source or ""):
        raise ValueError(f"--source: {msg}")
    if not (source or "").strip():
        raise ValueError("--source is required: a date with no citation is a guess.")

    row = get_extracted(conn, screen_id)
    if row is None:
        raise ValueError(f"no screen_extracted row with id {screen_id}")
    _refuse_if_published(conn, screen_id, row["project"])
    if row["actual_first_output_dt"] and not force:
        raise DateOverwriteBlocked(
            f"screen #{screen_id} ({row['project']}) already has "
            f"actual_first_output={row['actual_first_output']!r} resolving to "
            f"{row['actual_first_output_dt']}. Pass --force only if that stored "
            f"date is wrong."
        )

    said = f"first output dated {date} from actual_date_source"
    return _first_output_update(conn, screen_id, {
        "actual_first_output": date,
        "actual_first_output_raw": raw or date,
        "actual_date_source": source.strip(),
        "flag": _append_flag(row["flag"], f"Resolved: {said}." + (f" {note}" if note else "")),
    })


def mark_first_output_unresolved(conn: sqlite3.Connection, screen_id: int,
                                 note: str) -> dict:
    """Record that a first-output date was searched for and not found.

    The date cells are left exactly as they are; only `flag` gains the standard
    marker plus why. A row that was never searched and a row that was searched
    without success look identical in the data otherwise, and they call for
    different next moves -- one wants another search, the other wants a person.
    """
    row = get_extracted(conn, screen_id)
    if row is None:
        raise ValueError(f"no screen_extracted row with id {screen_id}")
    _refuse_if_published(conn, screen_id, row["project"])
    if not (note or "").strip():
        raise ValueError("--unresolved needs a reason: what was searched, and what was found instead.")
    return _first_output_update(conn, screen_id, {
        "flag": _append_flag(row["flag"], f"{UNRESOLVED_MARKER}: {note.strip()}"),
    })


def undated_produced(conn: sqlite3.Connection,
                     include_searched: bool = False) -> list[sqlite3.Row]:
    """Rows that HAVE produced but carry no date for it -- the backfill queue.

    Not the same set as "no actual_first_output_dt": that also holds every
    project still waiting to produce (-1.0) and every cancelled one (-2.0).
    This is only PRODUCED_UNDATED (-4.0) -- an event whose date is missing.
    Largest capital first, the same order review proceeds in.

    A published row is left out always: it is frozen at Screen, so the loop
    would pay for a search it could not record. `published_undated` lists those
    separately -- they are a person's work, through `verify-edit`.

    A row already carrying UNRESOLVED_MARKER is left out by default. It stays
    at -4.0 either way -- marking it changes no date cell -- so without this the
    queue would re-offer every dead end on every run, and the second search
    would cost exactly what the first one did to learn the same thing. Pass
    `include_searched` to retry them anyway.
    """
    rows = conn.execute(
        "SELECT * FROM screen_extracted WHERE lag_years = ? "
        "ORDER BY CAST(NULLIF(promised_capital_usd, '') AS INTEGER) DESC "
        "NULLS LAST, id",
        (PRODUCED_UNDATED,),
    ).fetchall()
    rows = [r for r in rows if not published_as(conn, r["id"])]
    if include_searched:
        return rows
    return [r for r in rows if UNRESOLVED_MARKER not in (r["flag"] or "")]


def row_to_v0_dict(row: sqlite3.Row) -> dict:
    """Extract just the 20 v0 columns from a screen/verify row, as a plain dict.

    A column the stored table does not have yet reads as None rather than
    raising. `main()` migrates on every ordinary command, so the only way to see
    an un-migrated table is a read-only session -- where the alternative is a
    bare `IndexError: No item with that key` instead of the refusal the caller
    was going to get anyway.
    """
    have = set(row.keys())
    return {c: (row[c] if c in have else None) for c in V0_COLUMNS}


def run_check(conn: sqlite3.Connection, screen_extracted_id: int) -> dict:
    """Run the deterministic checker over one screen row and persist the result.

    Returns the check dict (result_status/n_errors/n_warnings/report) and writes
    a `screen_check` row pointing back at screen_extracted_id.
    """
    src = conn.execute(
        "SELECT * FROM screen_extracted WHERE id = ?", (screen_extracted_id,)
    ).fetchone()
    if src is None:
        raise ValueError(f"no screen_extracted row with id {screen_extracted_id}")

    # Judge the row by the rule that ADMITTED it, not by whatever is active now.
    # Otherwise lowering the threshold for a later sweep would silently re-grade
    # every earlier row, and raising it would fail rows that were properly in
    # scope when they were collected.
    result = check_row(row_to_v0_dict(src), criteria.get(src["criteria_id"]
                                                         if "criteria_id" in src.keys() else None))

    conn.execute(
        """
        INSERT INTO screen_check
            (datetime, screen_extracted_id, result_status, n_errors, n_warnings, report)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            now_iso(),
            screen_extracted_id,
            result["result_status"],
            result["n_errors"],
            result["n_warnings"],
            json.dumps(result["report"]),
        ),
    )
    conn.commit()
    return result


def latest_check(conn: sqlite3.Connection, screen_extracted_id: int) -> sqlite3.Row | None:
    """The most recent checker run for a given screen row (or None)."""
    return conn.execute(
        """
        SELECT * FROM screen_check
        WHERE screen_extracted_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (screen_extracted_id,),
    ).fetchone()


def review_queue(conn: sqlite3.Connection) -> dict:
    """What is waiting for a person, and what is in the way.

    Returns {"ready": [rows], "blocked": [rows], "published": int}. `ready` is
    largest capital first, which is the order review is meant to proceed in: the
    Tracker is made complete from the top down, so wherever review stops, the
    claim above that point is intact. `blocked` is the rows whose deterministic
    check FAILs -- promotion refuses those until the row is fixed.

    Every surface that tells a human what to do next asks this, so `status`, the
    bare-invocation landing page, the web dashboard and `review` itself cannot
    quote different numbers at the same person on the same database.
    """
    published = {r["project"] for r in conn.execute(
        "SELECT project FROM verify_verified")}
    ready, blocked = [], []
    for row in list_extracted(conn, by_capital=True):
        if row["project"] in published:
            continue
        chk = latest_check(conn, row["id"])
        (blocked if (chk and chk["result_status"] == "FAIL") else ready).append(row)
    return {"ready": ready, "blocked": blocked, "published": len(published)}


def list_extracted(conn: sqlite3.Connection,
                   by_capital: bool = False) -> list[sqlite3.Row]:
    """Screen rows, by id (insertion order) or largest capital first.

    Capital order is how verification is meant to proceed: the Tracker is made
    complete from the top down, so wherever review stops, the claim above that
    point is intact. promised_capital_usd is TEXT, so it is cast for sorting and
    rows without a figure sort last."""
    if by_capital:
        return conn.execute(
            "SELECT * FROM screen_extracted "
            "ORDER BY CAST(NULLIF(promised_capital_usd, '') AS INTEGER) DESC "
            "NULLS LAST, id"
        ).fetchall()
    return conn.execute("SELECT * FROM screen_extracted ORDER BY id").fetchall()


def published_undated(conn: sqlite3.Connection) -> list[tuple[sqlite3.Row, int]]:
    """(verify row, verify id) for PUBLISHED rows that still carry no date.

    The backfill cannot touch these, and they are the ones that matter most:
    'unconfirmed' in `verify_verified` is on the published Tracker, not in a
    staging table. Returned so the run says so at the end instead of leaving
    them to be noticed.

    Keyed on the Verify row's own lag_years, not the Screen row it came from.
    Verify holds a copy, so fixing the published row through `verify-edit`
    leaves the Screen row at -4.0 forever -- and keying on Screen meant a row
    that had already been fixed kept being reported as outstanding. TSMC Fab 1
    was: verify #8 carried a 2024-Q4 date and lag 4.5 while this function was
    still naming it as work to do.
    """
    return [(r, r["id"]) for r in conn.execute(
        "SELECT * FROM verify_verified WHERE lag_years = ? ORDER BY id",
        (PRODUCED_UNDATED,),
    ).fetchall()]


def get_extracted(conn: sqlite3.Connection, screen_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM screen_extracted WHERE id = ?", (screen_id,)
    ).fetchone()


# --------------------------------------------------------------------------- #
# The human gate: one person, one field, one cited page                        #
# --------------------------------------------------------------------------- #

# The fields a person settles by hand before promoting. Defined here rather than
# in the web app because which fields need a human eye is a methodology fact,
# and because two copies of a list like this is the bug this repository keeps
# rediscovering. The web app imports it.
#
# `project`, `sector`, `state` and the source URLs are not here: they are either
# the row's identity or the thing being read from, not a claim to check against
# it. `current_status` is, because a plant's status is exactly the kind of
# sentence a cited page either supports or does not.
ATTESTABLE_FIELDS = (
    "announced",
    "promised_first_output",
    "actual_first_output",
    "promised_capital_usd",
    "promised_jobs",
    "current_status",
)

ATTEST_STATES = ("confirmed", "not_in_source")

# Values recording that no cited source stated a field: the promised-date
# sentinel `n/a`, and an empty field. The two buttons record what the page
# shows, so for these "not in this source" IS the verification, and "confirmed"
# is refused -- it would say a page shows a value the field says does not exist.
#
# `pending`, `never` and `unconfirmed` are deliberately not here. Each describes
# something a page does show -- construction still under way, a cancellation, a
# plant running with no start date -- so they are confirmed like any other
# value, and zero literal hits for them is expected rather than suspicious.
ABSENCE_VALUES = frozenset({""} | set(PROMISED_SENTINELS))


def is_absence(value) -> bool:
    """True when a field's value records that no source stated it."""
    return ("" if value is None else str(value)).strip().lower() in ABSENCE_VALUES


def field_value(row, field: str) -> str:
    """One field as the string an attestation stores and later compares against.

    Both the write and the staleness check go through here. Storing `str(row[f])`
    at one end and comparing `row[f]` at the other would report every integer
    cell as changed the moment it was read back.
    """
    v = row[field] if field in row.keys() else None
    return "" if v is None else str(v)


# Columns that identify or date a record itself rather than describe the
# project. A published project's view keeps these from Screen, so every address
# built from the Screen id -- the checklist, the pane, the attest route -- still
# points where it did.
_IDENTITY_COLUMNS = frozenset({"id", "datetime", "created_at",
                               "source_collected_id", "screen_extracted_id"})


def review_view(conn: sqlite3.Connection, screen_id: int) -> tuple[dict | None, int | None]:
    """The record a reviewer should see for a Screen project, and its Verify id.

    Unpublished, that is the Screen record. Published, it is the Screen record
    with every content column taken from the published one, because a project
    can be corrected on its way out and the checklist has to vouch for what went
    out. Before this, a published project's inspect page showed the extractor's
    values, so a field corrected at verification could only be settled against
    the value the correction replaced.

    The Screen id and lineage are kept. Returns (None, None) for no such project.
    """
    row = get_extracted(conn, screen_id)
    if row is None:
        return None, None
    view = {k: row[k] for k in row.keys()}
    pub = conn.execute(
        "SELECT * FROM verify_verified WHERE screen_extracted_id = ? "
        "ORDER BY id DESC LIMIT 1", (screen_id,)).fetchone()
    if pub is None:
        return view, None
    for k in pub.keys():
        if k in view and k not in _IDENTITY_COLUMNS:
            view[k] = pub[k]
    return view, pub["id"]


def attest(conn: sqlite3.Connection, screen_extracted_id: int, field: str,
           state: str, attested_by: str, source_url: str | None = None,
           tab_index: int | None = None, match_count: int | None = None,
           value: str | None = None) -> int:
    """Record that `attested_by` settled one field. Returns the new row's id.

    Append-only: re-settling a field writes another row and the newest one
    counts.

    `value` is the value being vouched for -- what was in the field on screen
    when the button was pressed. It used to be read from the stored row, on the
    theory that the database is what can be trusted, and that was wrong about
    what the row is for: a reviewer who corrects a field vouches for the
    correction, and the stored Screen value is exactly what they are replacing.
    Seven of the first eight corrections published a value nobody had confirmed,
    while this table said the old one had been. Evidence is a different matter
    and still never comes from the caller: the URL and the hit count are what the
    server saw. Omitted, it falls back to the stored value, which is right for
    any caller that does not edit.
    """
    if field not in ATTESTABLE_FIELDS:
        raise ValueError(f"{field!r} is not one of the checklist fields")
    if state not in ATTEST_STATES:
        raise ValueError(f"{state!r} is not one of {ATTEST_STATES}")
    if not criteria.may_verify(attested_by):
        raise ValueError(
            f"{attested_by!r} is not on the verifier list. Add the address to "
            f"VERIFIERS in {criteria.where('VERIFIERS')} before attesting.")
    # The published record when there is one, so a settle with no value sent
    # vouches for what went out rather than the value it replaced.
    row, _published = review_view(conn, screen_extracted_id)
    if row is None:
        raise ValueError(f"no screen_extracted row #{screen_extracted_id}")
    held = field_value(row, field) if value is None else str(value).strip()
    if state == "confirmed" and is_absence(held):
        raise ValueError(
            f"{field} records that no source stated it, so there is nothing on "
            f"the page to confirm. Settle it as not in this source. If the page "
            f"does state it, enter the value first, then confirm.")

    cur = conn.execute(
        """
        INSERT INTO screen_attested
            (datetime, screen_extracted_id, field, state, value_at_time,
             source_url, tab_index, match_count, attested_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (now_iso(), screen_extracted_id, field, state, held,
         source_url or None, tab_index, match_count, attested_by.strip()),
    )
    conn.commit()
    return cur.lastrowid


def attestations(conn: sqlite3.Connection, screen_extracted_id: int) -> dict:
    """{field: row} -- the newest attestation for each field of one project."""
    out = {}
    for r in conn.execute(
        """
        SELECT * FROM screen_attested
        WHERE screen_extracted_id = ?
        ORDER BY id
        """,
        (screen_extracted_id,),
    ):
        out[r["field"]] = r          # later rows overwrite earlier ones
    return out


def settled_since(conn: sqlite3.Connection, screen_extracted_id: int, since: str,
                  by: str | None = None) -> set[str]:
    """The fields of one project settled after `since`, a UTC ISO timestamp, by
    `by` when given. The web app uses it to tell whether a model check has been
    acted on: its fields settled after it was asked."""
    sql = ("SELECT DISTINCT field FROM screen_attested "
           "WHERE screen_extracted_id = ? AND datetime > ?")
    params: list = [screen_extracted_id, since]
    if by:
        sql += " AND attested_by = ?"
        params.append(by)
    return {r[0] for r in conn.execute(sql, params)}


def attestation_state(conn: sqlite3.Connection, screen_extracted_id: int) -> dict:
    """{field: {"state", "stale", "match_count", "source_url", "by", "when"}}.

    `stale` means the field has been edited since it was settled, so the
    confirmation is about a value the row no longer holds. The interface shows
    those as needing another look instead of leaving them ticked.

    For a published project the comparison is with the published record, so a
    settle on the value a correction replaced reads as stale, which it is.
    """
    row, _published = review_view(conn, screen_extracted_id)
    if row is None:
        return {}
    out = {}
    for field, a in attestations(conn, screen_extracted_id).items():
        out[field] = {
            "state": a["state"],
            "stale": (a["value_at_time"] or "") != field_value(row, field),
            "value": a["value_at_time"],
            # Confirmed an absence. attest() refuses this now, but rows written
            # before the rule exist; the page shows them for re-settling rather
            # than rewriting them, because the table is append-only.
            "confirmed_absence": (a["state"] == "confirmed"
                                  and is_absence(a["value_at_time"])),
            "match_count": a["match_count"],
            "source_url": a["source_url"],
            "by": a["attested_by"],
            "when": a["datetime"],
        }
    return out


def needs_resettle(conn: sqlite3.Connection) -> dict[int, list[str]]:
    """{screen_id: [fields]} -- every project with a field to settle again.

    Two kinds: a settle on a value the project no longer holds (stale), and a
    "confirmed" on a field that records nothing was stated. Fields keep checklist
    order, so the list reads the way the page does. The Screen list counts these
    and filters to them; before it did, finding them took a query.
    """
    out: dict[int, list[str]] = {}
    for (sid,) in conn.execute(
            "SELECT DISTINCT screen_extracted_id FROM screen_attested ORDER BY 1"):
        state = attestation_state(conn, sid)
        fields = [f for f in ATTESTABLE_FIELDS
                  if f in state and (state[f]["stale"] or state[f]["confirmed_absence"])]
        if fields:
            out[sid] = fields
    return out
