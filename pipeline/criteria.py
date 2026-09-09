"""criteria.py -- what counts as a project.

CHANGE THE INCLUSION RULES HERE. This is the only place they live. Before this
file the size floor was written out in ten places -- once in code and nine times
in prose across the README, both operating prompts, the collection prompt, the
CLI help and the web app -- so "change the criteria" meant editing ten files and
hoping.

The four rules are the ones README.md states, and two of them are numbers you
turn:

    SIZE     capital and jobs thresholds, joined by OR or AND
    WHEN     the earliest announcement date in scope
    WHERE    which countries, and the valid subdivisions of each
    SECTOR   the closed manufacturing vocabulary

A **phase** is one complete set of those. The Scoreboard is built by sweeping at
a high threshold first and lowering it later, and each sweep is a phase. Rows
record the phase that admitted them (`criteria_id`), because otherwise a later,
looser sweep is indistinguishable from the earlier one and nobody can say what
was searched for at what level. "No $300M plants in 2019" and "we were not
looking for $300M plants in 2019" are different facts, and only the stamp keeps
them apart.

For a single run, override without editing anything:

    CRITERIA=p2 bash collect/all.sh

Ask what is actually in effect rather than reading it off:

    python3 scoreboard.py criteria
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


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
    "p1": Criteria(
        id="p1",
        capital_usd=1_000_000_000,
        jobs=2_000,
        op="OR",
        announced_from="2017-01",
        countries=("US",),
        note="Complete at $1B first. The Scoreboard is built from the top down, "
             "so wherever review stops, the claim above that point is intact.",
    ),
    "p2": Criteria(
        id="p2",
        capital_usd=100_000_000,
        jobs=200,
        op="OR",
        announced_from="2017-01",
        countries=("US",),
        note="The lowered threshold, for a later sweep. Rows collected under p1 "
             "already clear it; what p2 adds is everything between the two.",
    ),
}

# The phase in effect. EDIT THIS to change what the pipeline collects and admits.
ACTIVE = "p1"


# --------------------------------------------------------------------------- #
# What is actually in effect                                                   #
# --------------------------------------------------------------------------- #

def active() -> Criteria:
    """The phase in effect: $CRITERIA if set, else ACTIVE above."""
    name = (os.getenv("CRITERIA") or ACTIVE).strip()
    if name not in PHASES:
        raise SystemExit(
            f"unknown criteria phase {name!r}. Defined: {', '.join(sorted(PHASES))}. "
            f"Set $CRITERIA to one of those, or add a phase in pipeline/criteria.py."
        )
    return PHASES[name]


def why() -> str:
    """What decided the active phase -- for the `criteria` command to report."""
    return "$CRITERIA" if os.getenv("CRITERIA") else "criteria.py"


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
