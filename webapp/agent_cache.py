"""
agent_cache.py -- the agentic check's background jobs and its on-disk answers.

Two problems, one module, because they are the same problem seen twice: an
answer that took forty seconds to get must survive the next thing that happens
to the page.

**The job registry** runs the check on a thread instead of inside the request.
The check is embedded in an `<iframe>`, so a blocking request already kept the
form usable -- but only until something reloaded the page. Reloads are routine
here: the promote form bounces back with "you changed a cell, give a reason",
and that used to throw away a check that was still in flight, with no way to
tell it had ever been asked for. Now the request only ever *attaches* to a job.
Reload as often as you like; the work carries on and the pane finds it again.

**The answer cache** keeps replies in `outputs/agent_cache/`, one JSON file per
answer, so asking the same question twice costs one API call. Gitignored: it is
not part of the data product, and nothing in the Tracker may depend on it --
delete the whole directory and the only consequence is that the next check pays
full price. It is emphatically NOT a verdict store. `verify_verified` records
what a person concluded; this records what a model said while they were deciding,
which is a different kind of thing and must never be mistaken for the first.

## What makes two questions the same question

The cache key is a fingerprint, not just the row id, because the obvious key is
wrong in a way that would quietly mislead. A reviewer asks about `announced`,
sees "CONFIRMED — the release says March 2019", corrects the cell to `2019-03`,
and asks again. Keyed on the row, they get the old answer back: a confirmation
of a value that is no longer in the cell. So the key covers everything the
answer depends on --

  * which row, in which database and stage (the same id means different
    projects in two databases, and the picker can switch between them);
  * which cells were ticked;
  * the current value of each ticked cell, and its verbatim `*_raw` partner;
  * every source URL on the row, since those are the pages being read;
  * the model that answered.

Change any of them and it is a different question with a different file. Edit a
cell and the old answer is not served again -- it stays on disk, as the record
of what was said about the value that used to be there.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

_ROOT = Path(__file__).resolve().parent.parent

# Beside the data product rather than inside it. `outputs/` is what someone
# clones this repository for; this directory is the one thing under it that is
# not part of that, which is why .gitignore names it explicitly.
CACHE_DIR = _ROOT / "outputs" / "agent_cache"

# Answers older than this are re-asked. A cited page can change -- a status page
# especially, which is the whole reason it is cited -- so an answer about it is
# a reading of a document on a day, not a fact. Two weeks is long enough that a
# review session never pays twice and short enough that a stale reading of a
# live page does not follow the row around for a season.
MAX_AGE = 14 * 24 * 60 * 60

# Files kept before the oldest are dropped. Each is a few KB; the cap exists so
# a directory nobody ever looks at cannot grow without limit.
KEEP_FILES = 400

# The fields folded into the fingerprint beyond the ticked cells themselves.
# Every source URL is here, not just the ones the ticked cells cite: the prompt
# shows the model all of them every time (a promised date missing from one
# document is often in the other), so all of them are part of the question.
_SOURCE_COLUMNS = ("promise_source", "status_source",
                   "promised_date_source", "actual_date_source")

_RAW_PARTNERS = {
    "announced": "announced_raw",
    "promised_first_output": "promised_first_output_raw",
    "actual_first_output": "actual_first_output_raw",
}


def _cell(row, col):
    """A row value that may not exist on this row (older databases lack the
    `*_raw` columns). Absent and empty are the same thing here."""
    try:
        v = row[col]
    except (IndexError, KeyError, TypeError):
        return ""
    return "" if v is None else str(v)


def fingerprint(stage: str, row_id: int, cells: list[str], row,
                model: str, db: str = "") -> str:
    """The cache key: a hash of everything the answer depends on.

    Hashed rather than spelled out because the parts include URLs and verbatim
    quotes, which do not belong in a filename. The parts themselves are written
    into the file, so a cache entry can always say what question it answers.
    """
    parts = [f"v1|{db}|{stage}|{row_id}|{model}"]
    for c in sorted(cells):
        parts.append(f"cell:{c}={_cell(row, c)}")
        raw = _RAW_PARTNERS.get(c)
        if raw:
            parts.append(f"raw:{raw}={_cell(row, raw)}")
    for c in _SOURCE_COLUMNS:
        parts.append(f"src:{c}={_cell(row, c)}")
    blob = "\n".join(parts).encode("utf-8", "replace")
    return hashlib.sha256(blob).hexdigest()[:16]


def _path(stage: str, row_id: int, key: str) -> Path:
    # stage and id are in the NAME, not just the hash, so every answer about one
    # row can be found with a glob -- which is how the page re-attaches to the
    # last question after a restart, with no index to keep in step.
    return CACHE_DIR / f"{stage}-{row_id:06d}-{key}.json"


# --------------------------------------------------------------------------- #
# The on-disk answers                                                          #
# --------------------------------------------------------------------------- #

def read(stage: str, row_id: int, key: str) -> dict | None:
    """A cached answer, or None if there is none, it is stale, or it is junk.

    Every failure here is a miss, never an exception. A cache that can break the
    review screen is worse than no cache: the screen's job is to show a person
    two documents, and it must not stop doing that because a JSON file was
    truncated by a crash.
    """
    p = _path(stage, row_id, key)
    try:
        if not p.exists():
            return None
        if time.time() - p.stat().st_mtime > MAX_AGE:
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("reply"):
        return None
    return data


def write(stage: str, row_id: int, key: str, payload: dict) -> None:
    """Store one answer. Best-effort: a cache that cannot be written is fine."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _path(stage, row_id, key)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(p)   # atomic, so a reader never sees a half-written answer
        _prune()
    except OSError:
        pass


def forget(stage: str, row_id: int, key: str) -> None:
    """Drop one cached answer, so the next ask really asks. This is what the
    pane's "ask again" link does."""
    try:
        _path(stage, row_id, key).unlink(missing_ok=True)
    except OSError:
        pass


def _prune(keep: int = KEEP_FILES) -> None:
    try:
        files = sorted(CACHE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for p in files[:-keep] if len(files) > keep else []:
        try:
            p.unlink()
        except OSError:
            pass


def entries_for(stage: str, row_id: int) -> list[dict]:
    """Every cached answer about one row, newest first.

    Read off the filesystem rather than an index, so it survives a restart and
    cannot fall out of step with what is actually on disk.
    """
    out = []
    try:
        paths = sorted(CACHE_DIR.glob(f"{stage}-{row_id:06d}-*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return out
    for p in paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("cells"):
            data["_mtime"] = p.stat().st_mtime
            out.append(data)
    return out


# --------------------------------------------------------------------------- #
# The background jobs                                                          #
# --------------------------------------------------------------------------- #

@dataclass
class Job:
    """One agentic check, in flight or finished.

    Lives in this process only. A job the server forgets on restart is a job
    whose answer was either already written to the cache or never arrived, and
    in both cases the page asks again -- so nothing needs to be persisted here.
    """
    key: str
    stage: str
    row_id: int
    cells: list[str]
    model: str
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    reply: str | None = None
    error: str | None = None
    error_kind: str | None = None      # "unavailable" (no key) | "failed"

    @property
    def running(self) -> bool:
        return self.finished_at is None

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.time()) - self.started_at


_JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()


def get(key: str) -> Job | None:
    with _LOCK:
        return _JOBS.get(key)


def start(stage: str, row_id: int, key: str, cells: list[str], model: str,
          work: Callable[[], str]) -> Job:
    """Attach to the job for `key`, starting it if it is not already going.

    Idempotent on purpose. Every reload of the review page re-requests the pane,
    and the answer to "is this already being asked?" has to be yes -- otherwise
    a reviewer who reloads twice pays for three identical API calls and watches
    the last one win.

    A job that FAILED counts as existing too, and is handed back rather than
    silently retried. A failure is usually a standing condition -- a revoked
    key, an unreachable host -- so retrying it on every reload would bill the
    same error repeatedly while the reviewer read the message telling them about
    it. Retrying is `drop()` then this, which is what the pane's "try again"
    link does.
    """
    with _LOCK:
        existing = _JOBS.get(key)
        if existing is not None:
            return existing
        job = Job(key=key, stage=stage, row_id=row_id,
                  cells=list(cells), model=model)
        _JOBS[key] = job

    def _run() -> None:
        try:
            reply = work()
        except BaseException as e:                      # noqa: BLE001
            # Wide on purpose: this is the top of a thread. An exception that
            # escapes here is printed to a console nobody is reading and the
            # pane waits for an answer that will never come, so every failure
            # has to become a state the page can render.
            job.error = str(e) or e.__class__.__name__
            job.error_kind = ("unavailable"
                              if e.__class__.__name__ == "LLMUnavailable"
                              else "failed")
        else:
            job.reply = reply
            write(stage, row_id, key, {
                "key": key, "stage": stage, "row_id": row_id,
                "cells": list(cells), "model": model,
                "asked_at": job.started_at,
                "answered_at": time.time(),
                "seconds": round(time.time() - job.started_at, 1),
                "reply": reply,
            })
        finally:
            job.finished_at = time.time()

    threading.Thread(target=_run, name=f"agent-{stage}-{row_id}",
                     daemon=True).start()
    return job


def drop(key: str, running_too: bool = False) -> None:
    """Forget a finished job, so the next request starts a fresh one.

    `running_too=False` by default: a job still in flight is deliberately left
    alone. "Ask again" during a check would otherwise abandon the answer that is
    seconds from arriving and pay for an identical one -- and the thread cannot
    be stopped anyway, so the only thing dropping it achieves is a second bill.
    """
    with _LOCK:
        job = _JOBS.get(key)
        if job is not None and (running_too or not job.running):
            _JOBS.pop(key, None)


def jobs(stage: str) -> list[Job]:
    """Every job for a stage in this process, newest first."""
    with _LOCK:
        js = [j for j in _JOBS.values() if j.stage == stage]
    return sorted(js, key=lambda j: j.started_at, reverse=True)


def latest_answers(stage: str) -> list[dict]:
    """The newest saved answer for each project in a stage, newest first.

    The project is read from the file name, so only one file per project is
    opened, however many answers it has. An answer too old to be served is left
    out, for the same reason read() would not serve it.
    """
    out, seen = [], set()
    try:
        paths = sorted(CACHE_DIR.glob(f"{stage}-*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return out
    now = time.time()
    for p in paths:
        try:
            row_id = int(p.name.split("-")[1])
            if row_id in seen or now - p.stat().st_mtime > MAX_AGE:
                continue
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError, IndexError):
            continue
        if isinstance(data, dict) and data.get("reply") and data.get("cells"):
            seen.add(row_id)
            out.append(data)
    return out


def jobs_for(stage: str, row_id: int) -> list[Job]:
    """Jobs about one row, newest first."""
    with _LOCK:
        js = [j for j in _JOBS.values()
              if j.stage == stage and j.row_id == row_id]
    return sorted(js, key=lambda j: j.started_at, reverse=True)


def last_cells(stage: str, row_id: int) -> list[str]:
    """What was last asked about this row -- the reason a reload finds its way
    back to a check in flight.

    The pane is an iframe whose `src` carries the ticked cells. On a reload the
    parent page renders that `src` from scratch, and without this it would render
    the empty one and show the picker's placeholder over the top of a running
    job. A live job wins over a cached answer: if both exist, the live one is
    what the reviewer just asked for.
    """
    running = [j for j in jobs_for(stage, row_id) if j.running]
    if running:
        return list(running[0].cells)
    done = jobs_for(stage, row_id)
    if done:
        return list(done[0].cells)
    entries = entries_for(stage, row_id)
    if entries:
        return [str(c) for c in entries[0].get("cells", [])]
    return []
