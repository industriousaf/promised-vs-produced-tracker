"""
gather.py -- batch collection driver for the medallion pipeline via the direct
Anthropic API (flavour B in llm.py), NOT Claude Code.

This is the batch shape the pipeline wants: gather *many* Source leads in one
run, then extract only *some* of them into Screen. Finding a lead is cheap (two
links and a summary); extracting one is not (open both links, fill 17 columns),
and publishing costs a person's attention. So the funnel is expected to be steep:

    #source_collected  >>  #screen_extracted  >  #verify_verified

So the two counts are independent hyperparameters: `--n-source` leads are
collected from the web, and only the first `--n-screen` of *those* are extracted
(default 0 -- Source only). The deterministic `screen_check` runs on every new
Screen row. Verify is deliberately NOT touched: promotion to Verify is the human
verification gate and is done from the CLI/web when a person signs off.

Needs an ANTHROPIC_API_KEY (this is the API path). No key? Use the Claude Code
prompts instead (`pipeline.cli source-prompt` / `screen-prompt`).

The key (and TRACKER_DB / PIPELINE_MODEL / PIPELINE_EFFORT) can come from a real
shell env var, OR from `config.env` in the repository root -- see
`_load_config_env` below. Nothing here uploads or logs that value; it only ever
lands in `os.environ` for the anthropic SDK to read.

Run it from the repository root:

    python3 tools/gather.py --n-source 10                 # 10 leads, Source only
    python3 tools/gather.py --n-source 10 --n-screen 3    # + extract the first 3
    python3 tools/gather.py --n-source 5 --dry-run        # show the plan, no API calls
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# tools/ -> tracker/, so `pipeline` imports resolve.
_TRACKER_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_TRACKER_ROOT))

import pipeline  # noqa: E402  (must follow the sys.path line above)

# config.env sits in the repository root, beside tracker.py, because that
# is where every command is run from. It is gitignored, and `import pipeline`
# above has already loaded it -- this file used to own that loader, back when it
# was the only thing that needed a key. These two names are kept because
# --dry-run reports the path it looked in, and the call below is harmless
# (setdefault) and says out loud that the settings are in place.
_CONFIG_ENV_PATH = pipeline.CONFIG_ENV
_load_config_env = pipeline.load_config_env

# The pipeline prints operating prompts containing non-ASCII (>=, ~); the Windows
# console defaults to cp1252, so force UTF-8 like cli.py does.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gather",
        description=(
            "Batch-collect N Source leads (and optionally extract some into "
            "Screen) via the direct Anthropic API. Verify is left to the human gate."
        ),
    )
    p.add_argument("--n-source", type=int, default=5,
                   help="how many new Source leads to collect from the web (default 5)")
    p.add_argument("--n-screen", type=int, default=0,
                   help="how many of the newly-collected leads to extract into "
                        "Screen (default 0 = Source only). Capped at --n-source; "
                        "keeping this < --n-source is the intended source >> screen shape.")
    p.add_argument("--no-check", dest="check", action="store_false",
                   help="skip the deterministic screen_check on new Screen rows")
    p.add_argument("--model", help="override PIPELINE_MODEL (e.g. claude-opus-4-8)")
    p.add_argument("--effort", help="override PIPELINE_EFFORT (e.g. high, medium)")
    p.add_argument("--db", help="path to the SQLite db (overrides TRACKER_DB)")
    p.add_argument("--sleep", type=float, default=0.0,
                   help="seconds to pause between API calls (rate-limit friendly)")
    p.add_argument("--stop-on-error", action="store_true",
                   help="abort the batch on the first failure (default: skip and continue)")
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan and validate wiring without any API calls")
    return p


def _apply_env(args) -> None:
    """Push CLI hyperparameters into the env the pipeline modules read.

    Done BEFORE importing llm/db so their module-level `os.getenv` defaults
    (PIPELINE_MODEL / PIPELINE_EFFORT) pick these up.
    """
    if args.db:
        os.environ["TRACKER_DB"] = args.db
    if args.model:
        os.environ["PIPELINE_MODEL"] = args.model
    if args.effort:
        os.environ["PIPELINE_EFFORT"] = args.effort


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    n_source = max(0, args.n_source)
    n_screen = max(0, min(args.n_screen, n_source))  # source >> screen: never exceed
    do_check = args.check

    _load_config_env()  # fills gaps from config.env; a real shell env var wins
    _apply_env(args)    # CLI flags always win, over both the file and the shell

    # Deferred imports: env must be set first (see _apply_env).
    from pipeline import screen, orchestrate as orch, llm  # noqa: E402
    from pipeline.db import connect, init_db, table_counts  # noqa: E402

    conn = connect()
    init_db(conn)

    before = table_counts(conn)
    key_status = "found (value never printed)" if os.getenv("ANTHROPIC_API_KEY") \
        else "NOT FOUND -- API calls will fail"
    print("gather -- batch collection via the Anthropic API")
    print("=" * 60)
    print(f"  model         {llm.MODEL}")
    print(f"  effort        {llm.EFFORT}")
    print(f"  db            {conn.execute('PRAGMA database_list').fetchone()[2]}")
    print(f"  api key       {key_status}")
    print(f"  n-source      {n_source}")
    print(f"  n-screen      {n_screen}  (capped at n-source)")
    print(f"  screen_check  {'on' if do_check else 'off'}")
    print(f"  verify          NOT touched -- promotion is the human gate")
    print("=" * 60)

    if args.dry_run:
        print("\n[dry-run] wiring OK. Would now:")
        print(f"  1. collect {n_source} Source lead(s) via llm.collect_source_lead()")
        print(f"     (steered away from {len(orch.published_project_names(conn))} "
              f"project(s) in verify_verified + "
              f"{len(orch.unpublished_project_names(conn))} collected but unpublished)")
        print(f"  2. extract the first {n_screen} into Screen via llm.extract_screen_row()")
        if do_check and n_screen:
            print(f"  3. run screen_check on the {n_screen} new Screen row(s)")
        print("\nNo API calls made. Drop --dry-run to run for real "
              "(needs ANTHROPIC_API_KEY).")
        conn.close()
        return 0

    # --- Phase 1: Source ---------------------------------------------------- #
    collected: list[int] = []
    print(f"\n[Source] collecting {n_source} lead(s)...")
    for i in range(n_source):
        try:
            bid, lead = orch.run_source_ai(conn)
            collected.append(bid)
            title = (lead.get("summary") or lead.get("promise_source") or "").strip()
            print(f"  [{i + 1}/{n_source}] source #{bid}  {title[:70]}")
        except Exception as e:  # LLMUnavailable or anything else -- keep the batch going
            print(f"  [{i + 1}/{n_source}] FAILED: {e}")
            if args.stop_on_error:
                print("  stopping (--stop-on-error).")
                break
        if args.sleep and i < n_source - 1:
            time.sleep(args.sleep)

    # --- Phase 2: Screen (only the first n_screen of what we just collected) - #
    extracted: list[int] = []
    to_extract = collected[:n_screen]
    if to_extract:
        print(f"\n[Screen] extracting {len(to_extract)} of {len(collected)} "
              f"collected lead(s)...")
        for i, bid in enumerate(to_extract):
            try:
                sid, row = orch.run_screen_ai(conn, bid)
                extracted.append(sid)
                print(f"  [{i + 1}/{len(to_extract)}] screen #{sid} "
                      f"<- source #{bid}  {row.get('project', '')[:60]}")
            except Exception as e:  # LLMUnavailable or anything else -- keep going
                print(f"  [{i + 1}/{len(to_extract)}] FAILED (source #{bid}): {e}")
                if args.stop_on_error:
                    print("  stopping (--stop-on-error).")
                    break
            if args.sleep and i < len(to_extract) - 1:
                time.sleep(args.sleep)

    # --- Phase 3: deterministic check -------------------------------------- #
    if do_check and extracted:
        print(f"\n[Check] running screen_check on {len(extracted)} new row(s)...")
        verdicts: dict[str, int] = {}
        for sid in extracted:
            result = screen.run_check(conn, sid)
            verdicts[result["result_status"]] = \
                verdicts.get(result["result_status"], 0) + 1
            print(f"  screen #{sid}: {result['result_status']} "
                  f"({result['n_errors']} err, {result['n_warnings']} warn)")
        print("  verdicts: " + ", ".join(f"{k}={v}" for k, v in sorted(verdicts.items())))

    # --- Summary ------------------------------------------------------------ #
    after = table_counts(conn)
    print("\n" + "=" * 60)
    print("gather complete")
    print(f"  source_collected  {before['source_collected']:>4} -> {after['source_collected']:>4}  "
          f"(+{after['source_collected'] - before['source_collected']})")
    print(f"  screen_extracted  {before['screen_extracted']:>4} -> {after['screen_extracted']:>4}  "
          f"(+{after['screen_extracted'] - before['screen_extracted']})")
    print(f"  screen_check      {before['screen_check']:>4} -> {after['screen_check']:>4}  "
          f"(+{after['screen_check'] - before['screen_check']})")
    print(f"  verify_verified     {after['verify_verified']:>4}  (unchanged -- human gate)")
    print("\nNext: review Screen rows, then promote verified ones to Verify from the "
          "CLI (`verify-promote`) or the web interface.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
