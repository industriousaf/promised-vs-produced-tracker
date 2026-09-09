#!/usr/bin/env bash
#
# all.sh -- run the Source stage and then the Screen stage for N
# datapoints, in one command.
#
# source.sh and screen.sh are the same outer loop pointed at different prompts and
# counting different tables, so the normal session is "collect N leads, then
# extract N rows" -- two commands and a wait in between. This runs the pair
# back-to-back and prints one before/after summary.
#
# RUN IT (from anywhere; it cd's to scoreboard/ itself. bash on macOS/Linux/WSL):
#   bash collect/all.sh            # N=10 to each stage
#   N=3 bash collect/all.sh        # 3 leads, then 3 extractions
#   N=5 DRY_RUN=1 bash collect/all.sh   # show the plan, call nothing
#
#   # different sizes per stage, and only one stage:
#   SOURCE_ADD=10 SCREEN_ADD=4 bash collect/all.sh
#   ONLY=screen N=5 bash collect/all.sh
#
#   # per-stage model/effort (screen extraction is the fiddlier job):
#   SOURCE_EFFORT=medium SCREEN_EFFORT=high N=8 \
#     bash collect/all.sh
#
# CONFIG
#   Shared knobs are passed to BOTH stages; every one can be overridden for a
#   single stage with a SOURCE_ / SCREEN_ prefix, which wins over the shared
#   value:
#
#     N            how many rows to add at each stage        (default 10)
#     ONLY         source | screen | both                    (default both)
#     MODEL        Claude model each iteration runs          (see below)
#     EFFORT       low | medium | high                       (see below)
#
#   MODEL and EFFORT default from pipeline/settings.py -- the one place the
#   names live. `python3 scoreboard.py models` says what is in effect and
#   what decided it. MODEL= on the command line changes every stage for one
#   run; SOURCE_MODEL= / SCREEN_MODEL= change a single stage and win over it.
#     MAX_ITERS    cost cap on loop turns per stage          (default 3x ADD)
#     MAX_STALL    give up after N no-progress iterations    (default 3)
#     VERBOSE      1 = stream tool calls                     (default 0)
#     PROMPT_FILE  operating prompt for the stage            (per-stage defaults)
#     COUNT_TABLE  table the stage counts                    (per-stage defaults)
#
#     e.g. SCREEN_MAX_STALL=5, SOURCE_MODEL=claude-sonnet-5, SCREEN_VERBOSE=1
#
#   Plus:
#     LOG              transcript path; LOG=0 disables    (default logs/<utc>-collect.log)
#                      Two machine files sit beside it, sharing its name:
#                        <run>-usage.jsonl  the token ledger, one line per
#                                           iteration -- always written
#                        <run>.raw.jsonl    every raw stream event, and only
#                                           under VERBOSE=1
#     DRY_RUN=1        print the two commands and exit without calling Claude
#     PREFLIGHT=0      skip the "is claude authenticated?" probe entirely
#     CONTINUE_ON_FAIL=1  run Screen even if Source exited non-zero
#
# NOTE ON COST: each stage starts one `claude -p` process per iteration, so
# `N=10` on both stages is ~20+ runs. Start small.
#
set -euo pipefail

trap 'echo; echo "interrupted -- stopping."; exit 130' INT TERM

HERE="$(cd "$(dirname "$0")" && pwd)"
SCOREBOARD_ROOT="$(cd "$HERE/.." && pwd)"
SOURCE_LOOP="$HERE/source.sh"
SCREEN_LOOP="$HERE/screen.sh"
for f in "$SOURCE_LOOP" "$SCREEN_LOOP"; do
  [ -f "$f" ] || { echo "ERROR: missing $f"; exit 1; }
done

cd "$SCOREBOARD_ROOT"
PY="${PY:-$(command -v python3 || command -v python)}"

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
  [ -n "$LOG" ] || LOG="logs/$(date -u +%Y%m%dT%H%M%SZ)-collect.log"
  mkdir -p "$(dirname "$LOG")"
  export LOG LOG_ACTIVE=1
  exec > >(tee -a "$LOG") 2>&1
fi

# --- The other two files ---------------------------------------------------- #
# The transcript is for reading, so it gets only what a human wants to read.
# Two machine files sit beside it, named after the same run:
#
#   <run>-usage.jsonl  one result object per iteration. Both stages append to
#                      this one file, which is what lets the summary at the end
#                      report the whole run rather than the last stage of it.
#   <run>.raw.jsonl    every raw stream event, VERBOSE=1 only -- the firehose.
#                      It used to go into the .log itself, which is how a 20-row
#                      run produced 6.5MB nobody could print.
#
# Exported, so the stages append here instead of opening their own pair. Same
# parent-owns-it arrangement as LOG_ACTIVE just above. LOG=0 leaves the ledger
# on a temporary file rather than skipping it: the summary still has to have
# something to total, and a run that asked for no logs still asked for a total.
if [ "$LOG" != "0" ]; then
  USAGE_LEDGER="${LOG%.log}-usage.jsonl"
  RAW_LOG="${LOG%.log}.raw.jsonl"
else
  USAGE_LEDGER="$(mktemp)"
  RAW_LOG=""
  trap 'rm -f "$USAGE_LEDGER"' EXIT
fi
export USAGE_LEDGER RAW_LOG


# --- Config ---------------------------------------------------------------- #
N="${N:-10}"
ONLY="${ONLY:-both}"
case "$ONLY" in source|screen|both) ;; *)
  echo "ERROR: ONLY must be source | screen | both (got '$ONLY')"; exit 1 ;;
esac

DRY_RUN="${DRY_RUN:-0}"
CONTINUE_ON_FAIL="${CONTINUE_ON_FAIL:-0}"

# stage_cfg <STAGE> <NAME> <fallback> -- the per-stage value if set, else the
# shared value if set, else the fallback. Lets SCREEN_EFFORT beat EFFORT.
stage_cfg() {
  local stage="$1" name="$2" fallback="$3" specific shared
  specific="${stage}_${name}"
  shared="$name"
  if [ -n "${!specific:-}" ]; then printf '%s' "${!specific}"
  elif [ -n "${!shared:-}" ]; then printf '%s' "${!shared}"
  else printf '%s' "$fallback"; fi
}

# See the note in source.sh: one integer, and screen_extracted counts distinct
# projects rather than rows so a duplicate extraction cannot tick the counter.
count() { "$PY" -m pipeline.cli count "$1"; }

# --- Run one stage --------------------------------------------------------- #
# Each stage is launched with an explicit, self-contained env (env -u strips any
# PROMPT_FILE/COUNT_TABLE inherited from the caller) so the Source stage's
# settings can never leak into the Screen stage.
run_stage() {
  local stage="$1" script="$2" default_prompt="$3" default_table="$4" preflight="$5"
  local add prompt table model effort iters stall verbose iters_default

  add="$(stage_cfg   "$stage" ADD         "$N")"
  prompt="$(stage_cfg "$stage" PROMPT_FILE "$default_prompt")"
  table="$(stage_cfg "$stage" COUNT_TABLE "$default_table")"
  # Not stage_cfg: pipeline/settings.py holds the names AND the
  # SOURCE_MODEL / SCREEN_MODEL / MODEL precedence, so asking it keeps one
  # implementation of that rule instead of a shell copy that can drift from it.
  model="$("$PY" -m pipeline.cli models --for "$stage")"
  effort="$("$PY" -m pipeline.cli models --for "$stage" --effort)"
  # Every default below comes from pipeline/settings.py. This block used to carry
  # its own copies, and all.sh's flat MAX_ITERS of 200 once shadowed
  # source.sh's scaling default -- a run asking for 300 rows capped at 200,
  # silently.
  iters="$(stage_cfg "$stage" MAX_ITERS   "$("$PY" -m pipeline.cli config --for max-iters --add "$add")")"
  stall="$(stage_cfg "$stage" MAX_STALL "$("$PY" -m pipeline.cli config --for max-stall --stage "$stage")")"
  verbose="$(stage_cfg "$stage" VERBOSE "$("$PY" -m pipeline.cli config --for verbose --stage "$stage")")"
  # Source only. The Screen prompt fixes one lead per call in its own text and
  # has no ceiling to set.
  leads="$(stage_cfg "$stage" LEADS_PER_CALL "$("$PY" -m pipeline.cli config --for leads-per-call --stage "$stage")")"

  echo
  echo "==================================================================="
  echo "  ${stage}: add $add to $table"
  echo "  prompt=$prompt"
  echo "  model=$model effort=$effort max_iters=$iters max_stall=$stall verbose=$verbose"
  [ "$table" = "source_collected" ] && echo "  leads_per_call=$leads"
  echo "==================================================================="

  local cmd=(env -u PROMPT_FILE -u COUNT_TABLE
    "ADD=$add" "PROMPT_FILE=$prompt" "COUNT_TABLE=$table" "STAGE_LABEL=$stage"
    "MODEL=$model" "EFFORT=$effort" "MAX_ITERS=$iters"
    "MAX_STALL=$stall" "VERBOSE=$verbose" "PREFLIGHT=$preflight"
    "LEADS_PER_CALL=$leads"
    bash "$script")

  if [ "$DRY_RUN" = "1" ]; then
    printf '  DRY RUN, would execute:\n    '
    printf '%q ' "${cmd[@]}"; printf '\n'
    return 0
  fi
  "${cmd[@]}"
}

# --- Plan ------------------------------------------------------------------ #
src_before="$(count source_collected)"; src_before="${src_before:-0}"
scr_before="$(count screen_extracted)"; scr_before="${scr_before:-0}"

echo "database : $("$PY" -c 'from pipeline.db import db_path; print(db_path())')"
echo "start    : source_collected=$src_before  screen_extracted=$scr_before"
echo "plan     : ONLY=$ONLY  N=$N"
if [ "$LOG" != "0" ]; then
  echo "log      : $LOG                (readable)"
  echo "usage    : $USAGE_LEDGER  (tokens per iteration)"
  [ "${VERBOSE:-0}" = "1" ] && echo "raw      : $RAW_LOG   (every stream event)"
fi

# The authentication probe costs one Claude call, so run it in the FIRST stage
# only and skip it in the second -- by then we already know the CLI works.
first_preflight="${PREFLIGHT:-1}"

rc=0
if [ "$ONLY" = "both" ] || [ "$ONLY" = "source" ]; then
  run_stage SOURCE "$SOURCE_LOOP" \
    "collect/prompts/prompt1_collect_recent.md" \
    "source_collected" "$first_preflight" || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo
    echo "!! SOURCE stage exited non-zero ($rc)."
    if [ "$CONTINUE_ON_FAIL" != "1" ]; then
      echo "   Stopping before Screen. Set CONTINUE_ON_FAIL=1 to run it anyway"
      echo "   (Screen can still extract from Source leads collected earlier)."
      exit "$rc"
    fi
    echo "   CONTINUE_ON_FAIL=1 -- carrying on to Screen."
  fi
  first_preflight=0     # authentication already proven
fi

if [ "$ONLY" = "both" ] || [ "$ONLY" = "screen" ]; then
  run_stage SCREEN "$SCREEN_LOOP" \
    "collect/prompts/prompt2_extract_screen.md" \
    "screen_extracted" "$first_preflight"
fi

# --- Summary --------------------------------------------------------------- #
if [ "$DRY_RUN" = "1" ]; then
  echo; echo "dry run -- nothing was called."; exit 0
fi

src_after="$(count source_collected)"; src_after="${src_after:-0}"
scr_after="$(count screen_extracted)"; scr_after="${scr_after:-0}"

echo
echo "==================================================================="
echo "  done"
echo "    source_collected  $src_before -> $src_after  (+$(( src_after - src_before )))"
echo "    screen_extracted  $scr_before -> $scr_after  (+$(( scr_after - scr_before )))"
echo "==================================================================="

# What the run spent. Both stages appended to one ledger, so this is the whole
# run: total tokens, split by where they went and which model spent them.
# --ledger-note names the ledger file, which is worth saying only when the
# file will still be there afterwards. Under LOG=0 it is a temporary the trap
# above deletes on the way out, so pointing at it would be an instruction to
# open a path that no longer exists.
NOTE_FLAG=()
[ "$LOG" != "0" ] && NOTE_FLAG=(--ledger-note)
"$PY" collect/tally.py --summary --ledger "$USAGE_LEDGER" \
    ${NOTE_FLAG[@]+"${NOTE_FLAG[@]}"}

echo
"$PY" -m pipeline.cli status
