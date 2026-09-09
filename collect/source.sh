#!/usr/bin/env bash
#
# source.sh -- drive PROMPT 1 (or PROMPT 2) as repeated, independent
# Claude Code calls until ADD new rows have been added THIS RUN.
#
# HOW IT WORKS (the "outer loop"):
#   It notes the starting row count for COUNT_TABLE, then each iteration...
#     1. reads the current count from `pipeline.cli status`,
#     2. stops once (current - starting) >= ADD, i.e. ADD rows were added this run,
#     3. launches ONE `claude -p` process that actually
#        does the job -- web-searches, collects, and writes to scoreboard.db using
#        the real pipeline CLI. It is a brand-new process every time, so no
#        context bleeds between runs (see prompts/README.md, "no chat history").
#   This script is JUST the loop; the `claude` CLI does the work each turn.
#
# WHY IT DOESN'T DOUBLE-COLLECT:
#   Every `source-add` commits immediately, and each iteration re-runs `source-prompt`
#   first -- which re-reads the DB (published + already collected) -- so each run
#   automatically steers around everything collected so far.
#
# RUN IT (from anywhere; it cd's to scoreboard/ itself. bash on macOS/Linux/WSL):
#   bash collect/source.sh                 # add ADD (default 10) source leads
#   ADD=3 bash collect/source.sh           # just add 3 this run
#   LEADS_PER_CALL=10 bash collect/source.sh   # ceiling per call (default 5)
#   PROMPT_FILE=collect/prompts/prompt2_extract_screen.md \
#     COUNT_TABLE=screen_extracted ADD=10 bash collect/source.sh   # PROMPT 2
#
set -euo pipefail

# Stop cleanly on Ctrl-C / terminal close instead of rolling into the next
# iteration. (Without this, the `|| echo` below swallows the interrupt.)
trap 'echo; echo "interrupted -- stopping the loop."; exit 130' INT TERM

# --- Config (override any of these via env) -------------------------------- #
PROMPT_FILE="${PROMPT_FILE:-collect/prompts/prompt1_collect_recent.md}"
COUNT_TABLE="${COUNT_TABLE:-source_collected}"   # which stage's rows we're adding to
ADD="${ADD:-10}"                                  # how many NEW rows to add to COUNT_TABLE this run

# Every default comes from pipeline/settings.py, asked for rather than repeated
# here -- all.sh needs the same values, and two copies of a default is this
# repo's most repeated bug. The note on what each one means lives there.
# NOTE: the print-mode `--effort` flag only accepts low|medium|high -- there is
# no "extra high" from the CLI. `high` is the ceiling.

# --- Locate scoreboard/ and the tools --------------------------------- #
cd "$(dirname "$0")/.."                            # collect/ -> scoreboard/
PY="${PY:-$(command -v python3 || command -v python)}"

# Which stage this is. all.sh sets it; a stage run on its own names itself from
# the table it counts. Derived FIRST: every lookup below is stage-specific, and
# resolving them above this block passed an empty stage, so SCREEN_MAX_STALL
# and friends were silently ignored on a direct run.
if [ -z "${STAGE_LABEL:-}" ]; then
  case "$COUNT_TABLE" in
    source_collected) STAGE_LABEL=SOURCE ;;
    screen_extracted) STAGE_LABEL=SCREEN ;;
    *)                STAGE_LABEL="$COUNT_TABLE" ;;
  esac
fi

# --- Settings, from pipeline/settings.py ---------------------------------- #
# Resolved after $PY exists, after the cd, and after STAGE_LABEL so the
# SOURCE_* / SCREEN_* overrides apply. Each is skipped when the caller (all.sh)
# already passed it, so a value in hand never costs a second interpreter.
LEADS_PER_CALL="${LEADS_PER_CALL:-$("$PY" -m pipeline.cli config --for leads-per-call --stage "$STAGE_LABEL")}"
MAX_ITERS="${MAX_ITERS:-$("$PY" -m pipeline.cli config --for max-iters --add "$ADD" --stage "$STAGE_LABEL")}"
MAX_STALL="${MAX_STALL:-$("$PY" -m pipeline.cli config --for max-stall --stage "$STAGE_LABEL")}"
VERBOSE="${VERBOSE:-$("$PY" -m pipeline.cli config --for verbose --stage "$STAGE_LABEL")}"
MODEL="${MODEL:-$("$PY" -m pipeline.cli models --for "$STAGE_LABEL")}"
EFFORT="${EFFORT:-$("$PY" -m pipeline.cli models --for "$STAGE_LABEL" --effort)}"

CLAUDE="$(command -v claude || echo "$HOME/.local/bin/claude")"
[ -x "$CLAUDE" ] || { echo "ERROR: claude CLI not found ($CLAUDE)"; exit 1; }

# --- Preflight: the claude CLI must be authenticated ----------------------- #
# One tiny call detects the "Not logged in" state so we fail fast with guidance
# instead of spinning through failed iterations. Skip with PREFLIGHT=0.
if [ "${PREFLIGHT:-1}" = "1" ]; then
  # `timeout` is GNU coreutils and is absent on stock macOS; fall back to
  # `gtimeout` if present, else run the probe unbounded.
  if command -v timeout >/dev/null 2>&1; then TIMEOUT="timeout 120"
  elif command -v gtimeout >/dev/null 2>&1; then TIMEOUT="gtimeout 120"
  else TIMEOUT=""; fi
  probe="$($TIMEOUT "$CLAUDE" -p "Reply with exactly: READY" \
             --model "$MODEL" --permission-mode default 2>&1 || true)"
  if ! printf '%s' "$probe" | grep -q "READY"; then
    echo "ERROR: claude preflight failed. First response was:"
    printf '  %s\n' "$probe" | head -5
    echo "If it says 'Not logged in', run this once:  claude   (then  /login)"
    echo "Then re-run. (Set PREFLIGHT=0 to skip this check.)"
    exit 1
  fi
  echo "preflight ok (claude authenticated)."
fi

PROMPT="$(cat "$PROMPT_FILE")"
# The per-call ceiling is the loop's parameter, so the loop states it rather than
# the prompt file carrying a number that only prose could change. PROMPT 2 has no
# ceiling -- it extracts exactly one lead per call, which its own text settles.
if [ "$COUNT_TABLE" = "source_collected" ]; then
  PROMPT="$PROMPT

## How many to collect this call

**Up to $LEADS_PER_CALL.** This is the ceiling referred to above: it is the most
you may return, never the least you must find. Collecting fewer, or none, is the
correct outcome when nothing else genuinely clears every bar."
fi
# What the loop counts to decide it is done. `count` prints one integer, and for
# screen_extracted it prints DISTINCT PROJECTS rather than rows -- a lead
# extracted twice is one project, and counting rows let a duplicate tick the
# counter, so an N=20 run stopped at 19 real projects and called it success.
# (This used to awk the last field out of the human-readable `status` table,
# which tied the stop condition to that table's wording.)
count() { "$PY" -m pipeline.cli count "$COUNT_TABLE"; }

# --- How the CLI reports itself -------------------------------------------- #
# Every `claude -p` call ends by reporting the tokens it spent and the models it
# spent them on. The default `text` format prints the answer and throws that
# report away, which is why a quiet run used to leave no accounting anywhere at
# all -- not buried, absent. So always ask for a machine-readable result and
# pipe it through collect/tally.py, which prints the readable part plus one line
# of accounting.
#
#   quiet      --output-format json         one result object at the end
#   VERBOSE=1  --output-format stream-json  live events, rendered compactly
#
# --include-partial-messages is deliberately NOT set under VERBOSE=1. It emits
# every token twice -- once as a partial chunk, once in the finished block --
# and was most of the 6.5MB that made the last run's log unprintable. The full
# events still reach $RAW_LOG; tally.py renders a compact trace for the log.
#
# Expanded below as ${VERBOSE_FLAGS[@]+"${VERBOSE_FLAGS[@]}"} rather than plain
# "${VERBOSE_FLAGS[@]}": under `set -u`, bash 3.2 (which macOS still ships)
# treats an empty array expansion as an unbound variable and aborts.
OUT_FORMAT=json
VERBOSE_FLAGS=(--output-format json)
if [ "$VERBOSE" = "1" ]; then
  OUT_FORMAT=stream-json
  VERBOSE_FLAGS=(--verbose --output-format stream-json)
fi


# --- Run transcript --------------------------------------------------------- #
# Every run tees its own output to logs/. The transcript is the only record of
# what a run actually did: which model and effort, how many turns, which
# iterations failed, why it stopped. The database says what was collected but
# not how, and for a Scoreboard that will be cited, how is part of the claim.
#
# LOG=0 turns it off. LOG=<path> picks the file.
#
# LOG_ACTIVE is what stops a stage double-writing. all.sh already redirects its
# own output through tee, and a stage it calls inherits that -- so if the stage
# also opened the file, every line would be written twice: once through the
# stage's tee and once through all.sh's. The parent sets LOG_ACTIVE, the child
# sees it and skips its own redirect, and the lines still reach the file by
# flowing up through the parent. Run a stage on its own and LOG_ACTIVE is unset,
# so it opens its own transcript.
LOG="${LOG:-}"
if [ "$LOG" != "0" ] && [ -z "${LOG_ACTIVE:-}" ]; then
  [ -n "$LOG" ] || LOG="logs/$(date -u +%Y%m%dT%H%M%SZ)-${COUNT_TABLE}.log"
  mkdir -p "$(dirname "$LOG")"
  export LOG LOG_ACTIVE=1
  exec > >(tee -a "$LOG") 2>&1
fi

# --- The other two files ---------------------------------------------------- #
# The transcript above is for reading, so it gets only what a human wants to
# read. Two machine files sit beside it:
#
#   <run>-usage.jsonl  one result object per iteration -- the accounting, kept
#                      so a finished run can be re-totalled without re-running
#   <run>.raw.jsonl    every raw stream event, VERBOSE=1 only. This is the
#                      firehose. It used to go into the .log itself, which is
#                      how a 20-row run produced 6.5MB nobody could print.
#
# LOG=0 still has to leave a ledger somewhere or the summary at the end would
# have nothing to total, so it gets a temporary one and deletes it on the way
# out. USAGE_LEDGER already set means all.sh owns these and both stages append
# to the same pair -- the same parent-owns-it arrangement as LOG_ACTIVE.
LEDGER_OWNER=0
if [ -z "${USAGE_LEDGER:-}" ]; then
  LEDGER_OWNER=1
  if [ "$LOG" != "0" ]; then
    USAGE_LEDGER="${LOG%.log}-usage.jsonl"
    RAW_LOG="${LOG%.log}.raw.jsonl"
  else
    USAGE_LEDGER="$(mktemp)"
    RAW_LOG=""
    trap 'rm -f "$USAGE_LEDGER"' EXIT
  fi
  export USAGE_LEDGER RAW_LOG
fi
RAW_FLAGS=()
[ "$VERBOSE" = "1" ] && [ -n "${RAW_LOG:-}" ] && RAW_FLAGS=(--raw "$RAW_LOG")

START="$(count)"; START="${START:-0}"
# One header line, so a transcript read on its own says when it ran and which
# database it wrote to. all.sh prints these too; a stage run directly did not.
echo "started : $(date -u +%Y-%m-%dT%H:%M:%SZ)  db=$("$PY" -c 'from pipeline.db import db_path; print(db_path())')"
_CEIL=""
[ "$COUNT_TABLE" = "source_collected" ] && _CEIL=", leads/call=$LEADS_PER_CALL"
echo "loop: prompt=$PROMPT_FILE  add $ADD to $COUNT_TABLE (now $START)  (model=$MODEL, effort=$EFFORT$_CEIL)"

stall=0
hit_cap=1        # cleared by either deliberate exit below
for i in $(seq 1 "$MAX_ITERS"); do
  now="$(count)"; now="${now:-0}"
  added=$(( now - START ))
  echo "=== iter $i - added $added/$ADD this run ($COUNT_TABLE=$now) ==="
  [ "$added" -ge "$ADD" ] && { echo "added $ADD this run; stopping."; hit_cap=0; break; }

  # One fresh process. The operating prompt goes in the system prompt (works for
  # prompt1 OR prompt2). docs/cli.md is attached as context via an @-mention
  # (like clicking a file into context in the IDE) rather than read with a tool.
  # --allowedTools pre-approves tools so nothing stalls on a permission prompt.
  #
  # docs/cli.md is the command reference and nothing else, so the attachment is
  # scoped by construction. The two guardrails below still have to be said: with
  # Bash pre-approved above, the process could otherwise re-launch the loop that
  # started it, and Verify is a human-only gate.
  "$CLAUDE" -p "@docs/cli.md is attached as reference context -- it is this pipeline's CLI reference. Never run a shell script from collect/ (that is the loop that launched you), never promote to Verify (verify-promote / --promote-tier) -- that is a human-only gate -- and never delete rows (screen-remove): if an extraction of yours is wrong, re-add it with --replace, which supersedes it in one step. Now carry out your operating instructions (in the system prompt) to completion using the real pipeline CLI. Do only what those instructions say." \
    --model "$MODEL" \
    --effort "$EFFORT" \
    --append-system-prompt "$PROMPT" \
    --allowedTools "Bash Read Write WebSearch WebFetch" \
    --permission-mode acceptEdits \
    ${VERBOSE_FLAGS[@]+"${VERBOSE_FLAGS[@]}"} \
    | "$PY" collect/tally.py --format "$OUT_FORMAT" \
        --stage "$STAGE_LABEL" --iter "$i" --ledger "$USAGE_LEDGER" \
        ${RAW_FLAGS[@]+"${RAW_FLAGS[@]}"} \
    || echo "  ! iteration $i failed; continuing."

  after="$(count)"; after="${after:-0}"
  if [ "$after" -le "$now" ]; then
    stall=$(( stall + 1 ))
    echo "  (no new rows this iteration; stall $stall/$MAX_STALL)"
    [ "$stall" -ge "$MAX_STALL" ] && { echo "no new leads in $MAX_STALL iterations; stopping."; hit_cap=0; break; }
  else
    stall=0
  fi
done

# Running out of turns used to look exactly like finishing: the `for` simply
# ended and the same "done." line printed. Say it, loudly, or a truncated run
# reads as a complete one. NOT an error exit -- the stage did real work, and
# all.sh skips Screen when Source exits non-zero, which would strand every row
# this run collected.
if [ "$hit_cap" = "1" ]; then
  final="$(count)"; final="${final:-0}"
  echo
  echo "!! ==================================================================="
  echo "!!  STOPPED AT THE ITERATION CAP -- THIS RUN IS INCOMPLETE"
  echo "!!"
  echo "!!    added $(( final - START ))/$ADD rows in $MAX_ITERS turns"
  echo "!!"
  echo "!!  Re-run with a higher MAX_ITERS. Nothing is collected twice: the"
  echo "!!  database de-duplicates, so a re-run continues where this stopped."
  echo "!! ==================================================================="
  echo
fi

# Only when this stage owns the ledger. Under all.sh the parent prints one
# summary for both stages, and printing it here too would report the Source
# stage's numbers as if they were the whole run.
# --ledger-note only when the ledger outlives the run; under LOG=0 it is a
# temporary the trap deletes, and naming it would point at nothing.
NOTE_FLAG=()
[ "$LOG" != "0" ] && NOTE_FLAG=(--ledger-note)
[ "$LEDGER_OWNER" = "1" ] && "$PY" collect/tally.py --summary \
    --ledger "$USAGE_LEDGER" ${NOTE_FLAG[@]+"${NOTE_FLAG[@]}"}

echo "done. Final:"; "$PY" -m pipeline.cli status
