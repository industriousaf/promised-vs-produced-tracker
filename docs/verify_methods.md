# Screen → Verify: the human review

Verify is the published, research-grade tracker. A Screen row is always tier **P**
(provisional) — an extraction nobody has vouched for yet. A Verify row is tier
**V1** or **V2** — a human has opened the sources and confirmed they hold up.
Making that judgment *is* the human gate; nothing reaches Verify without it.

The machine has already done its part. `screen-check` (Screen pt-2) checks the
**shape** of a row: required cells present, `announced` is a real `YYYY-MM`
anchor, sector/state in vocabulary, the inclusion floor of the row's own phase
is cleared, date cells parse, tiers are valid tokens, sources look like URLs, and
any open `flag` is surfaced. A `FAIL` there blocks promotion — fix it first.

So this review is **not** re-checking shape. It is the one thing the checker
can't do: reading each source and confirming it actually supports the claim.
`PASS`/`CLEAN` means "well-formed," not "true" — that last step is yours.

The web app's review screen (`/screen/N/inspect`) is built for exactly that step.
The cited pages render *in* the screen, one tab per link, with the row's claimed
values highlighted in them and arrows to step from one to the next; the check
panel lists every rule the checker applied against this row's values, and what it
cannot test; and an optional agentic check will open the links and say whether
they carry the value or where it actually is. All of it is a reading aid. None of
it promotes anything — see [`webapp/`](../webapp/README.md#the-review-screen).

## What a promised-vs-produced row is

One qualifying US manufacturing project tracked from the moment capital is promised
to the moment output actually appears. The **promise** side is the announcement
(capital, jobs, and a promised first-output date); the **produced** side is the
current/actual outcome. The gap between them is the whole point — the lag/slip
this tracker measures.

### The tiers measure how deeply a row was checked

| Tier | Means |
|---|---|
| `P` | provisional. Extracted, but nobody has confirmed it against a source. |
| `V1` | a person confirmed each load-bearing cell against **one** source. |
| `V2` | a person confirmed each load-bearing cell against **two independent** sources. |

"Independent" is about origin, not URL count: a company press release plus
independent trade coverage is two; the same wire story republished by two outlets
is one. A slash pair narrows the claim — `V1/P` means the announcement is
verified but first output is still provisional.

Every row has the *fields* for more than one source — `promise_source` (the
announcement), `status_source` (the outcome), and `promised_date_source` when the
promised date comes from a different document. How many of them a person actually
checked is what the tier records.

## What to look for

**1. Dates parse to what the source actually says.** `announced` (`YYYY-MM`) is
the anchor — every lag/slip figure is measured from it, so confirm it against the
announcement, not from memory. (Example caught in the drafts: Nucor's announcement
is *March* 2019, not January.) For the two output cells, read the verbatim
`*_raw` text against the derived `*_dt`: "first half of 2025" must not silently
become `2025-01` (Hyundai), and a promised *month* that only appears in a later
update needs its own `promised_date_source`, not the original release (Panasonic).
`lag_years` / `slip_years` are computed from the date strings — never hand-edit
them; if they look wrong, fix the date cell and they recompute.

**2. The produced side is a fair read of the status.** `actual_first_output` and
`current_status` together should give an honest picture of where the project
really is. It does **not** have to be the very latest headline — some staleness is
expected dataset noise — but it must be reasonable for what it is and not
contradicted by the promise. Renegotiations, delays, and "ramping / unconfirmed"
belong in `current_status` (and the `flag`), not swept away.

**3. It's genuinely a promise paired with a produced outcome.** A real
capital/jobs promise with a promised date on one side, and a source for what was
produced on the other. A bare announcement with no output signal, or two sources
that don't actually pair up, isn't ready — leave it in Screen.

## Promoting

Once the sources check out:

```
python3 -m pipeline.cli verify-promote --screen-id N --tier V1
```

Use `--set col=value` to correct a cell at promotion time (e.g. fix a date the
checker flagged), and `--tier V1/P` when the announcement is verified but first
output is still provisional. Use `V2` only when you checked two genuinely
independent sources.

Every change to a published row is written to `verify_edits` with a reason. The
one exception is a change to `flag` and nothing else: that writes its own reason,
quoting the old text and the new, because the flag *is* a statement of what is
unresolved and a second box saying "resolved the flag" recorded nothing. Change
any other cell — with or without the flag — and the reason is required.

A correction is also held to the checker before it is saved, at all three places
that make one: the verify button on the inspect page, the Verify edit page and
`tracker.py verify-edit`. One that would leave the published record failing the
checker is refused and nothing is written. A sentinel typed with capitals is
stored in its one spelling. On the inspect page the check runs before anything
is published, so a refused correction never leaves a project live with the value
it was replacing.

**The guided queue always writes V1.** `review` and the web app's inspect page
both show you the promised side and the produced side — two documents covering
two halves of one project, not two readings of one claim — so reading what they put in front of you
is one source, however many links you clicked. Neither screen helps you find a
corroborating source, so neither offers the choice: they used to ask, and the
honest answer was always V1. `V2` is a deliberate act. Go find the second source,
then publish that project with `verify-promote --screen-id N --tier V2` by
hand, where N is the Screen id. `--screen-id` is required, so the command
without it fails. The `flag` is rewritten into a resolution record on the way
in. A `FAIL`ing row is blocked unless you pass `--force` — reserve that for when
you've verified the row by hand and disagree with the checker.

## Collaborating on the Tracker

The tracker lives in one SQLite file, `tracker.db`. Three ways to share the work,
roughly in order of effort:

1. **Ping-pong the file.** We hand `tracker.db` back and forth and take turns.
   Zero setup; only one person can be verifying at a time, and merges are manual.
2. **Shared backend (preferred).** Put `tracker.db` behind an organized shared
   store so we both work against it. Better for real collaboration — a little more
   to stand up, but no hand-offs.
3. **Shared Claude account** for the project, so the AI collection/extraction
   steps run from one place regardless of who's driving.
