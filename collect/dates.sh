#!/usr/bin/env bash
#
# dates.sh -- drive PROMPT 3 (find the missing first-output date) over the rows
# that have produced but carry no date for it.
#
# WHY THIS IS NOT source.sh:
#   source.sh runs an open-ended search -- "add 10 rows" -- so it needs a target
#   count, a stall detector and an iteration cap to decide when to stop. This job
#   has none of that shape. The work is a KNOWN, FINITE list read out of the
#   database before the first call, so a `for` over that list terminates by
#   construction. No counter, no stall, no cap, and nothing to get wrong.
#
#   Re-running is safe and is the retry: a resolved row leaves the queue on its
#   own, because the queue is "lag_years = -4.0" and screen-date changes it.
#
# RUN IT (from anywhere. bash on macOS/Linux/WSL):
#   bash collect/dates.sh              # every undated row, one call each
#   DRY_RUN=1 bash collect/dates.sh    # print the queue and stop
#   IDS="3 6 37" bash collect/dates.sh # just these rows
#   LIMIT=5 bash collect/dates.sh      # the first 5 of the queue
#   RETRY_UNRESOLVED=1 bash collect/dates.sh   # include rows already searched
#
# Knobs: MODEL, EFFORT, VERBOSE, LOG, PREFLIGHT -- same meanings as source.sh.
set -euo pipefail

trap 'echo; echo "interrupted -- stopping."; exit 130' INT TERM

cd "$(dirname "$0")/.."                            # collect/ -> scoreboard/
PY="${PY:-$(command -v python3 || command -v python)}"

# This is a Screen-stage job on Screen rows, so it runs the Screen model rather
# than introducing a fourth slot in models.py for one script.
MODEL="$("$PY" -m pipeline.cli models --for SCREEN)"
EFFORT="$("$PY" -m pipeline.cli models --for SCREEN --effort)"
VERBOSE="${VERBOSE:-0}"

CLAUDE="$(command -v claude || echo "$HOME/.local/bin/claude")"
[ -x "$CLAUDE" ] || { echo "ERROR: claude CLI not found ($CLAUDE)"; exit 1; }

# --- The queue -------------------------------------------------------------- #
# Read ONCE, before any call. The list cannot grow while the run is in flight,
# so every row gets exactly one attempt and the run is bounded before it starts.
queue() {
  "$PY" - <<'PYEOF'
import os
from pipeline.db import connect
from pipeline.screen import undated_produced
retry = os.getenv("RETRY_UNRESOLVED", "0") not in ("", "0", "false", "no")
with connect() as conn:
    print(" ".join(str(r["id"]) for r in undated_produced(conn, include_searched=retry)))
PYEOF
}

IDS="${IDS:-$(queue)}"
# shellcheck disable=SC2206
ID_LIST=($IDS)
if [ -n "${LIMIT:-}" ]; then
  ID_LIST=("${ID_LIST[@]:0:$LIMIT}")
fi
TOTAL="${#ID_LIST[@]}"

if [ "$TOTAL" -eq 0 ]; then
  echo "nothing to do: no rows are 'produced, date unknown' (-4.0)."
  exit 0
fi

if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "would work $TOTAL row(s): ${ID_LIST[*]}"
  RUN_IDS="${ID_LIST[*]}" "$PY" - <<'PYEOF'
import os
from pipeline.db import connect
from pipeline.screen import get_extracted
with connect() as conn:
    for i in os.environ["RUN_IDS"].split():
        r = get_extracted(conn, int(i))
        if r is None:
            print(f"  #{i:<4} NO SUCH ROW"); continue
        print(f"  #{r['id']:<4} {r['project'][:58]:<58} promised {r['promised_first_output'] or '-'}")
PYEOF
  exit 0
fi

# --- Preflight: the claude CLI must be authenticated ------------------------ #
if [ "${PREFLIGHT:-1}" = "1" ]; then
  if command -v timeout >/dev/null 2>&1; then TIMEOUT="timeout 120"
  elif command -v gtimeout >/dev/null 2>&1; then TIMEOUT="gtimeout 120"
  else TIMEOUT=""; fi
  probe="$($TIMEOUT "$CLAUDE" -p "Reply with exactly: READY" \
             --model "$MODEL" --permission-mode default 2>&1 || true)"
  if ! printf '%s' "$probe" | grep -q "READY"; then
    echo "ERROR: claude preflight failed. First response was:"
    printf '  %s\n' "$probe" | head -5
    echo "If it says 'Not logged in', run this once:  claude   (then  /login)"
    exit 1
  fi
  echo "preflight ok (claude authenticated)."
fi

PROMPT="$(cat collect/prompts/prompt3_find_dates.md)"

# --- Transcript + accounting, the same three files source.sh writes --------- #
LOG="${LOG:-}"
if [ "$LOG" != "0" ]; then
  [ -n "$LOG" ] || LOG="logs/$(date -u +%Y%m%dT%H%M%SZ)-dates.log"
  mkdir -p "$(dirname "$LOG")"
  exec > >(tee -a "$LOG") 2>&1
  USAGE_LEDGER="${LOG%.log}-usage.jsonl"
  RAW_LOG="${LOG%.log}.raw.jsonl"
else
  USAGE_LEDGER="$(mktemp)"; RAW_LOG=""
  trap 'rm -f "$USAGE_LEDGER"' EXIT
fi

OUT_FORMAT=json
VERBOSE_FLAGS=(--output-format json)
RAW_FLAGS=()
if [ "$VERBOSE" = "1" ]; then
  OUT_FORMAT=stream-json
  VERBOSE_FLAGS=(--verbose --output-format stream-json)
  [ -n "$RAW_LOG" ] && RAW_FLAGS=(--raw "$RAW_LOG")
fi

echo "started : $(date -u +%Y-%m-%dT%H:%M:%SZ)  db=$("$PY" -c 'from pipeline.db import db_path; print(db_path())')"
echo "dates: $TOTAL undated row(s) (model=$MODEL, effort=$EFFORT)"
echo "queue: ${ID_LIST[*]}"

i=0
for id in "${ID_LIST[@]}"; do
  i=$(( i + 1 ))
  echo "=== $i/$TOTAL - screen row #$id ==="

  "$CLAUDE" -p "@docs/cli.md is attached as reference context -- it is this pipeline's CLI reference. Never run a shell script from collect/ (that is the loop that launched you), never promote to Verify (verify-promote / --promote-tier) -- that is a human-only gate -- and never add or delete rows (screen-add / screen-remove). The ONE row to work in this call is screen_extracted id $id, and the ONE write you may make is a single screen-date on it. Now carry out your operating instructions (in the system prompt)." \
    --model "$MODEL" \
    --effort "$EFFORT" \
    --append-system-prompt "$PROMPT" \
    --allowedTools "Bash Read Write WebSearch WebFetch" \
    --permission-mode acceptEdits \
    ${VERBOSE_FLAGS[@]+"${VERBOSE_FLAGS[@]}"} \
    | "$PY" collect/tally.py --format "$OUT_FORMAT" \
        --stage DATES --iter "$i" --ledger "$USAGE_LEDGER" \
        ${RAW_FLAGS[@]+"${RAW_FLAGS[@]}"} \
    || echo "  ! row #$id failed; continuing."

  # Run the checker here rather than asking the model to. It is deterministic,
  # it knows the id already, and a step the model can forget is a step that
  # eventually gets forgotten.
  "$PY" -m pipeline.cli screen-check --id "$id" 2>&1 | sed 's/^/  /' || true
done

NOTE_FLAG=()
[ "$LOG" != "0" ] && NOTE_FLAG=(--ledger-note)
"$PY" collect/tally.py --summary --ledger "$USAGE_LEDGER" ${NOTE_FLAG[@]+"${NOTE_FLAG[@]}"}

echo
echo "done. Rows still undated:"
"$PY" - <<'PYEOF'
from pipeline.db import connect
from pipeline.screen import undated_produced, published_undated, UNRESOLVED_MARKER
with connect() as conn:
    rows = undated_produced(conn, include_searched=True)
    if not rows:
        print("  none -- every unpublished produced row now carries a date.")
    for r in rows:
        searched = UNRESOLVED_MARKER in (r["flag"] or "")
        print(f"  #{r['id']:<4} {'searched, none found' if searched else 'not attempted'}"
              f"  {r['project'][:52]}")

    pub = published_undated(conn)
    if pub:
        print()
        print(f"  {len(pub)} PUBLISHED row(s) are still undated. This loop cannot")
        print("  touch them -- Verify is a human gate. Each needs one command:")
        for r, vid in pub:
            print(f"    scoreboard.py verify-edit --id {vid} \\")
            print(f"        --set actual_first_output=YYYY-MM --set actual_date_source=URL \\")
            print(f"        --desc \"first output dated from <source>\"   # {r['project'][:44]}")
PYEOF
