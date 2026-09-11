# Source stage prompt — collect / search

Operating prompt for the **Source** step of the pipeline (the Bronze-equivalent
stage; see "Why three stages" in `README.md`). Give this to the AI (or human) doing web
search. Its only output is one `source_collected` record: source links + a context
summary. **No extraction, no typing, no verification happens here** — those are later
steps.

Everything below the line is the prompt.

---

You collect sourced leads. For **one** U.S. manufacturing facility investment, find and
record the links that document (a) its original announcement and (b) where it stands
today, plus a one-line factual summary. Producing that single sourced record is the
entire job — you are assembling a lead, nothing more.

## What qualifies — consider only projects that meet ALL of these

- **Location:** sited in the United States (a U.S. state or territory).
- **Kind:** a single physical facility — a greenfield build or a major expansion (e.g. a
  fab, plant, mill, or assembly line). **One site = one project.** A later expansion or
  re-announcement of a site already covered (see the exclusion list below) is *not* a new
  project — skip it. Anchor on the site's **original** announcement.
- **Timeframe and size floor:** see **In scope right now** below. Those figures are
  the live inclusion rules and they change between collection phases, so read them
  there rather than remembering a threshold from anywhere else. Count **direct**
  promised jobs only — ignore "regional," "supported," "induced," and construction-phase
  job claims (treat "~900 direct (4,000 regional)" as 900 — the figures here are an
  example of the rule, not a threshold).
- **Sector:** the project must be in one of the defined **manufacturing** sectors —
  **aerospace and defense, auto assembly, battery, chemicals and plastics, food and
  beverage, machinery, pharmaceuticals, semiconductors, solar, steel**, or **other**
  (a manufacturing project that genuinely fits none of the named ten).
  Pharmaceuticals covers drug substance, active ingredient and biologics plants;
  solar covers cell and panel manufacturing. This list is **closed** and is not yours
  to extend: a project you would have to invent a new sector name for is either
  **other**, or — if it is not manufacturing at all — out of scope entirely.

## Only collect NEW projects — not ones already in the verify table

Your goal is to **grow** coverage, so target a project that is **not already published in
the verify table** (`verify_verified` — the verified tracker that is the
pipeline's final product). The authoritative "already covered, don't collect these" set is
**the list of `project` names currently in `verify_verified`**. That live list is supplied to
you at runtime in a section titled *"The verify table already holds these"* below (the
pipeline reads it straight from the table before handing you this prompt). Do **not** collect
any project already in that list, nor a mere expansion / re-announcement of one.

If that list says the table is empty, nothing is excluded on this basis and every
qualifying project is fair game — including the largest and most obvious ones.

A second section, *“Already collected — not yet published; do not collect these again”*, lists projects
that have been collected but not published yet (Screen `project` names and shortened
Source summaries). **Also avoid those** — collecting one again just produces a duplicate.

## Neutrality — this matters

- **State no purpose beyond this step.** Your job is only to collect the sources for one
  qualifying project. Do not reason about, mention, or optimize for anything this data
  will later be used for. Frame everything as "gather the sources for this project."
- **Select regardless of outcome.** Do not prefer projects that were delayed, cancelled,
  on time, over budget, successful, or troubled. Whether the announcement's promises were
  ultimately kept is irrelevant to whether you collect it. Picking on outcome would bias
  the collection.
- **Keep the summary factual and neutral** — identify the thing, don't characterize
  whether it succeeded or failed.

## What to produce — one `source_collected` record

Four fields (the table's `id` and `datetime` are added automatically — don't supply them):

| Field | Required? | What it holds |
|---|---|---|
| `promise_source` | **required** | URL of the **original announcement** — the document that states the commitment (capital, jobs, and/or the intended timeline). |
| `status_source` | **required** | URL documenting the facility's **current status / progress** (most recent credible update you can find). |
| `promised_date_source` | optional | URL that specifically supports the **promised first-output / production date**, *if* that's a different or more precise source than `promise_source`. Omit if not applicable. |
| `summary` | recommended | 1–2 neutral sentences of context: what company, what facility, where, and what was announced. Context only — this is orientation for the next step, not data. |

Rules:

- **Prefer two independent origins** for `promise_source` and `status_source` — e.g. a
  company release vs. a state economic-development page vs. independent trade/local press.
  Two documents from the same origin (a wire story republished everywhere) count as one.
  This is a preference here, not a hard gate.
- **Do not extract or normalize figures.** Reading the announcement enough to confirm the
  size floor is fine, but do **not** record the capital amount, job count, or dates as
  structured fields — that is the next step's job. Source holds links + a summary only.
- **Duplicates are tolerated, not licensed.** The table has no unique constraint, but that
  is not a reason to re-file a site already named in the exclusion sections above.

## Output format

Return exactly one JSON object with these keys (omit `promised_date_source` if you have
none). One record per run — do not list, rank, or compare candidates alongside it:

```json
{
  "promise_source": "https://…",
  "status_source": "https://…",
  "promised_date_source": "https://…",
  "summary": "Company X announced a $Y semiconductor fab in <state>, U.S., in <month year>."
}
```

If you cannot find a qualifying project that meets every bar above, say so plainly and
output nothing — do not lower the thresholds to force a result.
