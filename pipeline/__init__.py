"""Promised vs. Produced — the Source/Screen/Verify pipeline package.

A medallion (data-lakehouse) architecture: Source/Screen/Verify are this
project's names for the conventional Bronze/Silver/Gold stages. See
"Why three stages" in README.md for the mapping, and docs/cli.md for the commands.

Importing this package also loads `config.env` (see below), so every entry point
finds the same settings whether it is the CLI, the web app or `tools/gather.py`.
"""

from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------- #
# config.env                                                                   #
# --------------------------------------------------------------------------- #
# One gitignored file, in the repository root beside tracker.py, holding
# the settings that are per-machine rather than per-project -- ANTHROPIC_API_KEY
# above all, and TRACKER_DB / MODEL / EFFORT if you want them pinned.
#
# It is loaded HERE, on `import pipeline`, rather than by each entry point,
# because there are four of them (the CLI, the web app, tools/gather.py and the
# collect scripts) and a key that works in one and not the others is worse than
# no key file at all. tools/gather.py had this loader to itself; the web app's
# agentic check then needed the same value, and the honest answer to "where do I
# put my key" was "in config.env, but only that one script reads it". Now every
# path that imports the pipeline reads it, which is every path there is.
#
# `setdefault`, never assignment: a real exported shell variable always wins, so
# `ANTHROPIC_API_KEY=... python3 tracker.py ...` and a per-run `MODEL=` still
# override the file. Missing file, unreadable file, junk lines: all no-ops. This
# runs on import and must never be a reason the pipeline fails to start.
CONFIG_ENV = Path(__file__).resolve().parent.parent / "config.env"


def load_config_env(path: Path = CONFIG_ENV) -> None:
    """Load simple KEY=VALUE lines from `config.env` into os.environ.

    Stdlib-only, with no python-dotenv dependency: the rest of the pipeline runs
    on the standard library alone (see requirements.txt) and this file is the
    reason a key does not have to be exported by hand in every shell. Comments
    (#) and blank lines are skipped; surrounding quotes on a value are stripped.

    Nothing here logs, prints or transmits a value -- it only ever lands in
    `os.environ` for the Anthropic SDK to read.
    """
    try:
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


load_config_env()
