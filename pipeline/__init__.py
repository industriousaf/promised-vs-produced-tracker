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


def set_config_env(key: str, value: str, path: Path | None = None) -> None:
    """Write KEY=value into `config.env`, and into this process's environment.

    For a setting a page can change, like the preload checkbox. Every other line
    stays as it was, comments and the API key included. An existing KEY line is
    replaced where it stands, and any later copy is dropped, since the loader
    above would only ever read the first; otherwise the line goes at the end.

    The file holds a key, so it keeps its permissions, or is created readable by
    its owner only, and it is replaced whole rather than rewritten in place, so
    a failure part way cannot leave it half-written. The temporary file beside
    it is gitignored for the same reason config.env is.
    """
    path = path or CONFIG_ENV
    try:
        text = path.read_text(encoding="utf-8")
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        text, mode = "", 0o600
    lines, found = [], False
    for line in text.splitlines():
        stripped = line.strip()
        name = stripped.partition("=")[0].strip()
        if "=" in stripped and not stripped.startswith("#") and name == key:
            if not found:
                lines.append(f"{key}={value}")
                found = True
            continue
        lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    os.environ[key] = value


load_config_env()
