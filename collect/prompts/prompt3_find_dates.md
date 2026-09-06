# PROMPT 3 — Find the first-output date for one Screen row

You are running as a single Claude Code call. You have been shown
`docs/cli.md`, the pipeline's command reference. Do **only** what this prompt says.

**Where to run:** from the `scoreboard/` directory —
`python3 -m pipeline.cli ...`.

**You must use web search / fetch.** Record only what a source you actually read
states — never a date from in-model knowledge, however confident you are.

## The one row

The loop has given you exactly one `screen_extracted` id in the user message.
Work that row and stop. Read it first:

```
python3 -m pipeline.cli screen-show --id <the id>
```

## What is wrong with it

That row's `actual_first_output` is `unconfirmed`, which in this schema means
**the plant HAS produced, but no cited source gives a date for it**. Its
`current_status` will say so — `IN FULL OPERATION`, `PRODUCING`, `AT VOLUME`.
Its `flag` usually explains why the date is missing, and that explanation is
almost always the same one:

> the status source proves the plant is running *today* and simply never says
> when it started

That is not an extraction error. It is a source that cannot carry two facts. A
Q4-2025 earnings release proves a mill is at volume and can never also date its
2021 first coil. **Your job is to find the second source** — the one that dates
first output — and cite it.

## Your job

1. **Read the row.** Note the project, the state, the announced date, and the
   promised first-output date. The promise is a strong prior for where to look:
   a plant promised for 2022 usually produced within a year or two of that.

2. **Search for the first-output event.** The phrasings that actually date it:
   *first coil*, *first slab*, *first vehicle*, *first cell*, *first module*,
   *start of production*, *SOP*, *began commercial production*, *rolled off the
   line*, *ribbon cutting* (only if the source says production began, not that
   the building opened). Company press releases and trade press are best; local
   news is often the only outlet that covered the day itself.

3. **Judge what you found**, and take exactly one of three exits:

   **(a) You found a dated source.** Record it:

   ```
   python3 -m pipeline.cli screen-date --id <id> \
       --date "2021-12" \
       --source "https://…" \
       --raw "the verbatim sentence the date appears in"
   ```

   `--date` is the normalized token in the same shapes the Screen prompt uses:
   `2021`, `2021-12`, `2022-Q3`, `2023 (first half)`, `2022-12-30`. Keep every
   bit of precision the source gives and no more — do not turn "in 2022" into
   `2022-06`. `--raw` is the exact sentence, copied, not paraphrased. The
   pipeline computes `lag_years` and `slip_years` from the token; do not
   calculate them.

   **(b) You searched and no source dates it.** Say so, with what you looked at:

   ```
   python3 -m pipeline.cli screen-date --id <id> \
       --unresolved "Searched company releases and trade press 2022-2024; all confirm operation, none dates first output."
   ```

   **(c) The question itself is wrong for this row.** Some plants never produced
   the thing they promised. The promised product was cancelled and the site
   makes something else; a mothballed mill was restarted rather than started;
   the "plant" is an expansion of a line that was already running. "First
   output" has no single answer there, and picking one silently is worse than
   leaving it open. Use `--unresolved` and **say which of those it is**:

   ```
   python3 -m pipeline.cli screen-date --id <id> \
       --unresolved "Promised LCD display panels; site produces data servers. First output of the promised product never occurred — needs a human decision on what the row should measure."
   ```

## What not to do

- **Do not date a different thing.** A later product line is not first output:
  a plant that began building R1 in 2021 and R2 in 2026 has a first output of
  2021, and a source that only dates the R2 is exit (b), not a date.
- **Do not settle for the plant's *opening*.** "Opened", "completed",
  "commissioned" and "began hiring" are not production. If the source says the
  facility opened in March and is "slated to produce" later, that is not a
  first-output date.
- **Do not touch any other cell.** `screen-date` is the only write in this
  prompt. Never `screen-add`, never `screen-remove`, never `verify-promote`.
- **Do not guess to avoid an empty answer.** Exit (b) is a *good* outcome. It
  records that a person looked, which is information the row does not have now.

## When a page will not load

`pipeline/prompts/fetching.md` is the ladder: WebFetch, then curl
with a browser user-agent, then a `web.archive.org` snapshot. Work down it before
giving up on a source, and note in `--note` or the `--unresolved` reason which
step you had to use.

That is the whole call. One row, one `screen-date`, then **stop**.
