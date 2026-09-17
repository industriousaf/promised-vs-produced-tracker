"""
page_cache.py -- saved copies of the pages projects cite, and the preload that fills them.

The pane used to keep what it fetched in memory: 24 pages for 30 minutes, gone
on every restart. A review session outgrew that within the hour, so nearly every
source a verifier opened was a live download at that moment, and a site that
refused could take a minute and a half to come back from the Wayback Machine.
Lucas saw the first source load at all about 70 percent of the time.

So each fetched page is saved to disk, and a preload can fetch every page the
waiting projects cite before anyone opens them.

## What is saved, and for how long

The page as it was downloaded, not as the pane rendered it. Highlighting runs
when the page is opened, against the project's values at that moment, so a
corrected cell is marked in the saved copy without downloading it again.

A page that loaded is used for a week (MAX_AGE) and then downloaded again: a
status page is cited because it changes. A failure is used for half an hour
(RETRY_AFTER), so reopening a tab does not start another minute-long wait on a
site that just refused. The preload retries every failure whatever its age,
which is what makes "turn the VPN on and run the preload again" work.

## Where

scratch/pages/, gitignored like the rest of scratch/. Nothing here is data:
delete the directory and each page downloads again the next time it opens. It
holds copies of other people's articles, which is one more reason it must never
be committed to a public repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

_ROOT = Path(__file__).resolve().parent.parent

PAGES_DIR = _ROOT / "scratch" / "pages"

MAX_AGE = 7 * 24 * 60 * 60
RETRY_AFTER = 30 * 60

# Raised whenever what fetch() decides about a page changes, so a page saved
# under the old rules downloads again instead of being trusted for a week.
# 2: a region notice is a failure, and a nearly empty page tries the archive.
FORMAT = 2

# Pages the preload downloads at once. Enough that one slow site does not hold
# up the rest, and few enough not to look like a crawler to the sites.
WORKERS = 4


# --------------------------------------------------------------------------- #
# The saved pages                                                              #
# --------------------------------------------------------------------------- #

def _path(key: str) -> Path:
    return PAGES_DIR / (hashlib.sha256(key.encode("utf-8")).hexdigest()[:32] + ".json")


def read(key: str, failures: bool = True) -> dict | None:
    """The saved result for `key`, or None if there is none fresh enough to use.

    A page that loaded counts for MAX_AGE and a failure for RETRY_AFTER. With
    failures=False a failure never counts, so the caller downloads it again.
    """
    try:
        data = json.loads(_path(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # The key is stored in the file as well as hashed into its name, so a
    # collision or a hand-copied file can never serve one page as another.
    if not isinstance(data, dict) or data.get("key") != key:
        return None
    if data.get("format") != FORMAT:
        return None
    age = time.time() - float(data.get("fetched_at") or 0)
    if data.get("ok"):
        return data if age < MAX_AGE else None
    return data if failures and age < RETRY_AFTER else None


def write(key: str, result: dict) -> None:
    """Save one fetch result. Never raises: a copy that cannot be saved only
    means the page downloads again the next time it opens."""
    try:
        PAGES_DIR.mkdir(parents=True, exist_ok=True)
        path = _path(key)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(result, key=key, format=FORMAT),
                                  ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def prune() -> None:
    """Delete saved pages too old to be used again."""
    cutoff = time.time() - MAX_AGE
    try:
        paths = list(PAGES_DIR.glob("*.json"))
    except OSError:
        return
    for p in paths:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def lock(key: str) -> threading.Lock:
    """One lock per page. A page the preload is downloading when a verifier
    opens it is waited for, not downloaded a second time."""
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


# --------------------------------------------------------------------------- #
# The preload                                                                  #
# --------------------------------------------------------------------------- #

@dataclass
class Run:
    """One pass over every page the waiting projects cite.

    `pages` is in the order the pages were asked for, and each entry gains the
    outcome of its fetch as it lands: ok, via, status, error, fetched_at, words,
    and why a page was not the page, if it was not (see evidence._judge).
    Held in this process only; the pages themselves are what is kept.
    """
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    pages: list[dict] = field(default_factory=list)
    done: int = 0
    error: str | None = None

    @property
    def running(self) -> bool:
        return self.finished_at is None

    @property
    def total(self) -> int:
        return len(self.pages)

    @property
    def saved(self) -> int:
        return sum(1 for p in self.pages if p.get("ok") is True)

    @property
    def failed(self) -> int:
        return sum(1 for p in self.pages if p.get("ok") is False)


_RUN: Run | None = None
_RUN_GUARD = threading.Lock()


def last_run() -> Run | None:
    """The preload running now, or the last one to finish, or None."""
    return _RUN


def start(targets: Callable[[], list[dict]], fetch: Callable[[str], dict]) -> Run:
    """Start a preload unless one is running. Returns the run either way.

    `targets` lists the pages, [{"url": ...}, ...], in the order they should
    arrive. It is called on the run's own thread, because working it out reads
    every waiting project and the page that asked should not wait for that.

    `fetch` downloads and saves one page. It is the pane's own fetch, so a page
    the preload saved and a page a click saved are the same thing.
    """
    global _RUN
    with _RUN_GUARD:
        if _RUN is not None and _RUN.running:
            return _RUN
        run = _RUN = Run()

    def one(page: dict) -> None:
        res = fetch(page["url"])
        with _RUN_GUARD:
            page.update(ok=bool(res.get("ok")), via=res.get("via"),
                        status=res.get("status"), error=res.get("error") or "",
                        fetched_at=res.get("fetched_at"), words=res.get("words"),
                        unreadable=res.get("unreadable"),
                        origin_unreadable=res.get("origin_unreadable"))
            run.done += 1

    def work() -> None:
        try:
            prune()
            run.pages = list(targets())
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                list(pool.map(one, run.pages))
        except Exception as exc:
            # The top of a thread: nothing above this would ever see it.
            run.error = f"{type(exc).__name__}: {exc}"
        finally:
            run.finished_at = time.time()

    threading.Thread(target=work, name="page-preload", daemon=True).start()
    return run
