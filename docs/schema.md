# The schema

Six SQLite tables in `outputs/scoreboard.db`. The canonical definition is
[`../pipeline/schema.py`](../pipeline/schema.py) — where
this file and that one disagree, the code wins.

Six SQL tables, a row's trust climbing left→right:

```
 SOURCE                SCREEN pt1           SCREEN pt2              VERIFY
 source_collected  →   screen_extracted →   screen_check           verify_verified
 links + summary       v0_out shape         schema.py result       v0_out shape
 (AI / human)          tier = P             + FK to screen         tier = V1/V2
                                            screen_attested
                                            what a person settled
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


## What the check returns

`screen_check.result_status` is one of three tokens. **`CLEAN` is the best of
them, then `PASS`, then `FAIL`.** The names do not sort that way and have been
read backwards, so the ordering is stated here rather than left to inference.

The verdict is derived from two severities, which `schema.py` assigns per cell
and nothing else in the repository defines:

- **`ERROR`** — the row is not structurally admissible. A bad type, a value
  outside a closed vocabulary, a missing `announced` anchor, a duplicate key, or
  figures clearing neither half of the inclusion floor.
- **`WARN`** — the row is admissible and something is still open for a person.
  In practice this is almost always an unresolved `flag`; the other case is a
  verified tier carrying no inline sources.

From those, in `pipeline/schema_check.py`:

| verdict | condition | `screen_check` counts | blocks `verify-promote` |
|---|---|---|---|
| `FAIL` | one or more `ERROR` | `n_errors` > 0 | yes, unless `--force` |
| `PASS` | no `ERROR`, one or more `WARN` | `n_warnings` > 0 | no |
| `CLEAN` | neither | both 0 | no |

`report` holds the issues as JSON, one object per issue, each with its column,
level and message.

### Why a `PASS` is promotable

A warning is not a thing to clear before the gate. It is the note you read *at*
the gate. The common warning is an open `flag`, and no command in the CLI or the
web app edits a flag on a Screen row — `verify-promote` is the only thing that
writes one, rewriting it into a resolution record on the way through. So
"resolve the warning first" would name a step that does not exist.

`schema.py --strict` does treat warnings as failures, but that is for validating
a CSV by hand before it is loaded, where editing the file *is* the workflow. The
Verify gate does not run strict.

### What none of the three can tell you

Whether the sources support the figures. The checker never opens a link. That is
the human gate's whole job, and it is why a `CLEAN` row is still unpublished
until a person promotes it. See [`verify_methods.md`](verify_methods.md).

## What a person confirmed

`screen_check` is the machine's opinion of a project's shape. `screen_attested`
is the person's, and it is the record behind every promotion: one row per field
settled, naming who settled it, which page they had open, and how many times the
pane found that value on that page.

| column | what it holds |
|---|---|
| `field` | one of the six checklist fields, rejected otherwise |
| `state` | `confirmed`, or `not_in_source` when the page does not carry it |
| `value_at_time` | what the field said when it was settled |
| `source_url` | the page the pane had open |
| `match_count` | hits the pane found there; `NULL` when it was never opened |
| `attested_by` | an address from `VERIFIERS` in `settings.py` |

Three properties are the point of the table.

**Append-only.** Re-settling a field writes another row and the newest one
counts, so someone changing their mind leaves both. Overwriting would hide the
one thing worth seeing.

**`value_at_time` makes staleness visible.** Edit a field after confirming it and
the interface shows it as needing another look rather than leaving it ticked, so
a confirmation always refers to a value the project still holds.

**`match_count` is what makes the record hard to wave away.** Nothing here proves
anyone read anything, and the checkbox is trivially tickable. What the table
stores instead is what the machine saw beside what the person asserted: a
confirmation recorded against a page where the value never appeared reads as
zero, and anyone can find those.

The address comes from a list in `settings.py`, not a text box. A box accepts
`asdf@asdf.com` as readily as a real address, so a typed identity is exactly as
forgeable as a typed name while looking more convincing to a reader. Nothing
here authenticates anyone either, and the data should be described as
self-identified. What the list buys is that adding someone is a deliberate act
by the project, and that there is nothing to typo.

It does not gate promotion. The command line has no such gate, so blocking one
of two doors would imply a stronger claim than the data supports.

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
- **Sectors are a closed vocabulary:** the checker ERRORs on a sector outside it. Add a
  genuinely new manufacturing sector by editing `SECTORS` in `pipeline/settings.py` — a
  code change on purpose, so what counts as in scope cannot move at runtime without a
  commit recording it. (`schema.py` only re-exports the set; editing it there does
  nothing.)
- **`criteria_id` names the rule that admitted the row.** The Scoreboard is built by
  sweeping at a high threshold first and lowering it, and every row records which
  sweep admitted it — `1B-or-2000-jobs`, `100M-or-200-jobs`. The name is the rule
  spelled out rather than a handle like `p1`, because it lives in a CSV column
  where it cannot be reworded later. A row is checked against the rule that
  admitted it, never against whatever is active now.
- **Source exclusion covers both stages:** the collector is steered away from projects
  already in `verify_verified` (the pipeline's authority for "already have it") *and*
  from those collected but not yet published, so a run does not re-find what it just
  found.
