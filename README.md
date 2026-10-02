# Promised vs. Produced Tracker: the data

The Promised vs. Produced Tracker follows America's biggest factory promises until they produce. For each promised factory it records what was announced (capital, jobs, the promised date of first output) next to what happened (the actual date of first output and the current status), with a public source for each. A person checks every published figure against its source.

This repository holds the Tracker's data. The Tracker itself, with a page per state and the full method, is at **[industriousaf.org/data/promised-vs-produced](https://industriousaf.org/data/promised-vs-produced/)**. How projects are chosen and checked: [methodology](https://industriousaf.org/data/promised-vs-produced/methodology).

Data as of 2026-10-02. 160 projects, 85 corrections, 5 retracted.

## Files

| File | What it holds |
|---|---|
| `outputs/csv_tables/tracker_verify.csv` | The published projects, one line each |
| `outputs/csv_tables/tracker_verify_edits.csv` | Every correction made after a project was published: the date, the project's `id` and the reason |
| `outputs/csv_tables/tracker_verify_retracted.csv` | Projects taken off the Tracker, with the date and the reason. A retracted project keeps its `id`, and the `id` is never reused |

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

## Cite it

For a sentence or a chart caption:

> Promised vs. Produced Tracker, IndustriousAF. CC BY 4.0.

In full, for a paper or report:

> IndustriousAF. (2026). *Promised vs. Produced Tracker* (Version 1.7) [Data set]. https://industriousaf.org/data/promised-vs-produced/

GitHub's "Cite this repository" button gives the same citation in APA and BibTeX, from [`CITATION.cff`](CITATION.cff).

When you publish online, please link to [industriousaf.org/data/promised-vs-produced](https://industriousaf.org/data/promised-vs-produced/).

## Report an error

Use the [correction form](https://github.com/industriousaf/promised-vs-produced-tracker/issues/new?template=correction.yml), or email hello@industriousaf.org. We need a public link that supports the correction. Accepted corrections are logged in `tracker_verify_edits.csv`.

## License

The data is licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/): reuse it for anything, with credit. See [`LICENSE`](LICENSE). The license covers the data, not the IndustriousAF name or marks.

The Tracker is refreshed at each quarterly recount.
