#!/usr/bin/env python3
"""tally.py -- say what a collection run actually spent.

WHY THIS EXISTS
    Every `claude -p` call ends by reporting exactly how many tokens it used,
    which models it used them on, and how many turns it took. Until this file,
    nothing in the repo read that report. A quiet run threw it away at the CLI
    boundary (`--output-format text` prints the answer and nothing else), and a
    VERBOSE=1 run buried it in six megabytes of raw stream-json. Either way the
    question "how many tokens did that cost?" had no answer.

    So: every iteration now pipes its output through here. Two modes.

      --stage/--iter   read ONE iteration's output on stdin. Print the text a
                       human would have seen, then one line of accounting.
                       Append the raw result object to the ledger.

      --summary        read the ledger, print the run's totals.

COUNTING TOKENS CORRECTLY -- THE TRAP
    The result object carries usage figures in two places, and the obvious one
    is wrong. Top-level `usage` reports the MAIN model only. A real run also
    spends tokens on whatever model the CLI picks for web search, and those
    never appear there. On the N=20 run that measured 12,426,236 tokens against
    a true 13,430,362 -- a million tokens, 7.5%, silently missing.

    `modelUsage` has one entry per model actually used. Everything here sums
    that, so the parts add up to the whole and the total is the real one.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

# The pipeline needs 3.9 or newer; tracker.py says so at the door. Nothing
# in this file depends on anything later.


# --------------------------------------------------------------------------- #
# Reading one iteration                                                        #
# --------------------------------------------------------------------------- #

def _short(model: str) -> str:
    """`claude-opus-4-8` -> `opus-4-8`. The vendor prefix is on every name, so
    it distinguishes nothing and costs width on a line that needs it."""
    return model[len("claude-"):] if model.startswith("claude-") else model


def _usage(result: dict) -> Counter:
    """Total tokens in one result object, summed from `modelUsage`.

    Read the module docstring before changing this to use `usage` instead."""
    t = Counter()
    for name, mu in (result.get("modelUsage") or {}).items():
        # canonicalModel drops the date suffix: claude-haiku-4-5-20251001 is
        # reported under two different spellings across a run otherwise.
        short = _short(mu.get("canonicalModel") or name)
        got = (mu.get("inputTokens") or 0,
               mu.get("cacheReadInputTokens") or 0,
               mu.get("cacheCreationInputTokens") or 0,
               mu.get("outputTokens") or 0)
        t["fresh"] += got[0]
        t["cache_read"] += got[1]
        t["cache_write"] += got[2]
        t["out"] += got[3]
        t["model:" + short] += sum(got)
        t["search:" + short] += mu.get("webSearchRequests") or 0
    # thinking is a subset of output, and only the top-level usage breaks it
    # out -- it is a detail of the main model, so there is no per-model version
    # to sum. Absent on older CLIs, hence the chain of gets.
    t["thinking"] += ((result.get("usage") or {})
                      .get("output_tokens_details") or {}).get("thinking_tokens", 0)
    t["total"] = t["fresh"] + t["cache_read"] + t["cache_write"] + t["out"]
    return t


def _read_single_json(text: str):
    """--output-format json: the whole of stdin is one result object."""
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) and obj.get("type") == "result" else None


def _read_stream(lines, raw_out) -> tuple[dict | None, list[str]]:
    """--output-format stream-json: one JSON event per line.

    Returns the terminal `result` event plus a compact human trace. The raw
    events go to their own file if one was given: they are the full record and
    worth keeping, but putting them in the readable log is what made the last
    run's log 6.5MB and unprintable.
    """
    result, trace = None, []
    for line in lines:
        if raw_out is not None:
            raw_out.write(line)
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            # Not JSON -- a wrapper's stderr, or a crash. Keep it: an
            # unparsable line in a failing run is the evidence.
            trace.append(f"      {line[:160]}")
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "result":
            result = ev
        elif ev.get("type") == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    trace.append(f"      . {block.get('name')}")
                elif block.get("type") == "text":
                    said = " ".join((block.get("text") or "").split())
                    if said:
                        trace.append(f"      > {said[:150]}")
    return result, trace


def cmd_iteration(args) -> int:
    raw_out = open(args.raw, "a", encoding="utf-8") if args.raw else None
    try:
        if args.format == "stream-json":
            result, trace = _read_stream(sys.stdin, raw_out)
            for line in trace:
                print(line)
        else:
            text = sys.stdin.read()
            result = _read_single_json(text)
            if result is None:
                # Not the JSON we expected. Print it verbatim rather than
                # swallowing it -- an iteration that failed says why here, and
                # this script must never be the reason that is lost.
                sys.stdout.write(text)
                return 0
            said = (result.get("result") or "").strip()
            if said:
                print("      " + said.replace("\n", "\n      "))
    finally:
        if raw_out is not None:
            raw_out.close()

    if result is None:
        print("      (no usage reported by this iteration)")
        return 0

    t = _usage(result)
    turns = result.get("num_turns") or 0
    secs = (result.get("duration_ms") or 0) / 1000.0
    print(f"    tokens {t['total']:>10,}   cache-r {t['cache_read']:>9,}  "
          f"fresh {t['fresh']:>7,}  out {t['out']:>6,}   "
          f"{turns:>2} turns  {secs:>5.1f}s")

    # Name the models. The stage banner prints the MODEL knob, which is only
    # the model doing the pipeline work -- the CLI picks its own for web
    # search, and that one spends real tokens under a name nobody asked for.
    models = sorted((k[len("model:"):] for k in t if k.startswith("model:")),
                    key=lambda m: -t["model:" + m])
    if len(models) > 1:
        parts = []
        for m in models:
            searches = t["search:" + m]
            parts.append(f"{m} {t['model:' + m]:,}"
                         + (f" ({searches} searches)" if searches else ""))
        print("      via " + "  +  ".join(parts))

    if args.ledger:
        with open(args.ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"stage": args.stage, "iter": args.iter,
                                 "result": result}) + "\n")
    return 0


# --------------------------------------------------------------------------- #
# The run summary                                                              #
# --------------------------------------------------------------------------- #

def cmd_summary(args) -> int:
    entries = []
    try:
        with open(args.ledger, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        return 0            # no ledger, nothing to say -- not an error

    if not entries:
        return 0

    total = Counter()
    per_stage: dict[str, Counter] = {}
    stage_iters: Counter = Counter()
    turns = 0
    secs = 0.0
    cost = 0.0
    for e in entries:
        r = e.get("result") or {}
        t = _usage(r)
        total.update(t)
        stage = e.get("stage") or "?"
        per_stage.setdefault(stage, Counter()).update(t)
        stage_iters[stage] += 1
        turns += r.get("num_turns") or 0
        secs += (r.get("duration_ms") or 0) / 1000.0
        cost += r.get("total_cost_usd") or 0.0

    into = total["fresh"] + total["cache_read"] + total["cache_write"]
    grand = into + total["out"]
    pct = (100.0 * total["cache_read"] / grand) if grand else 0.0

    print()
    print(f"  TOKENS  {grand:>24,}")
    print()
    print(f"    into the model        {into:>16,}")
    print(f"      fresh input         {total['fresh']:>16,}")
    print(f"      cache read          {total['cache_read']:>16,}   "
          f"{pct:.0f}% of everything")
    print(f"      cache write         {total['cache_write']:>16,}")
    print(f"    out of the model      {total['out']:>16,}")
    print(f"      text                {total['out'] - total['thinking']:>16,}")
    print(f"      thinking            {total['thinking']:>16,}")
    print()
    print("  BY MODEL")
    models = sorted((k[len("model:"):] for k in total if k.startswith("model:")),
                    key=lambda m: -total["model:" + m])
    for m in models:
        searches = total["search:" + m]
        note = f"{searches} web searches" if searches else ""
        print(f"    {m:<22} {total['model:' + m]:>14,}   {note}")
    print()
    print("  BY STAGE")
    for stage, t in sorted(per_stage.items(), key=lambda kv: -kv[1]["total"]):
        n = stage_iters[stage]
        print(f"    {stage:<8} {n:>2} iters  {t['total']:>12,} tokens  "
              f"{t['total'] // max(n, 1):>10,}/iter")
    print()
    print(f"  {len(entries)} iterations, {turns} turns, {secs / 60:.1f} min, ${cost:.2f}")
    if args.ledger_note:
        print(f"  per-iteration detail: {args.ledger}")
    return 0


# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="tally.py",
        description="Report the tokens a collection run spent.")
    p.add_argument("--summary", action="store_true",
                   help="print the run totals from the ledger instead of "
                        "reading an iteration on stdin")
    p.add_argument("--ledger", default="",
                   help="JSONL file of per-iteration result objects")
    p.add_argument("--ledger-note", action="store_true",
                   help="with --summary, name the ledger file in the output")
    p.add_argument("--raw", default="",
                   help="with --format stream-json, write every raw event here")
    p.add_argument("--format", default="json",
                   choices=("json", "stream-json"),
                   help="how the iteration's output is formatted (default json)")
    p.add_argument("--stage", default="?", help="SOURCE or SCREEN")
    p.add_argument("--iter", type=int, default=0, help="iteration number")
    args = p.parse_args(argv)
    return cmd_summary(args) if args.summary else cmd_iteration(args)


if __name__ == "__main__":
    sys.exit(main())
