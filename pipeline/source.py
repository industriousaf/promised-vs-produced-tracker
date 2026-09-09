"""
source.py -- Source stage operations (`source_collected`).

A Source row is a raw *lead*: two required source links (promise + status), an
optional promised-date link, and an optional context summary. No extraction, no
typing, no verification happens here -- that is entirely the later stages' job.
This module is deliberately thin: insert a lead, list leads, fetch one.
"""

from __future__ import annotations

import sqlite3

from pipeline.criteria import active as _active_criteria
from pipeline.db import now_iso
from pipeline.schema_check import NULL_STRINGS


def _clean(value: str | None) -> str | None:
    """Trim, and treat a missing value written as text as the absence it is.

    Source cells are URLs and prose, so 'None' or 'null' here is never a real
    answer -- it is str(None) from whatever handed the lead over. Blanking it at
    the door keeps a cell that only looks populated out of the database.
    """
    v = (value or "").strip()
    if not v or v.lower() in NULL_STRINGS:
        return None
    return v


def insert_lead(
    conn: sqlite3.Connection,
    promise_source: str,
    status_source: str,
    promised_date_source: str | None = None,
    summary: str | None = None,
    collected_via: str | None = None,
) -> int:
    """Insert one `source_collected` lead. Returns its new id.

    Only promise_source and status_source are required (necessary); the other
    three are optional. `collected_via` is a provenance label naming the entry
    path that produced this lead (prompt1 | prompt2 | seed | api | manual);
    NULL means unrecorded. Duplicates are permitted by design -- there is no
    unique constraint, because two collectors filing the same links is
    tolerated; dedup is a Screen/Verify concern, not Source's.

    Every lead is stamped with the inclusion phase that was searching when it was
    found. A lead collected under a $1B sweep and one collected under a later
    $100M sweep are not the same evidence about what exists, and once they are
    mixed in one table nothing else can tell them apart.
    """
    promise_source = (promise_source or "").strip()
    status_source = (status_source or "").strip()
    if not promise_source or not status_source:
        raise ValueError("promise_source and status_source are both required")

    cur = conn.execute(
        """
        INSERT INTO source_collected
            (datetime, promise_source, status_source, promised_date_source, summary,
             collected_via, criteria_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now_iso(),
            promise_source,
            status_source,
            _clean(promised_date_source),
            _clean(summary),
            _clean(collected_via),
            _active_criteria().id,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def screened_map(conn: sqlite3.Connection) -> dict[int, list[int]]:
    """{source lead id: [screen row ids extracted from it]}.

    A lead missing from this map has never been screened, which is the only
    question the Screen stage needs to ask of a lead before picking it up.
    """
    out: dict[int, list[int]] = {}
    for child, parent in conn.execute(
        "SELECT id, source_collected_id FROM screen_extracted "
        "WHERE source_collected_id IS NOT NULL ORDER BY id"
    ):
        out.setdefault(int(parent), []).append(int(child))
    return out


def unscreened_leads(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Leads with no Screen row yet -- the Screen stage's actual work queue.

    Screen used to find these by listing every lead and cross-referencing the
    Screen table by eye, which costs turns and fills the context with rows it is
    not going to touch. One lead is one Screen row (see screen.insert_extracted),
    so "has a Screen row" is the whole test.
    """
    return conn.execute(
        "SELECT * FROM source_collected s WHERE NOT EXISTS "
        "(SELECT 1 FROM screen_extracted e WHERE e.source_collected_id = s.id) "
        "ORDER BY s.id"
    ).fetchall()


def list_leads(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM source_collected ORDER BY id"
    ).fetchall()


def get_lead(conn: sqlite3.Connection, lead_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM source_collected WHERE id = ?", (lead_id,)
    ).fetchone()
