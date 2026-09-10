# Collection runs

One row per stage per run. The three data tables say *what* was collected; this
says *how* — which model, how many iterations and turns, what they cost, and how
many Source leads or Screen rows came out.

Figures are as each run reported them (`collect/tally.py --summary`).

## Runs

| run (UTC) | stage | model | effort | iters | turns | tokens | minutes | added |
|---|---|---|---|---|---|---|---|---|
| 20260905T083552Z | SOURCE | claude-opus-4-8 | high | 6 | 249 | 11,774,584 | 48.5 | 30 leads |
| 20260905T083552Z | SCREEN | claude-opus-4-8 | high | 30 | 382 | 11,243,755 | 43.3 | 30 rows |
| 20260905T150042Z | SOURCE | claude-opus-4-8 | high | 12 | 487 | 26,874,878 | 113.9 | 60 leads |
| 20260905T150042Z | SCREEN | claude-opus-4-8 | high | 60 | 832 | 26,265,358 | 105.5 | 60 rows |
| 20260906T182230Z | SOURCE | claude-opus-4-8 | high | 20 | 797 | 41,601,601 | 158.4 | 100 leads |
| 20260906T182230Z | SCREEN | claude-opus-4-8 | high | 100 | 1338 | 42,529,218 | 154.9 | 100 rows |
| 20260907T070342Z | DATES | claude-opus-4-8 | high | 29 | 310 | 12,463,605 | 45.0 | — |
| 20260910T032533Z | SCREEN | claude-opus-4-8 | high | 20 | 293 | 14,475,414 | 40.6 | 20 rows |

**187,228,413 tokens across all five runs.**

| run | what it was | totals | after |
|---|---|---|---|
| 20260905T083552Z | N=30 collection | 36 iters, 631 turns, 91.8 min, $29.32 | Screen holds 53 rows |
| 20260905T150042Z | N=60 collection | 72 iters, 1319 turns, 219.5 min, $68.12 | Screen holds 112 rows |
| 20260906T182230Z | N=100 collection | 120 iters, 2135 turns, 313.3 min, $106.45 | Screen holds 212 rows |
| 20260907T070342Z | first-output date backfill | 29 iters, 310 turns, 45.0 min, $17.66 | 24 of 29 undated rows dated |
| 20260910T032533Z | N=20 Screen-only, first run at the $1B floor | 20 iters, 293 turns, 40.6 min, $18.26 | Screen holds 20 rows |

The DATES run changed no row counts. It filled `actual_first_output` on Screen
rows that had produced but carried no date for it, and cited each in
`actual_date_source`.

Web search runs on a second model, and its tokens are included above:

| run | claude-opus-4-8 | claude-haiku-4-5 | web searches |
|---|---|---|---|
| 20260905T083552Z | 20,933,112 | 2,085,227 | 110 |
| 20260905T150042Z | 48,834,289 | 4,305,947 | 259 |
| 20260906T182230Z | 77,151,137 | 6,979,682 | 391 |
| 20260910T032533Z | 14,072,223 | 403,191 | 5 |

The 5 on the last row is not an anomaly. That run was SCREEN only, and
extraction reads the two links already stored on a Source lead rather than
searching for new ones. The three-figure counts above it all belong to runs
that included a SOURCE stage, which is where searching happens.

## Source yield per iteration

A Source call may return up to a **per-call ceiling**, set by `LEADS_PER_CALL` in
`pipeline/settings.py` and reported in each run's header. It is a ceiling, not a
quota: the prompt is explicit that returning fewer, even zero, beats loosening a
threshold to reach the number. **Every run below ran at a ceiling of 5**, which
is what makes the yields readable — a 5 means the call hit the ceiling, and
anything less means it ran out of qualifying projects before reaching it.

Leads added per Source iteration:

| run | Source held at start | yield |
|---|---|---|
| 20260905T083552Z | 25 leads | 5 5 5 5 5 5 |
| 20260905T150042Z | 55 leads | 5 5 5 5 5 5 5 5 5 5 5 5 |
| 20260906T182230Z | 115 leads | 5 5 5 5 5 5 5 5 5 5 5 5 5 5 5 5 5 5 5 5 |

**Thirty-eight consecutive iterations at the ceiling, never once short**, from 25
leads to 215. Search cost held flat across all three runs: 41.5, 40.6 and 40.4
turns per iteration. If qualifying projects were running out, the model would be
spending more turns to reach the same five. It was not.

Per-lead cost across those runs: **~416K tokens and ~8 turns**. That is the
number any change to the ceiling has to beat.

A separate scan of all 212 Screen rows for near-duplicate projects in the same
state returned one candidate pair, which on inspection is two genuinely different
projects at one site complex (US Steel Big River 2, a 2022 mill, and a 2026 DRI
facility). Nothing in the pipeline suppresses duplicates — `UNIQUE (project)` is
an exact-string constraint and applies only at Verify — so they simply were not
occurring.

**What this does and does not establish.** The inclusion criteria in
[`README.md`](../README.md#what-counts-as-a-project) did not bind at 215 Source
leads, and no run reached a wall. Each stopped at its target `N`, not at
exhaustion, so these runs put a floor under the ceiling rather than finding it.
Measuring the true ceiling needs a run whose target cannot be reached, ended by
`MAX_STALL` instead.

## Counting tokens correctly

A run's result object reports usage in two places and **the obvious one is
wrong**. Top-level `usage` covers the main model only; the web-search model's
tokens never appear there. `modelUsage` has one entry per model actually used,
and that is what the figures above sum.

The gap is not small. On the N=100 run, top-level `usage` gives 77,151,137
against a true 84,130,819 — 8% missing. On an earlier N=20 run it was 12,426,236
against 13,430,362.

Anything recomputing these figures should sum `modelUsage`, or simply call
`collect/tally.py`, which is the one implementation of this and carries the same
warning in its module docstring.

## What is kept here, and what is not

Each run writes three files: a transcript (`.log`), a per-iteration token ledger
(`-usage.jsonl`), and under `VERBOSE=1` the raw stream events (`.raw.jsonl`).

None of them are tracked — `.gitignore` excludes `logs/*.log` and `logs/*.jsonl`.
This file is the deliberate substitute: the figures a reader would want, in a
form a reader would open, without committing megabytes of model narration that
nobody reads. The files themselves stay on the machine that ran them.

Anything published from this data should cite this file. If a figure here is ever
disputed, the run that produced it is reproducible from
[`collect/`](../collect/) and the prompts committed alongside it.
