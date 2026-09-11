"""settings.py -- everything configurable about the Scoreboard, in one file.

EDIT THIS FILE to change what the pipeline collects, which model does it, or how
a collection run behaves. It is the only place any of these values is defined.
Ask what is actually in effect, with the line to edit for each, rather than
reading it off:

    python3 scoreboard.py config

Four sections, and the division is worth keeping in mind because the sections
mean different things:

    1. WHAT COUNTS AS A PROJECT   methodology. Changing a threshold changes what
                                  the research population IS, needs a footnote in
                                  the paper, and normally means a fresh sweep.
    2. WHICH MODEL RUNS EACH STAGE   operations.
    3. HOW A COLLECTION RUN BEHAVES  operations, except for two settings that are
                                  not -- LEADS_PER_CALL and MAX_STALL decide what
                                  a run MEANS. See the note on each.
    4. WHO MAY VERIFY             methodology. These addresses are published
                                  beside the data they confirmed.

These were four separate places once -- two Python modules and defaults written
twice across collect/source.sh and collect/all.sh, because all.sh launches
source.sh with an explicit environment and had to supply a value for every knob
source.sh already defaulted. Two copies of a default is this repository's most
repeated bug, and it had already fired: all.sh's flat MAX_ITERS of 200 shadowed
source.sh's scaling default, so a run asking for 300 rows capped at 200 in
silence. The shell scripts now ask this file for its values.

For one run, override without editing anything:

    CRITERIA=100M-or-200-jobs MAX_STALL=8 SCREEN_MODEL=claude-sonnet-5 \
        bash collect/all.sh
"""

from __future__ import annotations

import ast
import functools
import os
from dataclasses import dataclass, field
from pathlib import Path

# ========================================================================== #
#  1. WHAT COUNTS AS A PROJECT                                             #
# ========================================================================== #
#
# A phase's name is its rule, spelled out -- `100M-or-200-jobs`, never `p1`. It
# is written into `criteria_id` on every row and into the checker's messages, so
# it has to mean something to a person reading a CSV column with no
# documentation open. A short opaque handle would be the same mistake as an
# undefined term in the README, except stored in the data, where it cannot be
# reworded later. Two phases exist because the Scoreboard is built by sweeping
# at a high threshold first and lowering it; the stamp is what keeps a later,
# looser sweep distinguishable from this one.

# --------------------------------------------------------------------------- #
# WHERE -- the subdivisions of each country in scope                           #
# --------------------------------------------------------------------------- #

# US postal abbreviations (50 states + DC + inhabited territories).
US_SUBDIVISIONS = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR", "GU", "VI", "AS", "MP",
}

# Add a country by adding its subdivisions here and naming it in a phase's
# `countries`. The Scoreboard is US-only today; the plan covers allied countries,
# and the `country` column exists on every row from the start so that adding one
# is a config change rather than a migration.
SUBDIVISIONS: dict[str, set] = {
    "US": US_SUBDIVISIONS,
}


# --------------------------------------------------------------------------- #
# SECTOR -- the closed manufacturing vocabulary                                #
# --------------------------------------------------------------------------- #

# Adding a sector is a code change on purpose, and it used to be two things: this
# set, plus a JSON registry that `register_sector()` wrote at runtime. The
# registry was wired to the API path, the web app and a `sectors-add` command,
# and in the whole life of the project it was never once used -- while both
# operating prompts told the model never to touch it ("extending the vocabulary
# is a human decision, not yours").
#
# It is gone, and the reason is not tidiness. A vocabulary that can change at
# runtime can change mid-run with nothing in git showing it, which moves what
# counts as in scope and leaves no trace. Adding a sector is now a commit.
SECTORS = {
    "Aerospace and Defense",
    "Auto Assembly",
    "Battery",
    "Chemicals and Plastics",
    "Food and Beverage",
    "Machinery",
    "Pharmaceuticals",
    "Semiconductors",
    "Solar",
    "Steel",
    "Other",
}


# --------------------------------------------------------------------------- #
# The phases                                                                   #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Criteria:
    """One complete definition of what counts as a project."""
    id: str
    capital_usd: int
    jobs: int
    op: str                      # 'OR' (either threshold) or 'AND' (both)
    announced_from: str          # inclusive, 'YYYY-MM'
    countries: tuple             # ISO country codes, e.g. ('US',)
    note: str = ""
    sectors: frozenset = field(default_factory=lambda: frozenset(SECTORS))

    def clears(self, capital, jobs) -> bool:
        """Does this pair of figures put a row in scope?

        Either figure may be None -- a source that states jobs and no capital is
        ordinary, and under OR the known figure can settle it alone. Under AND a
        missing figure can never settle it, because the unknown one might fail.
        """
        cap_ok = capital is not None and capital >= self.capital_usd
        jobs_ok = jobs is not None and jobs >= self.jobs
        return (cap_ok or jobs_ok) if self.op == "OR" else (cap_ok and jobs_ok)

    def subdivisions(self) -> set:
        """Every valid subdivision code across the countries in scope."""
        out: set = set()
        for c in self.countries:
            out |= SUBDIVISIONS.get(c, set())
        return out

    def describe(self) -> str:
        cap = f"${self.capital_usd:,}"
        return f"{cap} {self.op} {self.jobs:,} jobs"


PHASES: dict[str, Criteria] = {
    "1B-or-2000-jobs": Criteria(
        id="1B-or-2000-jobs",
        capital_usd=1_000_000_000,
        jobs=2_000,
        op="OR",
        announced_from="2017-01",
        countries=("US",),
        note="The high threshold. The Scoreboard is made complete here first, "
             "because it is built from the top down -- wherever review stops, the "
             "claim above that point is intact.",
    ),
    "100M-or-200-jobs": Criteria(
        id="100M-or-200-jobs",
        capital_usd=100_000_000,
        jobs=200,
        op="OR",
        announced_from="2017-01",
        countries=("US",),
        note="The low threshold, and the rule that produced the rows in this "
             "repository. Everything admitted at the high threshold clears this "
             "one too; what this phase adds is the range between them.",
    ),
}

# The phase in effect. EDIT THIS to change what the pipeline collects and admits.
#
# Raised to the high threshold together with emptying the database, because the
# two are one decision. Changing the floor under rows collected at another one
# leaves a repository whose data fails its own checker -- 94 of the previous 212
# rows did exactly that during the hour the two were out of step. The rows
# collected at 100M-or-200-jobs are in the scoreboard-v1.2 tag:
#
#     git show scoreboard-v1.2:outputs/scoreboard.db > /tmp/sb-100M.db
ACTIVE = "1B-or-2000-jobs"


# --------------------------------------------------------------------------- #
# What is actually in effect                                                   #
# --------------------------------------------------------------------------- #

def active() -> Criteria:
    """The phase in effect: $CRITERIA if set, else ACTIVE above."""
    name = (os.getenv("CRITERIA") or ACTIVE).strip()
    if name not in PHASES:
        raise SystemExit(
            f"unknown criteria phase {name!r}. Defined: {', '.join(sorted(PHASES))}. "
            f"Set $CRITERIA to one of those, or add a phase in pipeline/settings.py."
        )
    return PHASES[name]


def criteria_source() -> str:
    """What decided the active phase -- for the `criteria` command to report."""
    return "$CRITERIA" if os.getenv("CRITERIA") else "settings.py"


def get(criteria_id) -> Criteria:
    """The phase a stored row was admitted under.

    A row is checked against the rule that admitted it, never against whatever
    is active now, so moving the threshold can never retroactively invalidate
    data that was in scope when it was collected. Rows written before the column
    existed, or carrying a phase since deleted, fall back to the active one --
    the alternative is refusing to check a row at all, which is worse.
    """
    name = (criteria_id or "").strip()
    return PHASES.get(name) or active()

# ========================================================================== #
#  2. WHICH MODEL RUNS EACH STAGE                                          #
# ========================================================================== #

# --------------------------------------------------------------------------- #
# The defaults. Edit these.                                                    #
# --------------------------------------------------------------------------- #

# Finding new projects on the web. The harder of the two jobs: it has to judge
# whether a project is genuinely new, clears the inclusion floor, and is not
# already in the database under another name.
SOURCE = "claude-opus-4-8"

# Reading two sources and filling the 20 columns. Mechanical on its face, but
# it is also the only stage that catches the Source stage asserting something
# its own cited pages do not say -- which happened three times in twenty rows.
# That check is the reason this is not automatically the cheap-model slot.
SCREEN = "claude-opus-4-8"

# The direct-API flavour (source-collect / screen-extract / tools/gather.py),
# which needs ANTHROPIC_API_KEY and is not what the collection loops use.
API = "claude-opus-4-8"

# The agentic check on the review screen: "does the cited page actually say
# this, and if not, where is the right answer?" Deliberately the cheaper, faster
# model. It is run interactively, one row and a couple of cells at a time, while
# a person waits for the answer -- and it is not deciding anything, only pointing
# a human at a passage they then read themselves. Sonnet is the right size for
# that; the collection stages above are not, which is why they keep Opus.
AGENT = "claude-sonnet-5"

# Reasoning effort for the loops: low | medium | high. The print-mode CLI flag
# takes no value above `high`.
EFFORT = "high"


# --------------------------------------------------------------------------- #
# What is actually in effect                                                   #
# --------------------------------------------------------------------------- #

# The three sections each had a _pick, and two had a `why` and an `in_effect`.
# Merged into one file the later definition silently shadowed the earlier, and
# models.screen() stopped honouring $MODEL because it was calling the run
# section's _pick. Caught by a test. Every name here is now section-specific.
def _pick_model(specific: str, default: str) -> str:
    """The stage's own variable, else the global MODEL, else the default here."""
    return os.getenv(specific) or os.getenv("MODEL") or default


def source() -> str:
    return _pick_model("SOURCE_MODEL", SOURCE)


def screen() -> str:
    return _pick_model("SCREEN_MODEL", SCREEN)


def api() -> str:
    # PIPELINE_MODEL is the name this path has always used; it still wins, so
    # nothing that set it stops working.
    return _pick_model("PIPELINE_MODEL", API)


def agent() -> str:
    return _pick_model("AGENT_MODEL", AGENT)


def effort() -> str:
    return os.getenv("EFFORT") or EFFORT


def models_in_effect() -> dict[str, tuple[str, str]]:
    """{stage: (model, source)} -- what would run right now, and what decided it."""
    out = {}
    for stage, var, default, fn in (
        ("source", "SOURCE_MODEL", SOURCE, source),
        ("screen", "SCREEN_MODEL", SCREEN, screen),
        ("api", "PIPELINE_MODEL", API, api),
        ("agent", "AGENT_MODEL", AGENT, agent),
    ):
        if os.getenv(var):
            why = f"${var}"
        elif os.getenv("MODEL"):
            why = "$MODEL"
        else:
            why = "settings.py"
        out[stage] = (fn(), why)
    return out

# ========================================================================== #
#  3. HOW A COLLECTION RUN BEHAVES                                         #
# ========================================================================== #

# --------------------------------------------------------------------------- #
# The defaults. Edit these.                                                    #
# --------------------------------------------------------------------------- #

# How many leads ONE Source call may return. A ceiling, never a quota: the prompt
# is explicit that returning fewer, even zero, beats loosening a threshold to
# reach it, and the saturation measurement depends on a short answer being
# allowed. It also sets what a full call means -- with the ceiling at 5, a call
# returning 1 still counts as progress and resets the stall counter below.
#
# Raising it is not obviously cheaper. Measured cost at the $1B threshold was
# ~385K tokens per lead early in a run; the Screen batching A/B found three jobs
# per call cost 15% MORE tokens than one, because a call holding several jobs
# re-reads all of them every turn. Measure before moving it.
LEADS_PER_CALL = 5

# Stop after this many CONSECUTIVE calls that add nothing. Any single find resets
# the counter to zero.
#
# This is the Scoreboard's current answer to "is this threshold exhausted", and
# it is a weak one. On the run that first reached it, the first empty call was
# call 40 and the rule did not fire until call 50 -- four times the counter
# reached 1 and a stray find reset it, at a cost of 16M tokens for nine leads.
# The number a run reports as its saturation point therefore depends on where
# those stray finds happen to land. A rate-based rule (stop when the trailing
# calls average well below the ceiling) would degrade gracefully; an estimator
# (capture-recapture across independent sweeps) is what turns "we stopped
# finding things" into "at least N exist". Neither is built yet.
MAX_STALL = 3

# 1 = stream every tool call and message live (a JSON firehose, megabytes per
# run). The transcript and the token ledger are written either way.
VERBOSE = 0

# The iteration cap scales with the rows asked for rather than sitting at a flat
# number, because "too many turns" depends on how many rows a run wants. It
# bounds COST, not liveness -- MAX_STALL is the liveness check. It exists for the
# case MAX_STALL cannot see: steady but slow progress, where `stall` resets on
# every success and a run adding one row every fourth call never trips it.
ITERS_PER_ROW = 3
MIN_ITERS = 30


def max_iters(add) -> int:
    """The cost cap for a run asking for `add` rows.

    A non-numeric or tiny `add` falls back to the floor so the loop can never be
    capped at zero -- BSD `seq 1 0` counts DOWN, which would run two turns with
    i=1 then i=0 rather than none.
    """
    try:
        n = int(str(add).strip())
    except (TypeError, ValueError):
        return MIN_ITERS
    return max(n * ITERS_PER_ROW, MIN_ITERS)


# --------------------------------------------------------------------------- #
# What is actually in effect                                                   #
# --------------------------------------------------------------------------- #

def _pick_run(name: str, default, stage: str | None = None):
    """A stage's own variable, else the global one, else the default here.

    Mirrors the precedence the model section above uses for SOURCE_MODEL / MODEL, and the one
    all.sh's `stage_cfg` implements for the shell: SOURCE_MAX_STALL beats
    MAX_STALL beats the constant above.
    """
    if stage:
        v = os.getenv(f"{stage.upper()}_{name}")
        if v:
            return v
    return os.getenv(name) or default


def leads_per_call(stage: str | None = None) -> int:
    return int(_pick_run("LEADS_PER_CALL", LEADS_PER_CALL, stage))


def max_stall(stage: str | None = None) -> int:
    return int(_pick_run("MAX_STALL", MAX_STALL, stage))


def verbose(stage: str | None = None) -> int:
    return int(_pick_run("VERBOSE", VERBOSE, stage))


def run_source(name: str, stage: str | None = None) -> str:
    """What decided a setting -- for `config` to report alongside the value."""
    if stage and os.getenv(f"{stage.upper()}_{name}"):
        return f"${stage.upper()}_{name}"
    return f"${name}" if os.getenv(name) else "settings.py"


def max_iters_in_effect(stage: str | None = None, add=None) -> int:
    """The cap actually in force: $MAX_ITERS (or a stage-specific one) if set,
    else the formula. `config` had been printing the formula while the shell
    honoured the variable -- the very shadowing incident this file exists to
    make visible, hidden by the tool built to show it."""
    return int(_pick_run("MAX_ITERS", max_iters(add), stage))


def effort_source() -> str:
    return "$EFFORT" if os.getenv("EFFORT") else "settings.py"


def run_in_effect(stage: str | None = None, add=None) -> dict:
    """{setting: (value, what decided it)} -- everything this section owns."""
    return {
        "leads_per_call": (leads_per_call(stage), run_source("LEADS_PER_CALL", stage)),
        "max_stall": (max_stall(stage), run_source("MAX_STALL", stage)),
        "verbose": (verbose(stage), run_source("VERBOSE", stage)),
        "max_iters": (max_iters_in_effect(stage, add), run_source("MAX_ITERS", stage)),
    }


# ========================================================================== #
#  4. WHO MAY VERIFY                                                       #
# ========================================================================== #
#
# The people allowed to confirm a field against a cited page. Their address is
# written into `screen_attested.attested_by` on every confirmation, and those
# rows are exported to a public CSV.
#
# A list and not a text box, and the difference is the whole point. A box
# accepts "asdf@asdf.com" as readily as a real address, so a typed identity is
# exactly as forgeable as a typed name while looking more convincing to whoever
# reads the data later. Nothing here authenticates anyone either -- but adding
# someone is a deliberate act by the project, so every attestation traces back
# to a decision about who counts as a verifier, and there is nothing to typo.
#
# Addresses are published. Say so to anyone before adding them.
VERIFIERS = [
    "ashwin@industriousaf.org",
    "lucas@industriousaf.org",
]


# --------------------------------------------------------------------------- #
# What is actually in effect                                                   #
# --------------------------------------------------------------------------- #

def verifiers() -> list[str]:
    """The addresses that may attest, in the order they are listed."""
    return [v.strip() for v in VERIFIERS if v.strip()]


def may_verify(address: str) -> bool:
    """True when `address` is on the list. Nothing else may be written."""
    return address.strip() in verifiers()


# --------------------------------------------------------------------------- #
# Where to edit each value                                                     #
# --------------------------------------------------------------------------- #

@functools.lru_cache(maxsize=None)
def _definitions() -> dict[str, int]:
    """{name: line} for every top-level assignment in THIS file, plus one entry
    per phase -- "PHASES[1B-or-2000-jobs]" -- pointing at its Criteria(...) call.

    Parsed with `ast`, not a regex: a regex on `^NAME =` could cite ACTIVE but
    never the thresholds inside PHASES, which are the numbers a person actually
    turns. Read once per process; `config` asks a dozen times.
    """
    tree = ast.parse(Path(__file__).read_text())
    out: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        for n in names:
            out.setdefault(n, node.lineno)
        if "PHASES" in names and isinstance(node.value, ast.Dict):
            for k, v in zip(node.value.keys, node.value.values):
                if isinstance(k, ast.Constant):
                    out[f"PHASES[{k.value}]"] = v.lineno
    return out


def line_of(name: str) -> int | None:
    """The line in this file where `name` is defined, or None."""
    return _definitions().get(name)


def where(name: str) -> str:
    """'settings.py:154' for a named definition.

    Raises on an unknown name rather than degrading to a bare filename: a
    renamed constant should fail `config` (and the test that covers it), not
    quietly print a less useful answer for every row that cited it.
    """
    n = line_of(name)
    if n is None:
        raise KeyError(f"{name!r} is not defined at top level of settings.py")
    return f"settings.py:{n}"


def where_phase(phase_id: str) -> str:
    """The line of one phase's Criteria(...) inside PHASES."""
    return where(f"PHASES[{phase_id}]")
