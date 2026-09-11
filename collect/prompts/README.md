# Operating prompts for the collection loops

This folder holds the prompts that grow the Tracker's data. The loop scripts
one directory up start a fresh `claude -p` process per
iteration and hand it exactly one of these files.

```
  PROMPT 1 (open-web discovery, most-recent status) ──► source ─► PROMPT 2 ─► screen
```

- `prompt1_collect_recent.md` — discover one **new** qualifying project by web
  search, skipping everything already collected. The status ("produced") source
  must be the most recent one available as of today. Driven by
  `source.sh`.
- `prompt2_extract_screen.md` — extract one Source lead into a Screen row using
  the pipeline's own rendered Screen prompt, then run the deterministic check.
  Driven by `screen.sh`. It extracts any Source lead,
  whatever put the lead there.

Both files deliberately **reference the canonical prompts rather than restating
their rules**: `source-prompt` renders `prompt_source_collected.md` plus the live
exclusion lists, and `screen-prompt` renders `prompt_screen_extracted.md` plus
the sector vocabulary and the specific lead. Those two rendered prompts are the
authority on what qualifies and how a row is shaped. This file is orientation
only; nothing here overrides them.

**Contents**

- [How each process is configured](#how-each-process-is-configured)
- [Constraints when writing or running these prompts](#constraints-when-writing-or-running-these-prompts)

## How each process is configured

The loop scripts set all of this. The values below describe what they do, so if
they ever disagree, the scripts are correct.

| | |
|---|---|
| Reference context | `docs/cli.md`, attached to the process as an `@`-mention |
| Model | set in `pipeline/settings.py`; `MODEL=` overrides every stage for one run, `SOURCE_MODEL=` / `SCREEN_MODEL=` one stage. `python3 tracker.py models` says what is in effect. |
| Effort | `high` (`EFFORT` overrides). The CLI has no "extra high"; `high` is the ceiling. |
| Chat history | none. Every iteration starts fresh, remembering nothing. |
| Scope | run only what the given prompt file says |

## Constraints when writing or running these prompts

- Pipeline commands run from the repository root
  (`python3 -m pipeline.cli ...`), per
  [`docs/cli.md`](../../docs/cli.md).
- Reference `prompt_source_collected.md` and `prompt_screen_extracted.md` through
  `source-prompt` / `screen-prompt` instead of restating their rules.
- Every prompt must run the real pipeline code. No invented commands.
- Every prompt must use web fetch and search, not the model's own recollection.
- Promotion to Verify is a human gate and is never part of these prompts.
