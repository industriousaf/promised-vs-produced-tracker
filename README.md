# Promised vs. Produced Tracker: the data

The Promised vs. Produced Tracker follows America's biggest factory promises until they produce. For each promised factory it records what was announced (capital, jobs, the promised date of first output) next to what happened (the actual date of first output and the current status), with a public source for each. A person checks every published figure against its source.

This repository holds the Tracker's data. The Tracker itself, with a page per state and the full method, is at **[industriousaf.org/data/promised-vs-produced](https://industriousaf.org/data/promised-vs-produced/)**. How projects are chosen and checked: [methodology](https://industriousaf.org/data/promised-vs-produced/methodology).

Data as of 2026-10-03. 165 projects, 1098 checks, 88 corrections, 5 retracted.

## Files

| File | What it holds |
|---|---|
| `outputs/csv_tables/tracker_verify.csv` | The published projects, one line each |
| `outputs/csv_tables/tracker_verify_edits.csv` | Every correction made after a project was published: the date, the project's `id` and the reason (`date`, `project_id`, `description`) |
| `outputs/csv_tables/tracker_verify_retracted.csv` | Projects taken off the Tracker, with the date and the reason (`retracted_at`, `retracted_reason`). A retracted project keeps its `id`, and the `id` is never reused |
| `outputs/csv_tables/tracker_verify_checks.csv` | Every check a person made of a published figure against its source: who, when, which page, and what they found |

## What counts

A project is on the Tracker if it is a single factory site in a U.S. state, announced on or after January 1, 2017, that promised at least $1 billion in capital or 2,000 direct jobs, in manufacturing. A site announced again with bigger numbers stays one project, dated and sized from its first announcement. If the company later changes what the factory will make, it is still the same promise.

## Columns in `tracker_verify.csv`

| Column | Meaning |
|---|---|
| `id` | The project's ID on the Tracker, the "Project ID" in the correction form |
| `project`, `sector`, `state`, `country` | What and where |
| `announced` | When the project was first announced, as precisely as the source says |
| `promised_capital_usd` | Promised capital in U.S. dollars. `promised_capital_max` holds the upper figure when the announcement gave a range |
| `promised_jobs` | Promised direct jobs in the factory. Blank when the announcement gave no figure for this site |
| `promised_first_output` | When the announcement said the factory would start producing, as written. `n/a` when it gave no date |
| `actual_first_output` | When the factory first made goods for sale. Test runs don't count. `pending` means not yet, `never` means it ended before producing, `unconfirmed` means it is producing but no source confirms the date |
| `status` | One of: announced, under construction, paused, producing, closed, cancelled |
| `current_status`, `notes` | Short written notes on the status and on anything unusual about the project |
| `announced_dt`, `promised_first_output_dt`, `actual_first_output_dt` | The same dates as a single day, for sorting and arithmetic. A month counts from the 15th, a quarter from its middle, a year alone from July 1, "early" from March 1, "first half" from April 1, and "second half" or "late" from October 1 |
| `lag_years` | Years from announcement to first output. Blank until the factory has produced on a confirmed date |
| `slip_years` | Years from the promised to the actual date of first output. Positive means late, negative means early. Blank unless both dates are known |
| `promise_source`, `status_source` | Links to the announcement and to the source for the current status. A cell can hold more than one link, separated by spaces or semicolons |
| `promised_date_source`, `actual_date_source`, `size_source` | Where a date or a size figure came from, when that is not the promise source |
| `checked_by` | Who checked the project's published figures against their sources. More than one address is separated by semicolons |
| `last_checked` | The newest date among those checks |

## Who checked what: `tracker_verify_checks.csv`

A person checks each of six figures on every project against its source before it is published, and again when a figure changes: `announced`, `promised_capital_usd`, `promised_jobs`, `promised_first_output`, `actual_first_output` and `current_status`. Each check is one line. A model may help find a page, but only a person saves a check.

| Column | Meaning |
|---|---|
| `id` | The check's number. Checks are never edited or deleted |
| `date` | When the check was saved |
| `project_id` | The project's `id` in `tracker_verify.csv` |
| `field` | Which figure was checked, named as the column in `tracker_verify.csv` |
| `result` | `confirmed`: the page states the value. `not stated on the page`: the person read the page and it does not state one. That is how a blank figure, such as `promised_jobs` or an `n/a` promised date, is checked |
| `value_checked` | The value the figure held when it was checked |
| `page` | The page that was open when the check was saved. Blank when the app did not record it; the project's own source links say which pages it was checked against |
| `matches` | How many times the page showed the value. Blank when not counted |
| `checked_by` | Who saved the check |
| `backs_current_value` | `yes` for the check that stands behind the figure as published today. `no` for a check later redone, or made on a value since corrected; the correction is in `tracker_verify_edits.csv` |

## Cite it

For a sentence or a chart caption:

> Promised vs. Produced Tracker, IndustriousAF. CC BY 4.0.

In full, for a paper or report:

> IndustriousAF. (2026). *Promised vs. Produced Tracker* (Version 1.8) [Data set]. https://doi.org/10.5281/zenodo.23116675

GitHub's "Cite this repository" button gives the same citation in APA and BibTeX, from [`CITATION.cff`](CITATION.cff).

When you publish online, please link to [industriousaf.org/data/promised-vs-produced](https://industriousaf.org/data/promised-vs-produced/).

## Report an error

Use the [correction form](https://github.com/industriousaf/promised-vs-produced-tracker/issues/new?template=correction.yml), or email hello@industriousaf.org. We need a public link that supports the correction. Accepted corrections are logged in `tracker_verify_edits.csv`.

## License

The data is licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/): reuse it for anything, with credit. See [`LICENSE`](LICENSE). The license covers the data, not the IndustriousAF name or marks.

The Tracker is refreshed at each quarterly recount.
