"""Regression tests for the review screen -- the reading pane and the edit gate.

Same rule as test_pipeline.py: every test here is a defect that would corrupt
data or quietly mislead a reviewer, not a test of layout. Two of them are bugs
found while building the pane, and both had the same shape -- the pane showing a
clean article with no highlight in it, which a reviewer reads as "the source does
not say this" and acts on. A pane that is wrong in that direction is worse than
no pane, so those two are pinned here.

The web interface is the one part of this project with dependencies, so the whole
module skips when FastAPI is absent rather than failing:

    python3 -m unittest discover -s tests
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from webapp import evidence as ev
    from webapp.agent import render_reply
    from webapp.shared import flag_only_reason
    HAVE_WEBAPP = True
except ImportError:  # pragma: no cover - FastAPI not installed
    HAVE_WEBAPP = False


# `ignore_cleanup_errors` arrived in Python 3.10, and this project supports 3.9 --
# scoreboard.py says so and refuses to run below it. It is only a backstop here
# (tearDown drains the jobs, which is the actual fix), so on 3.9 we go without.
# Without this the whole TestAgentCache class errored in setUp on the interpreter
# the project actually declares.
_TMPDIR_KW = {"ignore_cleanup_errors": True} if sys.version_info >= (3, 10) else {}


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestFlagOnlyReason(unittest.TestCase):
    """A flag-only edit is its own reason; anything else still needs one.

    Every write to a published row goes to verify_edits with a reason, and that
    is what makes the Scoreboard auditable. But the reason box was being filled
    with "resolved the flag" on every pass through the queue -- a second copy of
    what the flag cell already said. The exemption is exactly one cell wide, and
    these tests are what keeps it there.
    """

    def test_flag_alone_writes_its_own_reason(self):
        r = flag_only_reason({"flag": "Resolved: both sources agree."}, "date unclear")
        self.assertIsNotNone(r)
        self.assertIn("date unclear", r)
        self.assertIn("Resolved: both sources agree.", r)

    def test_setting_and_clearing_read_differently(self):
        self.assertIn("Flag set", flag_only_reason({"flag": "sources disagree"}, ""))
        self.assertIn("Flag cleared", flag_only_reason({"flag": ""}, "sources disagree"))
        self.assertIn("sources disagree", flag_only_reason({"flag": ""}, "sources disagree"))

    def test_any_other_cell_still_needs_a_reason(self):
        """The whole point of the narrowness. A changed date is a changed fact
        about the world, and nothing in `flag` can be trusted to explain it."""
        self.assertIsNone(flag_only_reason({"announced": "2022-02"}, "x"))
        self.assertIsNone(
            flag_only_reason({"announced": "2022-02", "flag": "and this"}, "x"))
        self.assertIsNone(flag_only_reason({}, "x"))


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestTabs(unittest.TestCase):
    def test_a_second_link_in_one_cell_gets_its_own_tab(self):
        """A reviewer who found a corroborating page pastes it beside the first.
        Reading only the first URL in the cell would hide the one link somebody
        went to the trouble of adding."""
        row = {"promise_source": "https://a.test/x https://b.test/y",
               "status_source": "https://c.test/z",
               "promised_date_source": None, "actual_date_source": None}
        tabs = ev.tabs_for(row)
        self.assertEqual([t["url"] for t in tabs],
                         ["https://a.test/x", "https://b.test/y", "https://c.test/z"])

    def test_one_page_cited_twice_is_one_tab(self):
        """One article often carries both the promise and the promised date.
        That is one document, and reading it twice is not two checks."""
        row = {"promise_source": "https://a.test/x",
               "promised_date_source": "https://a.test/x",
               "status_source": "https://c.test/z", "actual_date_source": None}
        tabs = ev.tabs_for(row)
        self.assertEqual(len(tabs), 2)
        self.assertEqual(tabs[0]["fields"], ["promise_source", "promised_date_source"])
        # ...and it highlights the union of what both columns are cited for.
        self.assertIn("promised_first_output", tabs[0]["highlight"])
        self.assertIn("promised_capital_usd", tabs[0]["highlight"])

    def test_trailing_punctuation_is_not_part_of_the_url(self):
        row = {"promise_source": "see https://a.test/x, and more",
               "status_source": "", "promised_date_source": "",
               "actual_date_source": ""}
        self.assertEqual(ev.urls_in(row["promise_source"]), ["https://a.test/x"])


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestNeedles(unittest.TestCase):
    """What the pane looks for. A needle that misses is not a cosmetic failure:
    an unhighlighted page reads as "the source does not say this"."""

    def test_a_month_year_anchor_matches_a_dated_press_release(self):
        """`announced` is stored YYYY-MM and almost nothing prints it that way.
        Before the optional-day gap, this row highlighted the bare year eleven
        times on Intel's release and never the announcement sentence itself."""
        row = {"announced": "2022-01", "announced_raw": ""}
        doc, _, counts = ev.render_document(
            "<p>SANTA CLARA, Calif., Jan. 21, 2022 -- Intel today announced</p>",
            "https://x.test/", ev.needles_for(row, ["announced"]))
        self.assertIn("Jan. 21, 2022", doc)
        self.assertEqual(counts.get("announced"), 1)

    def test_capital_matches_how_a_page_actually_prints_it(self):
        row = {"promised_capital_usd": 1_600_000_000}
        needles = {n.lower() for n, _f, _k in
                   ev.needles_for(row, ["promised_capital_usd"])}
        for spelling in ("$1.6 billion", "$1.6b", "1,600,000,000"):
            self.assertIn(spelling, needles)

    def test_the_verbatim_quote_leads(self):
        """The *_raw cell is the extractor's claim about what the page literally
        says, so it is tried first -- and its ABSENCE from the page is the
        finding the reviewer is there to make."""
        row = {"promised_first_output": "2025",
               "promised_first_output_raw": "output is slated for 2025"}
        first = ev.needles_for(row, ["promised_first_output"])[0]
        self.assertEqual(first[0], "output is slated for 2025")

    def test_a_quote_matches_across_a_line_break(self):
        """A quote copied off a rendered page and the same sentence in the HTML
        source never agree about whitespace."""
        row = {"announced": "2022-01", "announced_raw": "announced in January 2022"}
        doc, _, counts = ev.render_document(
            "<p>Acme announced in\n   January 2022 that it would build</p>",
            "https://x.test/", ev.needles_for(row, ["announced"]))
        self.assertEqual(counts.get("announced"), 1)

    def test_a_sentinel_date_highlights_the_language_that_would_answer_it(self):
        """`pending` is not a wrong date, it is an absent one. There is nothing
        to match, so the pane marks what WOULD carry the answer."""
        row = {"actual_first_output": "pending", "actual_first_output_raw": ""}
        kinds = {k for _n, _f, k in ev.needles_for(row, ["actual_first_output"])}
        self.assertEqual(kinds, {"context"})
        _doc, _t, counts = ev.render_document(
            "<p>The plant began production last spring.</p>", "https://x.test/",
            ev.needles_for(row, ["actual_first_output"]))
        self.assertEqual(counts.get("actual_first_output"), 1)


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestSanitizer(unittest.TestCase):
    def test_nothing_executable_survives(self):
        doc, _t, _c = ev.render_document(
            '<script>alert(1)</script><style>p{}</style>'
            '<p onclick="evil()">text</p><iframe src="x"></iframe>',
            "https://x.test/", [])
        for banned in ("script", "alert", "onclick", "iframe", "<style"):
            self.assertNotIn(banned, doc)
        self.assertIn("text", doc)

    def test_a_void_tag_does_not_swallow_the_rest_of_the_page(self):
        """The regression. `img` was dropped WITH its subtree, but it has no
        closing tag, so one image armed a skip nothing ever disarmed and the
        pane rendered the headline and nothing after it -- which reads exactly
        like an article that does not mention the date."""
        doc, _t, _c = ev.render_document(
            "<h1>Headline</h1><img src=x><p>The body of the article.</p>",
            "https://x.test/", [])
        self.assertIn("The body of the article.", doc)

    def test_links_are_absolute_and_open_out(self):
        doc, _t, _c = ev.render_document(
            '<a href="/story">more</a>', "https://x.test/news/a", [])
        self.assertIn('href="https://x.test/story"', doc)
        self.assertIn('target="_blank"', doc)

    def test_unbalanced_source_cannot_leave_the_pane_open(self):
        doc, _t, _c = ev.render_document("<p>Unclosed <b>bold", "https://x.test/", [])
        self.assertTrue(doc.endswith("</b></p>"), doc)

    def test_the_title_is_read_out_of_the_head(self):
        _d, title, _c = ev.render_document(
            "<html><head><title>Big Fab</title></head><body><p>x</p></body></html>",
            "https://x.test/", [])
        self.assertEqual(title, "Big Fab")

    def test_a_compressed_body_is_never_shown_as_text(self):
        """The other pane-lies-by-omission bug. The Wayback `id_` form replays
        the original Content-Encoding, and a gzip stream decoded as text is 60KB
        of replacement characters: a clean-looking article with no highlight in
        it. `_looks_like_text` is the gate that stops it being rendered."""
        import gzip
        raw = b"<html><body><p>hello there, this is text</p></body></html>"
        self.assertTrue(ev._looks_like_text(raw))
        self.assertFalse(ev._looks_like_text(gzip.compress(raw)))
        self.assertEqual(ev._decompress(gzip.compress(raw), "gzip"), raw)


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestAgentReply(unittest.TestCase):
    def test_the_reply_is_escaped_before_it_is_formatted(self):
        html_out = render_reply("**CONFIRMED** <script>alert(1)</script> found it")
        self.assertNotIn("<script>", html_out)
        self.assertIn("CONFIRMED", html_out)

    def test_urls_in_the_reply_are_clickable(self):
        """The whole output of the check, when a value is not on the cited page,
        is a URL where it is. One a reviewer cannot click is not an answer."""
        html_out = render_reply("Found it at https://other.test/story instead.")
        self.assertIn('href="https://other.test/story"', html_out)


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestAgentCache(unittest.TestCase):
    """The check runs on a thread and its answer is kept on disk.

    Both are here because of the same incident. A check was running; the promote
    form bounced back with "you changed a cell, give a reason"; the page
    reloaded; the check was gone, with no sign it had ever been asked for. The
    reviewer asked again and paid for the same answer twice. So: a reload must
    re-attach to the job, and an answer already given must come back free.

    The dangerous half is the cache, not the threading. A cached answer served
    after the cell it describes has been edited is a confirmation of a value
    that is no longer there -- worse than no check at all, because it reads as
    corroboration. `test_editing_a_cell_invalidates_the_answer` is the one that
    has to keep passing.
    """

    def setUp(self):
        from webapp import agent_cache as ac
        self.ac = ac
        self.dir = tempfile.TemporaryDirectory(**_TMPDIR_KW)
        self._old = ac.CACHE_DIR
        ac.CACHE_DIR = Path(self.dir.name)
        ac._JOBS.clear()

    def tearDown(self):
        # Drain before deleting the directory. A job whose thread is still alive
        # writes its answer into the cache dir, and a test that tore the dir out
        # from under it failed roughly one run in eight -- on the cleanup, not on
        # anything it was testing. Waiting is the fix; ignore_cleanup_errors is
        # the backstop for a job that hangs past the deadline, where available.
        for job in list(self.ac._JOBS.values()):
            self.settle(job, limit=5.0, hard=False)
        self.ac.CACHE_DIR = self._old
        self.ac._JOBS.clear()
        self.dir.cleanup()

    def row(self, **over):
        r = {"project": "Test Fab", "announced": "2022-01",
             "announced_raw": "announced in January 2022",
             "promised_first_output": "2024", "promised_first_output_raw": "in 2024",
             "actual_first_output": "pending", "actual_first_output_raw": "",
             "promised_capital_usd": "5000000000", "promised_jobs": "3000",
             "current_status": "UNDER CONSTRUCTION",
             "promise_source": "https://a.test/promise",
             "status_source": "https://a.test/status",
             "promised_date_source": "", "actual_date_source": ""}
        r.update(over)
        return r

    def key(self, row, cells=("announced",), model="m"):
        return self.ac.fingerprint("screen", 7, list(cells), row, model, db="d.db")

    # --- what makes two questions the same question ------------------------
    def test_the_same_question_is_the_same_key(self):
        self.assertEqual(self.key(self.row()), self.key(self.row()))

    def test_editing_a_cell_invalidates_the_answer(self):
        """The reason the key is a fingerprint and not a row id. Correcting
        `announced` after a check must not leave the old CONFIRMED in place,
        describing a value the row no longer holds."""
        self.assertNotEqual(self.key(self.row()),
                            self.key(self.row(announced="2019-03")))

    def test_editing_the_verbatim_quote_invalidates_it_too(self):
        """The quote is the evidence the model was shown. Change it and the
        question changed, even though the date did not."""
        self.assertNotEqual(self.key(self.row()),
                            self.key(self.row(announced_raw="said March 2019")))

    def test_a_different_source_url_invalidates_it(self):
        """Re-pointing a citation changes which page is being read."""
        self.assertNotEqual(self.key(self.row()),
                            self.key(self.row(promise_source="https://b.test/x")))

    def test_an_unrelated_cell_does_not(self):
        """Narrow on purpose: nobody re-buys an answer about `announced`
        because the sector was corrected."""
        self.assertEqual(self.key(self.row()),
                         self.key(self.row(current_status="PRODUCING")))

    def test_ticking_a_different_box_is_a_different_question(self):
        r = self.row()
        self.assertNotEqual(self.key(r, ("announced",)),
                            self.key(r, ("announced", "promised_jobs")))
        # ...but the order the boxes arrive in is not part of the question.
        self.assertEqual(self.key(r, ("announced", "promised_jobs")),
                         self.key(r, ("promised_jobs", "announced")))

    # --- the answers on disk -----------------------------------------------
    def test_an_answer_survives_and_comes_back(self):
        k = self.key(self.row())
        self.ac.write("screen", 7, k, {"cells": ["announced"], "reply": "CONFIRMED",
                                       "answered_at": time.time(), "model": "m"})
        self.assertEqual(self.ac.read("screen", 7, k)["reply"], "CONFIRMED")

    def test_a_stale_answer_is_a_miss(self):
        """A status page is cited precisely because it changes, so an answer
        about one is a reading of a document on a day, not a fact."""
        k = self.key(self.row())
        self.ac.write("screen", 7, k, {"cells": ["announced"], "reply": "old",
                                       "answered_at": 0, "model": "m"})
        path = self.ac._path("screen", 7, k)
        old = time.time() - self.ac.MAX_AGE - 60
        os.utime(path, (old, old))
        self.assertIsNone(self.ac.read("screen", 7, k))

    def test_a_corrupt_file_is_a_miss_not_a_crash(self):
        """A cache that can break the review screen is worse than no cache."""
        k = self.key(self.row())
        self.ac.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.ac._path("screen", 7, k).write_text("{ truncated", encoding="utf-8")
        self.assertIsNone(self.ac.read("screen", 7, k))

    def test_forget_makes_the_next_ask_really_ask(self):
        k = self.key(self.row())
        self.ac.write("screen", 7, k, {"cells": ["announced"], "reply": "x",
                                       "answered_at": time.time(), "model": "m"})
        self.ac.forget("screen", 7, k)
        self.assertIsNone(self.ac.read("screen", 7, k))

    # --- the job in the background -----------------------------------------
    def test_a_reload_attaches_instead_of_paying_again(self):
        """The whole point. Three requests arriving while a check runs must be
        three views of one job, not three API calls."""
        calls, release = [], threading.Event()

        def work():
            calls.append(1)
            release.wait(5)
            return "done"

        jobs = [self.ac.start("screen", 7, "k1", ["announced"], "m", work)
                for _ in range(3)]
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(j is jobs[0] for j in jobs))
        self.assertTrue(jobs[0].running)
        release.set()
        self.settle(jobs[0])
        self.assertEqual(jobs[0].reply, "done")

    def settle(self, job, limit=5.0, hard=True):
        """Wait for one job's thread. `hard=False` in tearDown, where a job that
        overran is a cleanup problem, not a test failure to report."""
        deadline = time.time() + limit
        while job.running and time.time() < deadline:
            time.sleep(0.02)
        if hard:
            self.assertFalse(job.running, "the job never finished")

    def test_a_finished_job_writes_its_answer_to_disk(self):
        job = self.ac.start("screen", 7, "k2", ["announced"], "m", lambda: "kept")
        self.settle(job)
        self.assertEqual(self.ac.read("screen", 7, "k2")["reply"], "kept")

    def test_a_failure_becomes_a_state_not_a_lost_pane(self):
        """An exception at the top of a thread goes to a console nobody reads,
        and the pane would sit there waiting for an answer that never comes."""
        def boom():
            raise RuntimeError("network went away")

        job = self.ac.start("screen", 7, "k3", ["announced"], "m", boom)
        self.settle(job)
        self.assertEqual(job.error_kind, "failed")
        self.assertIn("network went away", job.error)
        self.assertIsNone(self.ac.read("screen", 7, "k3"))

    def test_the_page_remembers_what_was_last_asked(self):
        """This is what carries a running check across a reload: the pane is an
        iframe whose src is rebuilt from scratch every render, so the last
        question has to be recoverable from the server, not from the page."""
        self.assertEqual(self.ac.last_cells("screen", 7), [])
        self.ac.start("screen", 7, "k4", ["announced", "promised_jobs"], "m",
                      lambda: "x")
        self.assertEqual(self.ac.last_cells("screen", 7),
                         ["announced", "promised_jobs"])

    def test_it_remembers_across_a_restart_too(self):
        """Jobs die with the process; the answers on disk do not."""
        k = self.key(self.row())
        self.ac.write("screen", 7, k, {"cells": ["promised_first_output"],
                                       "reply": "x", "answered_at": time.time(),
                                       "model": "m"})
        self.ac._JOBS.clear()          # as if the server had been restarted
        self.assertEqual(self.ac.last_cells("screen", 7), ["promised_first_output"])

    def test_ask_again_leaves_a_running_check_alone(self):
        """Dropping a job in flight cannot stop the thread -- all it achieves is
        a second bill for the answer that is seconds away."""
        release = threading.Event()

        def work():
            release.wait(5)
            return "done"

        job = self.ac.start("screen", 7, "k5", ["announced"], "m", work)
        self.ac.drop("k5")
        self.assertIs(self.ac.get("k5"), job)
        release.set()
        self.settle(job)


if __name__ == "__main__":
    unittest.main()
