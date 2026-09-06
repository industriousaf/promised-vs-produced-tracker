# Screen stage prompt — extract

Operating prompt for the **Screen pt 1** step of the pipeline (the
Silver-equivalent stage; see "Why three stages" in `README.md`).
Give this to the AI (or human) doing extraction.
Input is **one `source_collected` lead** (its source links + summary); output is **one
`screen_extracted` row** in the 18-column `promised_vs_produced_v0_out.csv` shape, at
`verification_tier = P`.

Your output is checked next by `screen_check` (which runs
`promised_vs_produced_schema.py`), so it must be **schema-admissible** — the rules below
are exactly what that checker enforces. Everything below the line is the prompt.

---

You extract structured rows from sources. You are given one lead: a `promise_source`
link, a `status_source` link, an optional `promised_date_source` link, and a short
summary. **Open those links, read them, and fill in one project row** with the 18 core
fields below — **plus, for each of the three dates, a `*_raw` verbatim partner** (see
*Date interpretation*). Extract only what the sources actually state; where a source can't
be read or a value can't be found, record that in `flag` rather than guessing. **One lead in, one row out** — never merge projects.

## The 18 fields (schema of `screen_extracted`)

The table's `id` and `datetime` are added automatically — don't supply them. If you have
the originating Source row's id, include it as `source_collected_id` for lineage
(optional).

| Field | Rule (this is what the checker enforces) |
|---|---|
| `project` | Non-empty name of the facility/project, e.g. `TSMC Fab 1 Phoenix`. Unique per project. |
| `sector` | **A defined manufacturing sector (standardized).** Classify into one of the sectors in the *Sector vocabulary* list appended below (the base set is `Aerospace and Defense`, `Auto Assembly`, `Battery`, `Chemicals and Plastics`, `Food and Beverage`, `Machinery`, `Pharmaceuticals`, `Semiconductors`, `Solar`, `Steel`, `Other`. `Pharmaceuticals` covers drug substance, API and biologics plants; `Solar` covers cell and panel manufacturing; `Other` is for a manufacturing project that genuinely fits none of them). The list is **closed**: copy one of those strings exactly. Never invent a sector, coin a narrower label, edit `SECTORS`, or run `sectors-add` / `register_sector()` — extending the vocabulary is a human decision, not yours. If nothing fits, use `Other` and name the candidate in `flag` (e.g. `Other used; candidate new sector: Cement`). A sector outside the vocabulary is an ERROR. |
| `state` | 2-letter US postal abbreviation (e.g. `AZ`, `TX`). |
| `announced` | **Normalized token — strict `YYYY-MM`** — the announcement month. This is the row's anchor and the denominator for lag/slip, so it must be exact and match the source. No fuzzy values. (The pipeline stores its resolved date as `announced_dt`.) |
| `announced_raw` | **The exact source text** the `announced` date was read from, copied **verbatim** (e.g. `announced the project in May 2020`). Provenance only — never parsed. |
| `promised_capital_usd` | Integer US dollars, digits only in the value (no `$`, `,`, or words). e.g. `12000000000`. Use the **initial** announcement's figure; do **not** sum later re-announcements/expansions (see re-announcement discipline below). |
| `promised_jobs` | Integer, digits only. Count **direct** promised jobs only — not "regional," "supported," "induced," or construction jobs (record `2000`, not a `10000` regional claim). |
| `promised_first_output` | **Normalized token:** a 4-digit year (optionally narrowed to a month/quarter/qualifier — and **to the exact day when the source states one**, e.g. `2024`, `2025 (first half)`, `2024-Q4`, `2025-03`, `2022-12-30`) **or** a sentinel: `pending`, `never`, `unconfirmed`, `n/a`, `tbd`, `open`. Keep whatever precision the source gives — the pipeline honours a full `YYYY-MM-DD` and resolves anything coarser to its healthy-middle `promised_first_output_dt`. |
| `promised_first_output_raw` | **The exact source text** the promised-date token was read from, copied **verbatim** (e.g. `production is slated to begin in the first half of 2025`). Provenance only — never parsed. |
| `actual_first_output` | **Normalized token** — same rule as `promised_first_output`: a real first-output date if it has produced, else a sentinel (`pending` if not yet, `never` if cancelled). Resolved to `actual_first_output_dt`. |
| `actual_first_output_raw` | **The exact source text** the actual-date token was read from, copied **verbatim**. Provenance only — never parsed. |
| `current_status` | Non-empty short free-text status (e.g. `AT VOLUME`, `DELAYED; …`, `PRODUCING slow ramp`). |
| `lag_years` | **Do not supply — the pipeline computes it** deterministically (see *Date interpretation* below). A float: years from `announced` to `actual_first_output`; sentinel `-1` ("to be completed") if not produced yet, `-2` ("cancelled") if the promise was cancelled. |
| `slip_years` | **Do not supply — the pipeline computes it.** A float: years from `promised_first_output` to `actual_first_output` (negative = early); same `-1`/`-2` sentinels. |
| `verification_tier` | **Always `P`** at this stage. Nothing is verified here. |
| `notes` | Free-text context (caveats, renegotiations, related events). Optional but useful. |
| `promise_source` | The URL(s) from the Source lead backing the promise. Multiple allowed, separated by `;` or spaces. |
| `status_source` | The URL(s) backing the current status. |
| `flag` | Extraction problems / discrepancies (see below). Omit the key if the row extracted cleanly — never the word `None`. |
| `promised_date_source` | Optional URL specifically supporting the promised date, if provided by the lead. |
| `actual_date_source` | Optional URL specifically supporting the **actual** first-output date. Use it when `status_source` proves the plant is running today but does not say when it started — a 2025 earnings release cannot date a 2021 first coil. Leave it out when `status_source` carries both facts. |

## Date interpretation — the `*_raw` → token → `*_dt` chain, and how lag/slip are standardized

To make the schema reproducible (a *different* extractor that agrees on the tokens must
produce the *same* lag/slip), the pipeline — **not you** — resolves each date to a concrete
DATETIME and does the arithmetic. Every date cell therefore keeps **three** columns, and
you supply the **first two**:

1. **`*_raw` — the verbatim source text.** Copy the date **exactly as the page states it**,
   with no cleanup: `the first half of 2025`, `sometime next year`, `Q4 '24`. This is the
   audit trail — *what the source actually said* — so it must be a faithful quote, not your
   interpretation. Never parsed; store it as-is even when it's messy or a whole clause.
2. **The normalized token** (`announced`, `promised_first_output`, `actual_first_output`) —
   your **clean, parseable** reading of that raw text, in the shapes above (`2020-05`,
   `2025 (first half)`, `2024-Q4`, or a sentinel). This is the *only* cell the deterministic
   resolver reads, so keep it a single unambiguous date expression (one year, not a
   sentence). If the raw text is already clean, the token is just that text tidied to the
   canonical shape.
3. **`*_dt` — the resolved ISO date**, computed by the pipeline from the token (below).

If you omit a `*_raw`, the pipeline falls back to storing the token as the raw — so always
provide the real verbatim quote when you can; that is the whole point of this field.

Coarser tokens resolve to the **middle** of their window (a bare year → mid-year, a
quarter → its midpoint, `YYYY-MM` → the 15th), so keep every bit of precision the source
actually gives: `2024-Q4` carries information a bare `2024` throws away. The resolution
rules and the lag/slip arithmetic are implemented in `pipeline/dates.py`.

Your job is only to extract each date's **verbatim `*_raw`** and its **clean token**
accurately (and surface any date discrepancy in `flag`). Don't compute or supply lag/slip
or the `*_dt` cells yourself — if you do, they're overwritten.

## The size floor still applies

A row is out of scope unless `promised_capital_usd ≥ 100,000,000` ($100M) **OR**
`promised_jobs ≥ 200` — clearing **either** floor is enough (a row is out only if it falls
below **both**). Leads from Source should already meet this; if what you extract clears
neither floor, put that in `flag` — the row will fail the check. (Apply the same
**direct-jobs** rule as `promised_jobs` above when judging the 200 floor.)

## Re-announcement discipline (one site = one project)

A site announced once and then re-announced several times (e.g. `$12B → $40B → $65B →
$165B`) is **one project with one anchor**, not several. Use the **original**
announcement's `announced` date and `promised_capital_usd`; put the later
expansions/re-announcements in `notes` (e.g. "later expansions $40B/$65B/$165B are
re-announcements per rule 4") — never sum them into `promised_capital_usd`.

## Using the `flag` column

`flag` is where extraction problems surface — the row still gets written, the problem is
just recorded:

- A source can't be opened / scraped / 404s → note which one and that the affected field
  is unconfirmed.
- A needed value isn't in the sources → say which field and leave it as the best-supported
  value or a sentinel.
- A **discrepancy** between sources, or between a source and the lead → state it plainly
  (e.g. "announcement is dated 2019-03-27, not 2019-01"). Surface contradictions; do not
  silently pick a side.
- Clean extraction → **omit the key entirely**, or give it an empty string. Do not
  write the word `None`, `null`, or `n/a`: that is a missing value wearing the
  costume of a present one, and every check downstream reads it as real content.
  The same goes for any other cell you have nothing for — `promised_date_source`
  with no separate document is omitted, not filled in with `None`.

## Principles

- **Extract, don't infer.** Record what the sources state. Don't fill gaps with outside
  knowledge or characterize outcomes beyond what's cited.
- **Prefer honest over complete.** Being explicit about what is shaky (via `flag`) is worth
  more than a row that looks finished.

## Output format

Return exactly one JSON object with the extractable keys (plus optional
`source_collected_id`). For each date give **both** the normalized token **and** its
`*_raw` verbatim partner. **Omit** `lag_years`, `slip_years`, the `*_dt` columns, and
`verification_tier` — the pipeline derives lag/slip and the `*_dt` dates, and forces the
tier to `P`. Example shape:

```json
{
  "source_collected_id": 42,
  "project": "TSMC Fab 1 Phoenix",
  "sector": "Semiconductors",
  "state": "AZ",
  "announced": "2020-05",
  "announced_raw": "TSMC announced the Phoenix fab in May 2020",
  "promised_capital_usd": 12000000000,
  "promised_jobs": 1600,
  "promised_first_output": "2024",
  "promised_first_output_raw": "with production targeted for 2024",
  "actual_first_output": "2024-Q4",
  "actual_first_output_raw": "began high-volume production in the fourth quarter of 2024",
  "current_status": "AT VOLUME",
  "notes": "Workforce ramp delays reported 2023–2025.",
  "promise_source": "https://…",
  "status_source": "https://…",
  "flag": "promise_source 403s to WebFetch; read via web.archive.org snapshot",
  "promised_date_source": "https://…",
  "actual_date_source": "https://…"
}
```

`flag` is shown here carrying a real value, because that is the case worth seeing. A
clean extraction leaves the key out altogether. Return only JSON — no comments, no
trailing commas, nothing outside the object.
