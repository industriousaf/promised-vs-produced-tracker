"""
verify.py -- Verify stage operations (`verify_verified` + `verify_edits`).

Verify is the published, research-grade tracker. A row only reaches here once a
human (later: a verifier agent) has confirmed the two independent sources
actually support the claims -- so verification_tier is V1 (one source per
load-bearing cell) or V2 (two independent sources), never P. This
promotion is the human gate: the whole point of the pipeline.

Two invariants this module enforces (the Verify stage's whole purpose):
  * `flag` is REWRITTEN on promotion into a "resolution record" (what was fixed
    vs. what stays open), drawing on both Screen tables.
  * Every UPDATE to a verify_verified row writes a verify_edits row in the SAME
    transaction -- the audit log can never drift from the table it audits.
"""

from __future__ import annotations

import sqlite3

from pipeline import settings as criteria
from pipeline.db import now_iso
from pipeline.dates import enrich as enrich_dates, DATE_TRIPLES
from pipeline.schema_check import (
    V0_COLUMNS,
    INT_COLUMNS,
    DERIVED_DATE_COLUMNS,
    RAW_DATE_COLUMNS,
    DATE_SENTINELS,
    SENTINELS_FOR,
    check_row,
)
from pipeline.screen import _coerce, row_to_v0_dict, latest_check, get_extracted

# Tiers admissible on a published row -- P alone is never one of them.
# V1 = confirmed against one source, V2 = against two independent sources.
VERIFY_TIERS = {"V1", "V2", "V1/P", "V2/P", "V1/V2"}

# Derived cells are computed by pipeline/dates.py from the date strings -- never
# hand-edited, so they can't drift from the strings they summarise.
DERIVED_FIELDS = {"lag_years", "slip_years"} | set(DERIVED_DATE_COLUMNS)
DATE_STRING_COLUMNS = {"announced", "promised_first_output", "actual_first_output"}

# Columns a human may edit on a Verify row (identity/lineage AND derived excluded).
#
# RAW_DATE_COLUMNS is here deliberately. It used to be absent -- not by decision
# but because this list was derived from V0_COLUMNS alone, and the *_raw cells
# are a pipeline-stage addition that sits outside the v0 shape. The effect was
# that a human could correct a date at the Verify gate and could NOT correct the
# verbatim quote the date was supposedly read from. Verify #8 (TSMC Arizona Fab
# 1) ended up reading actual_first_output = '2024-Q4' with
# actual_first_output_raw = 'unconfirmed': the cell, its resolved date and its
# citation all correct, and the one field claiming to be the source text saying
# the opposite. For a Tracker whose whole argument is provenance, that is the
# worst cell to have wrong.
EDITABLE_COLUMNS = [c for c in list(V0_COLUMNS) + list(RAW_DATE_COLUMNS)
                    if c not in DERIVED_FIELDS]

# token -> its verbatim partner, for the consistency notice in edit().
_RAW_PARTNER = {token: raw for raw, token, _dt in DATE_TRIPLES}


class CorrectionRefused(ValueError):
    """A correction that would add a checker error to a published project."""


def prepare_changes(base_row, changes: dict) -> dict:
    """Coerce, normalise and check a set of corrections against `base_row`.

    Returns the cleaned changes, including any recomputed derived dates, or
    raises. Every door that publishes a correction ends here: the verify button on
    the inspect page, the Verify edit page and `tracker.py verify-edit`. They used
    to skip the checker entirely. A misspelt `penidng`, an `n/a` in the
    actual-output field and a `pending` in the promised field were each stored as
    typed, and each left a published record that fails it.

    Only errors the correction introduces are refused, judged by column, so a
    record published with force=True that already fails can still be corrected.
    The whole record is re-checked rather than the edited field alone because the
    size floor judges capital and jobs together and reports on only one of them.

    A sentinel typed with capitals or spaces is the same word, so it is stored in
    its one spelling. It passes the checker either way, which is why it had to be
    done here: nothing else would have stopped "Pending" reaching the CSV.
    """
    clean: dict[str, object] = {}
    for col, val in changes.items():
        if col in DERIVED_FIELDS:
            raise ValueError(
                f"{col!r} is derived (computed from the date strings) -- edit "
                "announced / promised_first_output / actual_first_output instead"
            )
        if col not in EDITABLE_COLUMNS:
            raise ValueError(f"{col!r} is not an editable Verify column")
        value = _coerce(col, val)
        if (col in SENTINELS_FOR and isinstance(value, str)
                and value.strip().lower() in DATE_SENTINELS):
            value = value.strip().lower()
        clean[col] = value

    before = row_to_v0_dict(base_row)
    merged = dict(before)
    merged.update({c: v for c, v in clean.items() if c in merged})

    # If a date string changed, recompute the derived *_dt + lag/slip from the
    # merged record so they never drift from the strings.
    if DATE_STRING_COLUMNS & set(clean):
        enriched = enrich_dates(dict(merged))
        for c in ("announced_dt", "promised_first_output_dt",
                  "actual_first_output_dt", "lag_years", "slip_years"):
            clean[c] = enriched[c]
            if c in merged:
                merged[c] = enriched[c]

    keys = base_row.keys() if hasattr(base_row, "keys") else ()
    crit = criteria.get(base_row["criteria_id"] if "criteria_id" in keys else None)
    had = {i["column"] for i in check_row(before, crit)["report"] if i["level"] == "ERROR"}
    new = [i for i in check_row(merged, crit)["report"]
           if i["level"] == "ERROR" and i["column"] not in had]
    if new:
        raise CorrectionRefused("; ".join(f"{i['column']}: {i['message']}" for i in new))
    return clean


class PromotionBlocked(Exception):
    """Raised when a screen row cannot be promoted (e.g. its check FAILs)."""


def promote(
    conn: sqlite3.Connection,
    screen_extracted_id: int,
    verification_tier: str,
    flag: str | None = None,
    overrides: dict | None = None,
    force: bool = False,
) -> int:
    """Promote a screen_extracted row to verify_verified. Returns the new verify id.

    This is the human gate. By default a row may only be promoted if its most
    recent screen_check is PASS or CLEAN (structurally admissible); a FAIL blocks
    promotion unless `force=True`.

    * `verification_tier` must be a verified tier (V1/V2, optionally slash-P).
    * `flag` becomes the Verify row's resolution record; if omitted, a default
      resolution note is written -- `flag` is always rewritten on promotion.
    * `overrides` lets the human correct individual cells at the moment of
      promotion (e.g. fix an `announced` date the checker flagged).
    """
    tier = (verification_tier or "").strip()
    if tier not in VERIFY_TIERS:
        raise ValueError(
            f"verify tier must be one of {sorted(VERIFY_TIERS)} (got {tier!r})"
        )

    src = get_extracted(conn, screen_extracted_id)
    if src is None:
        raise ValueError(f"no screen_extracted row with id {screen_extracted_id}")

    chk = latest_check(conn, screen_extracted_id)
    if chk is None and not force:
        raise PromotionBlocked(
            f"screen row {screen_extracted_id} has no screen_check yet -- "
            "run the check first, or promote with force=True"
        )
    if chk is not None and chk["result_status"] == "FAIL" and not force:
        raise PromotionBlocked(
            f"screen row {screen_extracted_id} FAILs its schema check "
            f"({chk['n_errors']} error(s)) -- fix it or promote with force=True"
        )

    # Build the verify row from the screen cells, applying human overrides.
    values = row_to_v0_dict(src)
    # Carry the verbatim *_raw date cells (not part of the v0 shape) through to
    # Verify unchanged -- they are provenance, so promotion never rewrites them.
    for raw_col in RAW_DATE_COLUMNS:
        values[raw_col] = src[raw_col] if raw_col in src.keys() else None
    for k, v in (overrides or {}).items():
        if k in V0_COLUMNS:
            values[k] = _coerce(k, v)

    values["verification_tier"] = tier

    if flag is not None:
        values["flag"] = flag.strip() or None
    else:
        # On promotion `flag` stops meaning "raw extraction problems" and
        # starts meaning "what the human fixed vs. what is still open".
        prior = (values.get("flag") or "").strip()
        note = f"Resolved: human-verified and promoted from screen_extracted #{screen_extracted_id}"
        if prior and not prior.lower().startswith(("none", "resolved", "n/a")):
            note += f". Prior extraction flag: {prior[:120]}"
        values["flag"] = note

    # Coerce ints one more time in case an override arrived as text.
    for c in INT_COLUMNS:
        values[c] = _coerce(c, values.get(c))
    # Recompute the derived *_dt + float lag/slip from the (possibly overridden)
    # date strings, so Verify is standardized identically to Screen.
    values = enrich_dates(values)

    ts = now_iso()
    all_cols = list(V0_COLUMNS) + list(DERIVED_DATE_COLUMNS) + list(RAW_DATE_COLUMNS)
    cols = ["datetime", "created_at", "screen_extracted_id"] + all_cols
    placeholders = ", ".join("?" for _ in cols)
    params = [ts, ts, screen_extracted_id] + [values[c] for c in all_cols]

    try:
        cur = conn.execute(
            f"INSERT INTO verify_verified ({', '.join(cols)}) VALUES ({placeholders})",
            params,
        )
    except sqlite3.IntegrityError as e:
        conn.rollback()
        if "UNIQUE" in str(e).upper():
            raise PromotionBlocked(
                f"project {values.get('project')!r} is already in verify_verified "
                "(one published row per project) -- edit the existing row instead"
            ) from e
        raise
    conn.commit()
    return int(cur.lastrowid)


def edit(
    conn: sqlite3.Connection,
    verify_verified_id: int,
    changes: dict,
    edit_description: str,
) -> list[str]:
    """Apply an edit to a Verify row AND log it, atomically. Returns any notices.

    `changes` maps editable columns to new values. `edit_description` is the
    provenance note. The verify_verified.datetime (last-modified) is bumped, and a
    verify_edits row is written in the same transaction -- the two always move
    together.

    Returns a list of human-readable notices about the edit -- currently one per
    date token changed without its `*_raw` partner. That case is NOT refused,
    because both readings are legitimate: the quote may have been misread (the
    raw was right, the token wrong) or a different source may now be in play (the
    raw needs replacing). Only the person making the edit knows which, so the
    command asks rather than guesses.
    """
    if not edit_description or not edit_description.strip():
        raise ValueError("edit_description is required (data provenance)")

    row = conn.execute(
        "SELECT * FROM verify_verified WHERE id = ?", (verify_verified_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no verify_verified row with id {verify_verified_id}")

    # Coerced, normalised, derived dates recomputed and held to the checker, in
    # the one function the inspect page also calls before it publishes.
    clean = prepare_changes(row, changes)

    # Guard: an edit must not demote a Verify row back to provisional.
    if "verification_tier" in clean:
        tier = (clean["verification_tier"] or "")
        if tier not in VERIFY_TIERS:
            raise ValueError(
                f"verification_tier on Verify must stay verified ({sorted(VERIFY_TIERS)})"
            )

    # A date token that moved without its verbatim partner. Surface it here,
    # while the person who made the change is still looking.
    notices: list[str] = []
    for token, raw_col in _RAW_PARTNER.items():
        if token in clean and raw_col not in clean:
            stored = row[raw_col] if raw_col in row.keys() else None
            notices.append(
                f"{token} changed to {clean[token]!r} but {raw_col} still reads "
                f"{(stored or '(empty)')!r}. If that quote does not support the new "
                f"value, correct it:  --set {raw_col}=\"the sentence it came from\""
            )

    ts = now_iso()
    try:
        if clean:
            set_clause = ", ".join(f"{c} = ?" for c in clean)
            conn.execute(
                f"UPDATE verify_verified SET {set_clause}, datetime = ? WHERE id = ?",
                [*clean.values(), ts, verify_verified_id],
            )
        else:
            conn.execute(
                "UPDATE verify_verified SET datetime = ? WHERE id = ?",
                (ts, verify_verified_id),
            )
        conn.execute(
            """
            INSERT INTO verify_edits (datetime, verify_verified_id, edit_description)
            VALUES (?, ?, ?)
            """,
            (ts, verify_verified_id, edit_description.strip()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return notices


def list_verified(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM verify_verified ORDER BY id").fetchall()


def get_verified(conn: sqlite3.Connection, verify_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM verify_verified WHERE id = ?", (verify_id,)
    ).fetchone()


def list_edits(conn: sqlite3.Connection, verify_verified_id: int | None = None) -> list[sqlite3.Row]:
    if verify_verified_id is None:
        return conn.execute("SELECT * FROM verify_edits ORDER BY id").fetchall()
    return conn.execute(
        "SELECT * FROM verify_edits WHERE verify_verified_id = ? ORDER BY id",
        (verify_verified_id,),
    ).fetchall()
