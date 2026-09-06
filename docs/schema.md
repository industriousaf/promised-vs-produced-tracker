# The schema

Five SQLite tables in `outputs/scoreboard.db`. The canonical definition is
[`../pipeline/schema.py`](../pipeline/schema.py) — where
this file and that one disagree, the code wins.

Five SQL tables, a row's trust climbing left→right:

```
 SOURCE                SCREEN pt1           SCREEN pt2              VERIFY
 source_collected  →   screen_extracted →   screen_check           verify_verified
 links + summary       v0_out shape         schema.py result       v0_out shape
 (AI / human)          tier = P             + FK to screen         tier = V1/V2
                            └─ human reads both screens, promotes ──┘  (human gate)
                                                                    verify_edits
                                                                    (every edit logged)
```

Each step works whether **AI or a human** does it, and — where the stage allows
— offers a **manual** path, a **Claude Code** path (no API key), and a **direct
Anthropic API** path. The design decisions left open by the original note are
baked in: cross-stage lineage FKs are present; Screen is
strictly tier `P`; Verify is one row per `project`; `verify_verified.datetime` is
last-modified with a separate `created_at`; `screen_check` is one row per run.

The store is **SQLite** (stdlib only) so it runs locally with zero services. The
column shapes match the Postgres design 1:1, so promoting to Supabase/Postgres
later is a mechanical translation.


## Details worth knowing

- **Reset:** delete `outputs/scoreboard.db` (or point `SCOREBOARD_DB` elsewhere)
  to start clean. Add `outputs/*.db` to `.gitignore` if you don't want to commit
  them.
- **Which tables per stage:** Source = `source_collected`; Screen =
  `screen_extracted` + `screen_check`; Verify = `verify_verified` + `verify_edits`.
- **`flag`'s lifecycle:** raw extraction problems in Screen → rewritten into a
  resolution record on promotion to Verify — `flag` stops meaning "raw
  extraction problems" and starts meaning "what the human fixed vs. what is
  still open".
- **Standardized dates (`*_raw` → token → `*_dt`):** for each date the extractor supplies
  the **verbatim source text** (`announced_raw` / `promised_first_output_raw` /
  `actual_first_output_raw`) *and* a clean normalized **token** (`announced` / …); the
  pipeline derives the `*_dt` DATETIMEs and the float `lag_years` / `slip_years` from the
  token (see `dates.py`). The `*_raw` cells are verbatim provenance (never parsed, carried
  unchanged into Verify); the `*_dt` and lag/slip cells are read-only in the Verify edit form —
  edit a date token and they recompute automatically. If no `*_raw` is supplied, the token
  is stored as the raw.
- **Two `*_date_source` columns, because one URL rarely carries two facts:**
  `promise_source` shows a promise was made and `promised_date_source` shows when
  it was for; `status_source` shows the plant runs today and `actual_date_source`
  shows when it first produced. Both date-source columns are optional — most rows
  need neither, because one link states both halves. Twenty-two of the first 112
  rows needed the actual-side one: a Q4-2025 earnings release proves a mill is at
  volume and can never also date its 2021 first coil, so recording the date meant
  overwriting the evidence of operation. `screen-date` is the writer.
- **Sectors are a defined, extensible vocabulary (not agnostic):** the checker ERRORs on a
  sector outside the vocabulary. Add a genuinely new manufacturing sector by editing
  `SECTORS` in `pipeline/schema.py` (Claude Code) or via `sectors-add` /
  `register_sector()` (API), which persists to `sector_registry.json`.
- **Source exclusion covers both stages:** the collector is steered away from projects
  already in `verify_verified` (the pipeline's authority for "already have it") *and*
  from those collected but not yet published, so a run does not re-find what it just
  found.
