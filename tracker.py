#!/usr/bin/env python3
"""tracker.py -- the entry point. Run this.

A thin launcher for the pipeline CLI, so the first thing anyone runs is short and
obvious:

    python3 tracker.py status        # row counts per stage
    python3 tracker.py verify-list   # the published tracker
    python3 tracker.py --help        # every command

It is exactly equivalent to `python3 -m pipeline.cli`,
which still works and is what the docs and the collection loops use internally.
Nothing lives here; the commands are defined in
pipeline/cli.py.
"""

from __future__ import annotations

import os
import sys

# The pipeline needs 3.9 or newer. Say so here, in the first file anyone runs,
# because the alternative is a TypeError from inside an annotation three
# imports down that reads like a bug in the Tracker rather than a mismatch
# in the interpreter. Nothing above this line is version-dependent.
if sys.version_info < (3, 9):
    sys.exit(
        f"tracker.py needs Python 3.9 or newer; this is "
        f"{sys.version.split()[0]} at {sys.executable}.\n"
        "If a newer one is installed under another name, run that instead -- "
        "nothing here depends on the spelling."
    )

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pipeline.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
