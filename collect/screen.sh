#!/usr/bin/env bash
#
# screen.sh -- drive PROMPT 2 (Source -> Screen extraction).
#
# There is no second outer loop: this is the *same* loop as source.sh, just
# pointed at the extraction prompt and counting a different table. So this is a
# thin wrapper that sets those two defaults and hands off to
# source.sh -- exactly the env-prefixed command you'd
# otherwise have to type by hand.
#
# RUN IT (from anywhere. bash on macOS/Linux/WSL):
#   bash collect/screen.sh          # extract ADD (default 10) screen rows
#   ADD=5 bash collect/screen.sh    # just 5 this run
#
# Every knob source.sh understands still works here (ADD, MAX_ITERS,
# MAX_STALL, MODEL, EFFORT, VERBOSE, PREFLIGHT) -- and PROMPT_FILE / COUNT_TABLE
# are still overridable too, since this only supplies defaults for them.
set -euo pipefail

export PROMPT_FILE="${PROMPT_FILE:-collect/prompts/prompt2_extract_screen.md}"
export COUNT_TABLE="${COUNT_TABLE:-screen_extracted}"

# Resolve to an absolute path so the inner script's own `cd $(dirname $0)/..`
# still lands on the repository root no matter where this was invoked from.
HERE="$(cd "$(dirname "$0")" && pwd)"
exec bash "$HERE/source.sh" "$@"
