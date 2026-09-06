"""models.py -- which Claude model each stage runs.

CHANGE THE MODEL HERE. This is the only place the names live; the collection
loops, the direct-API path and the web app all ask this file rather than
carrying a default of their own. Before it existed the same name was pinned in
three files under two different environment variables, so "switch the model"
meant knowing all three and remembering the odd one out.

For a single run, override without editing anything:

    MODEL=claude-sonnet-5 bash collect/all.sh     # every stage, this run
    SCREEN_MODEL=claude-sonnet-5 bash collect/all.sh   # just the Screen stage

`MODEL` is the global override and works on every path, including the API one.
A stage-specific variable beats it. Neither is needed for the normal case --
the normal case is what is written below.

Ask what is actually in effect, rather than reading it off:

    python3 scoreboard.py models
"""

from __future__ import annotations

import os

# --------------------------------------------------------------------------- #
# The defaults. Edit these.                                                    #
# --------------------------------------------------------------------------- #

# Finding new projects on the web. The harder of the two jobs: it has to judge
# whether a project is genuinely new, clears the inclusion floor, and is not
# already in the database under another name.
SOURCE = "claude-opus-4-8"

# Reading two sources and filling the 18 columns. Mechanical on its face, but
# it is also the only stage that catches the Source stage asserting something
# its own cited pages do not say -- which happened three times in twenty rows.
# That check is the reason this is not automatically the cheap-model slot.
SCREEN = "claude-opus-4-8"

# The direct-API flavour (source-collect / screen-extract / tools/gather.py),
# which needs ANTHROPIC_API_KEY and is not what the collection loops use.
API = "claude-opus-4-8"

# Reasoning effort for the loops: low | medium | high. The print-mode CLI flag
# takes no value above `high`.
EFFORT = "high"


# --------------------------------------------------------------------------- #
# What is actually in effect                                                   #
# --------------------------------------------------------------------------- #

def _pick(specific: str, default: str) -> str:
    """The stage's own variable, else the global MODEL, else the default here."""
    return os.getenv(specific) or os.getenv("MODEL") or default


def source() -> str:
    return _pick("SOURCE_MODEL", SOURCE)


def screen() -> str:
    return _pick("SCREEN_MODEL", SCREEN)


def api() -> str:
    # PIPELINE_MODEL is the name this path has always used; it still wins, so
    # nothing that set it stops working.
    return _pick("PIPELINE_MODEL", API)


def effort() -> str:
    return os.getenv("EFFORT") or EFFORT


def in_effect() -> dict[str, tuple[str, str]]:
    """{stage: (model, why)} -- what would run right now, and what decided it."""
    out = {}
    for stage, var, default, fn in (
        ("source", "SOURCE_MODEL", SOURCE, source),
        ("screen", "SCREEN_MODEL", SCREEN, screen),
        ("api", "PIPELINE_MODEL", API, api),
    ):
        if os.getenv(var):
            why = f"${var}"
        elif os.getenv("MODEL"):
            why = "$MODEL"
        else:
            why = "models.py"
        out[stage] = (fn(), why)
    return out
