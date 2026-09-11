# The Promised vs. Produced Tracker

A US factory gets announced with a capital figure, a job count, and a target date
for first output. Years later the outcomes vary widely. Some plants are producing
at volume, while others have slipped by several years or been cancelled. There is
little public data tracking which is which.

This directory builds that data. It collects the announcement and the current
status for each project, extracts them into a fixed set of columns, and publishes
the rows a person has checked. The result is the **Tracker**, stored in one
SQLite file, `outputs/tracker.db`, which grows as projects are added.

What you clone is a **multi-command CLI**. One entry point, `tracker.py`, with
a subcommand for each step: read the data, add to it, check a row, publish it.
`python3 tracker.py --help` lists them all.

A row is published only after someone opens its two source links and confirms the
figures match. The automated check catches malformed and out-of-range values, but
it cannot tell whether a source says what the row claims. That is why a person
signs off on every published row.

**Contents**

- [What counts as a project](#what-counts-as-a-project)
- [See the Tracker](#see-the-tracker)
- [How a project gets in](#how-a-project-gets-in)
- [Add data](#add-data)
- [Publish a row](#publish-a-row)
- [What is in here](#what-is-in-here)
- [Notes](#notes)
- [License](#license)

---

## What counts as a project

A project is in scope when **all** of these hold. The thresholds belong to a
**phase** — the Tracker is made complete at a high threshold first and lowered
in later sweeps, and every row records the phase that admitted it, so a later,
looser sweep stays distinguishable from an earlier one. Two are defined:

| Phase | Rule | |
|---|---|---|
| `100M-or-200-jobs` | capital ≥ $100,000,000 **OR** ≥ 200 direct promised jobs | the rule this repository's rows were collected under |
| `1B-or-2000-jobs` | capital ≥ $1,000,000,000 **OR** ≥ 2,000 direct promised jobs | the high threshold |

`tracker.py config` prints which is in force with the line to edit, and `pipeline/settings.py` is
the only place any of it is set.

| | Rule |
|---|---|
| **Where** | a single physical facility in the United States |
| **When** | announced on or after the active phase's start date |
| **Size** | the active phase's capital **OR** jobs threshold, either one qualifying (the operator is per phase too) |
| **Sector** | one of these ten:<br>1. Aerospace and Defense<br>2. Auto Assembly<br>3. Battery<br>4. Chemicals and Plastics<br>5. Food and Beverage<br>6. Machinery<br>7. Pharmaceuticals<br>8. Semiconductors<br>9. Solar<br>10. Steel<br><br>…or `Other`, for a manufacturing project that genuinely fits none of the ten. `Other` is a last resort, not a bucket: if it starts filling up, the list above is wrong. |

Direct jobs only. "Regional," "supported," "induced," and construction-phase job
claims do not count toward the jobs threshold.

These rules are enforced in code by
[`pipeline/schema.py`](pipeline/schema.py).
If this table and that file ever disagree, the file is correct.

---

## See the Tracker

Three ways in, depending on what you want to do.

### 1. Open the CSV — nothing installed

The published rows are exported to a flat file. Open it in Excel, a text editor,
or anything else:

```
outputs/csv_tables/tracker_verify.csv
```

`tracker_source.csv` and `tracker_screen.csv` beside it hold the two
earlier stages, and `tracker_screen_check.csv` and
`tracker_verify_edits.csv` hold the audit trail. Regenerate all five with
`python3 tracker.py export`.

These rows are CC BY 4.0 — use them anywhere, credit the Tracker when you
publish one. See [License](#license) below.

### 2. Use the command line — nothing installed

Python 3.9 or newer, standard library only. From this directory:

```bash
python3 tracker.py status             # row counts per stage
python3 tracker.py verify-list        # the published tracker
python3 tracker.py screen-list        # rows waiting for review (-> = unverified)
python3 tracker.py verify-show --id 6 # one row + its edit history
python3 tracker.py --help             # every command
```

Those read only. Every command that writes is named as such below.

**On the name `python3`.** Every command here spells it `python3`, because on
Unix that name is guaranteed to mean Python 3, while a bare `python` may be
missing or may be Python 2. Nothing in the pipeline depends on the spelling —
if your system keeps the right interpreter somewhere else, under conda or pyenv
or a distribution that ships only `python`, use that name instead.
`./tracker.py` works too, and names no interpreter at all.

What does matter is the version, **3.9 or newer**. `tracker.py` checks at the
door and says so, rather than failing later from somewhere inside the code.

The collection scripts resolve the interpreter once and take `PY=` as an
override, alongside their other knobs:

```bash
PY=/opt/homebrew/bin/python3.12 bash collect/all.sh
```

### 3. Run the web app — needs two packages

```bash
python3 -m venv .venv && source .venv/bin/activate   # recommended, not required
pip install -r pipeline/requirements.txt
python3 tracker.py webapp             # then open http://localhost:8100
```

The `webapp` command runs uvicorn in process, so it works whether or not the
`uvicorn` script landed on your `PATH`. `--port` moves it, `--reload` restarts on
source changes, and `--db` composes, so `python3 tracker.py --db /tmp/try.db
webapp` reviews a copy instead of the real data.

Reading the data is the *least* of what this is for. Its real job is the
**review workflow**, and it is not "here is the row, here are two links". The
cited pages are *rendered in the page*, one tab each, with the values the row
claims highlighted in them and arrows to step from one highlight to the next —
so a row is confirmed a sentence at a time rather than by reading two articles
end to end. Beside it, a check that can do what the deterministic one cannot:
open the links and say whether they really carry the value, or where it actually
lives. If you just want to look, options 1 and 2 are faster and need no install.
See [`webapp/`](webapp/).

The CSVs in option 1 carry every column of their table, in table order, sorted by
`id`, with NULLs as empty cells. Three hold the Tracker; the two named
`_check` and `_edits` hold the audit trail, which is exported because
`tracker.db` is committed and git cannot diff a binary. Without them a commit
could add fifty check runs, or a correction to a published figure, and show
nothing but "tracker.db changed". `pipeline/export_tables.py` takes `--db` to
read a different database and `--out-dir` to write somewhere else. `python3 tracker.py export`
is the same exporter as a command; `--out-dir` works there too.

---

## How a project gets in

```
  SOURCE          →   SCREEN            →   VERIFY
  the links           one typed row         the published row
  collected           + a schema check      tier V1 / V2
  (AI or human)       tier P (provisional)  (human only)
```

| Stage | Holds | Who does it |
|---|---|---|
| **Source** | the source links and a one-line summary. No figures are recorded yet. | AI or human |
| **Screen** | the 20-column row extracted from those links, plus a schema check returning `FAIL`, `PASS`, or `CLEAN` | AI or human, then the checker |
| **Verify** | the published row. Later corrections are logged with a reason. | human only |

The check returns one of three verdicts. **`CLEAN` is the best of them, then
`PASS`, then `FAIL`** — the names do not sort that way, so read them once:

| verdict | what it means | promotable |
|---|---|---|
| `CLEAN` | shaped correctly, in range, nothing left open | yes |
| `PASS` | shaped correctly and in range, with a warning. Almost always an open `flag` the extraction left for a person | yes |
| `FAIL` | a schema error — a bad type, a bad enum, or figures that clear neither half of the size floor | no, unless forced |

A `PASS` is promotable on purpose. The usual warning is an open `flag`, no
command edits a flag on a Screen row, and `verify-promote` is what rewrites it
into a resolution record. So the warning is not something to clear first; it is
the note you read at the gate.

None of the three says anything about whether the sources support the figures.
The checker never opens a link. Nothing is promoted to Verify automatically.

Full definitions, including the ERROR and WARN severities behind the verdicts:
[`docs/schema.md`](docs/schema.md).

<a id="medallion"></a>
<details>
<summary><b>Why three stages: this is a medallion architecture</b></summary>

The data-lakehouse pattern popularized by Databricks. Data moves through
stages, and each stage is kept rather than overwritten, so a published figure can
always be traced back to the links it came from.

The convention names those stages Bronze, Silver, and Gold. This project renames
them after what happens at each step. The mapping is exact:

| Here | Convention | Table(s) |
|---|---|---|
| **Source** | Bronze | `source_collected` |
| **Screen** | Silver | `screen_extracted`, `screen_check` |
| **Verify** | Gold | `verify_verified`, `verify_edits` |

Wherever this project says "medallion," it means this pattern and this mapping.
Databases written before the rename still carry the Bronze/Silver/Gold table
names. The pipeline can read those files but never writes to them.

</details>

---

## Add data

One command finds new projects and extracts them into rows:

```bash
python3 tracker.py collect --n 10
```

Each iteration starts one `claude -p` process that searches the web and writes
through the CLI, so this path needs the `claude` CLI installed and
logged in once (run `claude`, then `/login`). It checks that before it starts.
Stopping with Ctrl-C is safe, and re-running picks up where you left off because
the database de-duplicates.

Check the plan before spending anything:

```bash
python3 tracker.py collect --n 5 --dry-run
```

Each stage starts one process per iteration, so `--n 10` across both stages is 20
or more runs. Start small.

**Leaving a long run unattended.** A run of any size takes minutes per row, so
anything past `--n 20` is an hour or more. The risk is not the pipeline — Ctrl-C
is safe and a re-run resumes — it is the machine going to sleep underneath it and
stopping the loop mid-iteration.

On macOS, `caffeinate -i` holds off idle sleep for exactly as long as the command
it wraps, then releases, so there is no setting to remember to undo:

```bash
caffeinate -i python3 tracker.py collect --n 30
```

It does **not** survive closing the lid, which sleeps the machine by another
route: leave the lid open, and preferably on power. On Linux, `systemd-inhibit`
is the equivalent; on Windows, `powercfg` settings. Neither is needed if the
machine is already set never to sleep.

Two knobs worth setting on a long run. `MAX_STALL` (default 3) ends a stage after
that many iterations add no rows — reasonable at small sizes, but a large run
searches harder for projects it does not already hold, so stalling becomes normal
rather than a fault; raising it to 6 stops a run ending early. And `VERBOSE=1`
writes every event to `logs/<run>.raw.jsonl`, which is what you will want if
something goes wrong while you were asleep. It goes to its own file rather than
into the transcript, so the readable log stays readable — the three files a run
writes are described under **What a run costs** below.

### Five ways in, and only two of them need Anthropic to collect anything

Collection is the only part of the Tracker that touches a model on its own.
The data model, the checker, the human gate, the exports and the coverage measure
are standard-library Python with no provider anywhere; the review screen's
agentic check is optional, advisory, and stores nothing.

| Path | What you run | Needs | Works with |
|---|---|---|---|
| **manual** | `source-add`, `screen-add` | nothing | you and a browser |
| **prompt** | `source-prompt`, `screen-prompt` | nothing, no key | **any assistant that can search the web** |
| **direct API** | `source-collect`, `screen-extract`, `tools/gather.py` | `ANTHROPIC_API_KEY` | Anthropic only |
| **loops** | `collect` | the `claude` CLI, logged in | Claude Code only |
| **review check** | "Ask Claude" on the review screen | `ANTHROPIC_API_KEY`, else it prints the prompt | Anthropic, or any assistant via the prompt |

### Where the API key goes

Two places, and a real shell variable always wins over the file:

```bash
export ANTHROPIC_API_KEY=sk-ant-...          # this shell only
```

or, to stop typing it, a **`config.env` in this directory** — beside
`tracker.py`, not inside `pipeline/` or `webapp/`:

```
# config.env  (gitignored; never commit a key)
ANTHROPIC_API_KEY=sk-ant-...
```

It is loaded on `import pipeline`, so every entry point sees it: the CLI, the web
app's agentic check, `tools/gather.py`, and the collection scripts. `TRACKER_DB`,
`MODEL` and `EFFORT` can live there too. Simple `KEY=value` lines; `#` comments and
blank lines are ignored; a missing file is not an error. Confirm it is being read
without printing it:

```bash
python3 tools/gather.py --n-source 1 --dry-run    # says "api key  found" or "NOT FOUND"
```

`.gitignore` already covers `config.env` and `.env`. Nothing in this project logs,
prints or transmits the value — it goes into `os.environ` for the Anthropic SDK
and nowhere else.

The **prompt** row is the one worth knowing about. `source-prompt` prints text
and nothing else. Paste it into ChatGPT, Gemini, Perplexity, Claude, or read it
yourself and do the searching by hand; whatever comes back is one JSON object,
and `source-add --json` ingests it. Nothing in that loop is Anthropic-specific,
and the operating prompts say nothing about which model is reading them.

The two automated collection paths are the Anthropic-specific ones. They are
faster, not more capable: all four write the same rows through the same functions
and face the same schema check and the same human gate. The fifth row writes
nothing at all — it is a reading aid on the review screen, and it degrades to a
printed prompt without a key. Nothing reaches Verify without a person, by any
route.

Per-stage settings are in [`docs/collecting.md`](docs/collecting.md). The manual
and copy-the-prompt paths are in [`docs/cli.md`](docs/cli.md).

**What a run costs, and how you find out.** Every `claude` call reports the
tokens it spent; `collect/all.sh` now reads that report instead of discarding
it. Each iteration prints one line as it finishes, and the run ends with its
totals — how many tokens, split by fresh input, cache read, cache write and
output, and by which model spent them. The same text goes to the transcript, so
a finished run can be read back later.

Expect two models in that breakdown, not one. `MODEL=` names the one doing the
pipeline work; the CLI picks its own for web search, and it spends real tokens
under a name you did not choose.

Three files per run, sharing one name:

| file | what it holds |
|---|---|
| `logs/<run>-collect.log` | the readable transcript, including the token report |
| `logs/<run>-usage.jsonl` | one result object per iteration, for re-totalling later |
| `logs/<run>.raw.jsonl` | every raw stream event — `VERBOSE=1` only |

`VERBOSE=1` used to put that third file's contents in the transcript itself,
which is how a twenty-row run produced six megabytes nobody could read. The
firehose now has its own file and the transcript stays printable.

---

## Publish a row

Three routes to the same gate. They write the same row through the same
function, so the choice is only about how you would rather read the sources.

| | Needs | Best for |
|---|---|---|
| `python3 tracker.py review` | nothing | working the queue in order. Prints each row's figures and both links, then asks about them one at a time. |
| `python3 tracker.py webapp` | `pip install` | reading a row *inside* its sources: the cited pages render in the review screen with the row's claims highlighted in them, and any of the 20 cells is editable in the form beside. |
| `verify-promote` by hand | nothing | one particular row, or a script. |

The rest of this section is the third route, which is also what the other two
run underneath.

Find a row that passed its check and read it:

```bash
python3 tracker.py screen-list --by-capital   # biggest first; -> = still unverified
python3 tracker.py screen-show --id 42        # the row and its sources
```

Work largest first. The Tracker is made complete from the top down, so
wherever you stop, the claim above that point holds.

Open its `promise_source` and `status_source` links. Confirm they support the
capital figure, the job count, and the dates. Then publish it:

```bash
python3 tracker.py verify-promote --screen-id 42 --tier V1 \
    --flag "Resolved: two independent sources agree on the announced date."
```

The tier records how deeply you checked: `V1` if you confirmed each figure
against one source, `V2` if you confirmed it against two independent ones.
Publishing the same project twice is refused.

Corrections after publication always require a reason, and are written to
`verify_edits`:

```bash
python3 tracker.py verify-edit --id 6 \
    --set current_status="Delayed — production pushed to 2027" \
    --desc "Re-read the Q3 release."
```

[`docs/verify_methods.md`](docs/verify_methods.md)
covers what to look for while reviewing.

---

## What is in here

If you are here to **use** it rather than change it, you will touch three things:
`tracker.py`, `collect/`, and `outputs/` -- plus `webapp/` to review rows.
Everything else is machinery. Each of the three pipeline directories has its own
README.

| Path | What it is |
|---|---|
| [`tracker.py`](tracker.py) | **The entry point.** A thin launcher for the pipeline CLI. |
| [`pipeline/`](pipeline/) | **The pipeline.** The six tables, the commands, and the promotion gate. `collect/`, `tools/` and the web app all write through it. Also holds `settings.py` -- the one place every threshold, model and loop setting is defined -- and `schema.py`, the row validator. |
| [`collect/`](collect/) | **Ongoing collection.** The loops that find new projects and extract them, and the prompts they hand to each one. |
| [`webapp/`](webapp/) | The browser interface, for reviewing rows against their sources and promoting them. |
| [`tools/`](tools/) | Two standalone scripts nobody imports: bulk CSV load, and batch collection over the direct API. They stay scripts because they are rare and sharp. (`export` and `coverage` moved into `pipeline/`, which imports them.) |
| [`outputs/`](outputs/) | `tracker.db`, plus `csv_tables/` holding a flat CSV export of each stage. |
| `docs/` `logs/` `scratch/` `tests/` | Reference, run transcripts (local only -- `logs/RUNS.md` is the committed record), working files, and the test suite. Nothing to click. |

Longer reference, kept out of this file:

- [`docs/cli.md`](docs/cli.md) — every command, the manual and Claude Code paths,
  and a short offline test.
- [`docs/collecting.md`](docs/collecting.md) — the collection loops in full.
- [`docs/schema.md`](docs/schema.md) — the six tables, the date handling, and how
  `flag` changes meaning between stages.

---

## Notes

- Run every command from this directory, the one holding this README.
- **The CSV exports keep themselves in step.** Any command that changes
  `outputs/tracker.db` refreshes `outputs/csv_tables/` as it closes, so the
  two are never committed out of sync. The database is committed and git cannot
  diff a binary, so those CSVs are how a change becomes readable in a review.
  This happens for the committed database only: a `--db` copy never overwrites
  the real exports. `TRACKER_NO_AUTOEXPORT=1` turns it off, and any command
  will then warn you that the CSVs have fallen behind.
- The database is `outputs/tracker.db`. Override it with `TRACKER_DB=/path/to/other.db`
  or `--db` on any command. Most commands open it read-write.
- `TRACKER_READONLY=1` opens it read-only for that command: everything that reads
  still works, everything that writes refuses and says which flag to unset. Use it
  when you are checking on the Tracker rather than changing it — poking at a research
  dataset to confirm something is fine should not be able to alter it. Cheaper and
  more certain than copying the file first.
- Keep the database on a local disk. SQLite locking is unreliable over network
  and virtual-machine file shares, which shows up as `database is locked`.
- Only the web interface needs installed dependencies
  (`pipeline/requirements.txt`). Nothing here requires an
  API key except the optional direct-API collection path.

---

## License

Two licenses, because this repository holds two things. Which one applies
depends on which file you took.

**The data is CC BY 4.0.** That is `outputs/tracker.db` and every CSV in
`outputs/csv_tables/`. Use it for anything, commercial work included. The one
condition is attribution: when you publish a figure or a number drawn from
these rows, name the Tracker in the caption or the sentence. This line
satisfies it:

```
The Promised vs. Produced Tracker, IndustriousAF. CC BY 4.0.
https://github.com/ashwinl4/promised-vs-produced-tracker
```

Full terms, the attribution rules and what is *not* covered are in
[`LICENSE-DATA`](LICENSE-DATA).

**The code is MIT.** Everything else in this repository — the pipeline, the
CLI, the web interface, the collection scripts and the prompts. Terms in
[`LICENSE`](LICENSE).

The split is worth stating plainly because the root `LICENSE` file is
unmodified MIT text, which is what lets GitHub detect and label it. Nothing in
that file says it stops at the code, so this section is where that boundary
actually gets drawn.

Two things neither license grants. The rows cite news articles and company
statements by URL; those pages belong to their publishers and are not
redistributed here. And no license here conveys any right in the IndustriousAF
name or in the Tracker's marks.
