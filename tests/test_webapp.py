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
import re
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
# tracker.py says so and refuses to run below it. It is only a backstop here
# (tearDown drains the jobs, which is the actual fix), so on 3.9 we go without.
# Without this the whole TestAgentCache class errored in setUp on the interpreter
# the project actually declares.
_TMPDIR_KW = {"ignore_cleanup_errors": True} if sys.version_info >= (3, 10) else {}


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestFlagOnlyReason(unittest.TestCase):
    """A flag-only edit is its own reason; anything else still needs one.

    Every write to a published row goes to verify_edits with a reason, and that
    is what makes the Tracker auditable. But the reason box was being filled
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


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestRowCheckButton(unittest.TestCase):
    """The per-row "Run check" button appears only on a row with no verdict.

    It used to sit on every row in the Screen list, next to a verdict that was
    already printed, immediately before "Inspect & promote". That read as step
    one of two. It was not: extraction runs the check seconds later, so every
    collected row already has one, and pressing the button only appended
    another screen_check row. A row added by hand has no check -- `screen-add`
    deliberately does not run one -- and that is the case the button is for.
    """

    def setUp(self):
        from pipeline import db as pdb
        from pipeline import screen as pscreen, source as psource
        self.dir = tempfile.TemporaryDirectory(**_TMPDIR_KW)
        self.path = Path(self.dir.name) / "t.db"
        conn = pdb.connect(self.path)
        pdb.init_db(conn)
        row = {"project": "Checked Fab", "sector": "Semiconductors", "state": "TX",
               "announced": "2022-01", "promised_capital_usd": 5_000_000_000,
               "promised_jobs": 1500, "promised_first_output": "2024",
               "actual_first_output": "pending", "current_status": "UNDER CONSTRUCTION",
               "promise_source": "https://example.com/p", "status_source": "https://example.com/s",
               "verification_tier": "P"}
        a = psource.insert_lead(conn, promise_source="https://example.com/a",
                                status_source="https://example.com/s", summary="x")
        b = psource.insert_lead(conn, promise_source="https://example.com/b",
                                status_source="https://example.com/s", summary="y")
        self.checked = pscreen.insert_extracted(
            conn, dict(row, flag="source states no production-start date"),
            source_collected_id=a)
        self.unchecked = pscreen.insert_extracted(
            conn, dict(row, project="Unchecked Fab"), source_collected_id=b)
        pscreen.run_check(conn, self.checked)      # only this one gets a verdict
        conn.commit(); conn.close()
        pdb.set_active_db(self.path)               # never the real database

    def tearDown(self):
        from pipeline import db as pdb
        pdb.set_active_db(None)
        self.dir.cleanup()

    def _cards(self) -> dict:
        from fastapi.testclient import TestClient
        from webapp.main import app
        body = TestClient(app).get("/screen").text
        out = {}
        for chunk in body.split('<div class="card">'):
            for sid in (self.checked, self.unchecked):
                if f"<b>#{sid}</b>" in chunk:
                    out[sid] = chunk
        return out

    def test_a_row_with_a_verdict_has_no_check_button(self):
        card = self._cards()[self.checked]
        self.assertIn("PASS", card)          # an open flag, so not CLEAN
        self.assertIn("verdict-", card)      # the verdict is still shown
        self.assertNotIn("Run check", card)

    def test_a_row_with_no_verdict_still_offers_the_button(self):
        card = self._cards()[self.unchecked]
        self.assertIn("Run check", card)
        self.assertIn("not checked", card)   # was a bare em-dash, which said nothing

    def test_the_bulk_recheck_survives(self):
        """Removing the per-row button must not remove the only way to regrade
        after a rule change."""
        from fastapi.testclient import TestClient
        from webapp.main import app
        self.assertIn("/screen/check-all", TestClient(app).get("/screen").text)

    def test_the_bulk_recheck_is_below_the_rows_and_says_what_it_is_for(self):
        """It used to sit between the heading and the filter, labelled "run the
        deterministic check on all rows", which named the implementation and
        not the job and read as the next thing to do. It is maintenance: the
        only thing that can make a stored verdict wrong is a rule change."""
        from fastapi.testclient import TestClient
        from webapp.main import app
        b = TestClient(app).get("/screen").text
        self.assertLess(b.index('class="toggle"'), b.index("/screen/check-all"))
        self.assertNotIn("deterministic check on all rows", b)
        self.assertIn("pipeline/settings.py", b)      # names the one reason


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestDashboardIsAPipeline(unittest.TestCase):
    """The dashboard states three stages, not five tables, and every number is
    given something to be measured against.

    It used to print five equal tiles, one per table. Two of them were not
    stages: screen_check is one row per Screen row by construction, so it
    printed the same number twice, and verify_edits is an audit log. Five equal
    boxes also asserted five equal steps, when the methodology is three layers
    with the last one a human-only gate.
    """

    def setUp(self):
        from pipeline import db as pdb, screen as pscreen, source as psource
        self.dir = tempfile.TemporaryDirectory(**_TMPDIR_KW)
        self.path = Path(self.dir.name) / "t.db"
        conn = pdb.connect(self.path)
        pdb.init_db(conn)
        row = {"project": "Test Fab", "sector": "Semiconductors", "state": "TX",
               "announced": "2022-01", "promised_capital_usd": 5_000_000_000,
               "promised_jobs": 1500, "promised_first_output": "2024",
               "actual_first_output": "pending", "current_status": "UNDER CONSTRUCTION",
               "promise_source": "https://example.com/p",
               "status_source": "https://example.com/s", "verification_tier": "P"}
        lead = psource.insert_lead(conn, promise_source="https://example.com/a",
                                   status_source="https://example.com/s", summary="x")
        rid = pscreen.insert_extracted(conn, dict(row), source_collected_id=lead)
        pscreen.run_check(conn, rid)
        conn.commit(); conn.close()
        pdb.set_active_db(self.path)

    def tearDown(self):
        from pipeline import db as pdb
        pdb.set_active_db(None)
        self.dir.cleanup()

    def _body(self) -> str:
        from fastapi.testclient import TestClient
        from webapp.main import app
        return TestClient(app).get("/").text

    def test_three_stages_named_not_five_tables(self):
        b = self._body()
        for stage in ("Source", "Screen", "Verify"):
            self.assertIn(f'<div class="stage-name">{stage}</div>', b)
        self.assertEqual(b.count('class="stage-name"'), 3)

    def test_the_audit_log_is_not_a_stage(self):
        b = self._body()
        self.assertNotIn('<div class="stage-name">Verify edits', b)
        self.assertIn("edit(s) logged", b)      # still reported, as a detail

    def test_published_count_carries_a_denominator(self):
        """0 alone is unreadable: 0 of how many, and is that expected?"""
        self.assertRegex(self._body(), r"<span class=\"stage-n\">\s*0\s*</span>\s*of \d+ published")

    def test_the_gate_between_stages_is_shown(self):
        self.assertIn('class="gate"', self._body())


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestChromePlacement(unittest.TestCase):
    """Rare controls do not get the best space on every page."""

    def setUp(self):
        from pipeline import db as pdb
        self.dir = tempfile.TemporaryDirectory(**_TMPDIR_KW)
        self.path = Path(self.dir.name) / "t.db"
        conn = pdb.connect(self.path); pdb.init_db(conn); conn.close()
        pdb.set_active_db(self.path)

    def tearDown(self):
        from pipeline import db as pdb
        pdb.set_active_db(None)
        self.dir.cleanup()

    def _get(self, path="/"):
        from fastapi.testclient import TestClient
        from webapp.main import app
        return TestClient(app).get(path).text

    def test_the_switcher_is_in_the_footer_not_above_the_content(self):
        b = self._get()
        self.assertIn('class="dbswitch"', b)
        self.assertLess(b.index('class="wrap"'), b.index('class="dbswitch"'))
        self.assertIn("pagefoot", b)

    def test_the_band_names_the_database(self):
        self.assertIn('class="dbstatus', self._get())

    def test_a_scratch_database_says_so(self):
        """Quiet on the canonical file; loud anywhere else, because promoting
        into a scratch copy believing it is the real one cannot be undone."""
        self.assertIn("NOT THE CANONICAL DB", self._get())


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestOneVocabularyPerTransition(unittest.TestCase):
    """A stage transition is described in one set of words, and a verdict never
    appears on its own.

    The Screen list said the same fact three ways at once: the filter read "Not
    yet in Verify", the pill on every row read "not promoted yet", and the
    button read "promote". Promotion is the CLI's verb; a person reading a list
    needs the stage, and the stages are named.

    Separately, PASS and CLEAN are both positive words whose order is not
    visible in the words. CLEAN is the best of the three, which is the reverse
    of how they sort alphabetically and of how most readers rank them.
    """

    def setUp(self):
        from pipeline import db as pdb, screen as pscreen, source as psource
        self.dir = tempfile.TemporaryDirectory(**_TMPDIR_KW)
        self.path = Path(self.dir.name) / "t.db"
        conn = pdb.connect(self.path)
        pdb.init_db(conn)
        base = {"project": "Test Fab", "sector": "Semiconductors", "state": "TX",
                "announced": "2022-01", "promised_capital_usd": 5_000_000_000,
                "promised_jobs": 1500, "promised_first_output": "2024",
                "actual_first_output": "pending", "current_status": "UNDER CONSTRUCTION",
                "promise_source": "https://example.com/p",
                "status_source": "https://example.com/s", "verification_tier": "P"}
        for n, extra in ((1, {"flag": "announcement gives no start date"}), (2, {})):
            lead = psource.insert_lead(conn, promise_source=f"https://example.com/{n}",
                                       status_source="https://example.com/s", summary="x")
            rid = pscreen.insert_extracted(
                conn, dict(base, project=f"Fab {n}", **extra), source_collected_id=lead)
            pscreen.run_check(conn, rid)
        conn.commit(); conn.close()
        pdb.set_active_db(self.path)

    def tearDown(self):
        from pipeline import db as pdb
        pdb.set_active_db(None)
        self.dir.cleanup()

    def _screen(self) -> str:
        from fastapi.testclient import TestClient
        from webapp.main import app
        return TestClient(app).get("/screen?show=all").text

    def test_promote_is_not_shown_to_a_reader(self):
        b = self._screen()
        self.assertNotIn("not promoted yet", b)
        self.assertNotIn("Inspect &amp; promote", b)

    def test_the_pill_and_the_filter_agree(self):
        b = self._screen()
        self.assertIn("not verified yet", b)
        self.assertIn("Not yet verified", b)

    def test_a_pass_says_how_many_flags_are_open(self):
        self.assertRegex(self._screen(), r">PASS</span><span class=\"verdict-gloss\">1 open flag")

    def test_a_clean_says_nothing_is_open(self):
        self.assertRegex(self._screen(), r">CLEAN</span><span class=\"verdict-gloss\">nothing open")

    def test_no_verdict_token_ever_renders_alone(self):
        """Two contexts gloss a verdict: a row (.verdict-gloss) and the legend
        (.vkey-g). Both count; a token with neither beside it does not."""
        import re
        b = self._screen()
        seen = 0
        for m in re.finditer(r'<span class="verdict-(FAIL|PASS|CLEAN)">[A-Z]+</span>', b):
            tail = b[m.end():m.end() + 32]
            seen += 1
            self.assertTrue(
                tail.startswith('<span class="verdict-gloss">') or tail.startswith('<span class="vkey-g">'),
                f"bare verdict token at offset {m.start()}: {tail!r}")
        self.assertGreater(seen, 0)

    def test_the_legend_defines_all_three_with_counts(self):
        """Defined where they are used, not in a glossary somewhere else."""
        b = self._screen()
        self.assertIn('class="vlegend"', b)
        for v in ("CLEAN", "PASS", "FAIL"):
            self.assertIn(f'>{v}</span>', b)
        self.assertIn("nothing open", b)
        self.assertIn("blocked", b)

    def test_each_verdict_filters(self):
        from fastapi.testclient import TestClient
        from webapp.main import app
        c = TestClient(app)
        self.assertIn("verdict=PASS", c.get("/screen?show=all").text)
        only_pass = c.get("/screen?show=all&verdict=PASS").text
        self.assertIn("Fab 1", only_pass)        # the flagged row
        self.assertNotIn("Fab 2", only_pass)     # the clean one


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestFetchErrorsAreReadable(unittest.TestCase):
    """A failed fetch says what happened, not what the interpreter said.

    The pane used to headline the raw exception, so a reviewer checking a
    factory's capital figure was shown "URLError: <urlopen error [Errno 8]
    nodename nor servname provided, or not known>". Whether the source is
    gone, refusing, or merely slow leads to three different next steps, and
    none of them is legible in that string.
    """

    def _x(self, error, status=0, host="example.org"):
        return ev.explain_fetch_error({"error": error, "status": status}, host)

    def test_dns_failure_says_the_domain_did_not_resolve(self):
        h, m = self._x("URLError: <urlopen error [Errno 8] nodename nor "
                       "servname provided, or not known>", host="georgia.org")
        self.assertIn("did not resolve", h)
        self.assertNotIn("Errno", h)
        self.assertNotIn("URLError", h)

    def test_refusal_is_distinguished_from_absence(self):
        self.assertIn("refused", self._x("HTTP 403 Forbidden", 403)[0])
        self.assertIn("gone", self._x("HTTP 404 Not Found", 404)[0])

    def test_a_server_error_says_it_may_work_later(self):
        h, m = self._x("HTTP 503 Service Unavailable", 503)
        self.assertIn("server error", h)
        self.assertIn("retrying", m)

    def test_a_timeout_names_the_budget(self):
        self.assertIn("25 seconds", self._x("TimeoutError: timed out")[1])

    def test_an_unrecognised_error_still_gets_a_sentence(self):
        h, m = self._x("ValueError: something odd", host="weird.example")
        self.assertIn("weird.example", h)
        self.assertIn("something odd", m)     # raw text kept, just not first

    def test_no_headline_leaks_an_exception_type(self):
        for e, st in (("URLError: <urlopen error [Errno 8] x>", 0),
                      ("HTTP 403 Forbidden", 403), ("HTTP 404 Not Found", 404),
                      ("TimeoutError: timed out", 0),
                      ("SSLCertVerificationError: certificate verify failed", 0)):
            h, _ = self._x(e, st)
            for leak in ("Error:", "Errno", "urlopen", "Traceback"):
                self.assertNotIn(leak, h, f"{leak!r} leaked into: {h!r}")

    def test_the_internal_ladder_vocabulary_is_gone(self):
        """"step 6 of the fetch ladder" named an implementation detail no
        reader can act on."""
        src = Path(ev.__file__).read_text()
        self.assertNotIn("fetch ladder", src)


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestProjectNotRow(unittest.TestCase):
    """The interface calls the thing a project, and says row only about storage.

    `project` is the name this repository already settled on: it is the
    identity column in the schema, the README defines scope with "A project is
    in scope when all of these hold", and the methodology says "for each
    individual project that is announced". The interface said row 221 times and
    project 14, which is the database's word for its own storage leaking onto
    every screen a person uses.

    Row is still correct for three things and this test allows exactly those:
    counting stored rows, naming a table's rows, and the audit trail. Where a
    sentence means the factory rather than the record, it says project.
    """

    # "cell" is the same fault as "row": spreadsheet vocabulary for a thing a
    # person filling a form calls a field. The internals keep it -- the
    # data-cell attribute, the `cell` form parameter, the .cells class, the JS
    # variables -- because those are addresses, not sentences.
    CELL_ALLOWED = (
        'data-cell=',        # the strip's own attribute
        'name="cell"',       # the agent picker's form parameter
        'class="cells"',     # its container
        'dataset.cell',      # JS
        'state[cell]',
        'looked[cell]',
        'var cell',
    )

    def test_no_reader_facing_string_says_cell_about_a_field(self):
        import ast
        root = Path(__file__).resolve().parent.parent / "webapp"
        bad = []
        for f in sorted(root.glob("*.py")):
            tree = ast.parse(f.read_text())
            docs = {ast.get_docstring(tree, clean=False) or ""}
            skip = set()
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                    d = ast.get_docstring(n, clean=False)
                    if d:
                        docs.add(d)
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Name) and t.id.endswith(("_CSS", "_JS")):
                            skip.update(id(x) for x in ast.walk(n.value))
            for n in ast.walk(tree):
                if id(n) in skip:
                    continue
                vals = []
                if isinstance(n, ast.Constant) and isinstance(n.value, str):
                    vals = [n.value]
                elif isinstance(n, ast.JoinedStr):
                    vals = [v.value for v in n.values
                            if isinstance(v, ast.Constant) and isinstance(v.value, str)]
                for v in vals:
                    if v in docs or ("<" not in v and "{" not in v):
                        continue
                    for m in re.finditer(r"\bcells?\b", v, re.I):
                        ctx = " ".join(
                            v[max(0, m.start() - 45):m.start() + 45].split())
                        if any(a in ctx for a in self.CELL_ALLOWED):
                            continue
                        bad.append(f"{f.name}:{n.lineno}: …{ctx}…")
        self.assertEqual(bad, [], "\n  ".join(
            ["these say 'cell' where a reader means a field:"] + bad))

    # Storage senses. Each is a phrase where "row" is the true word, not a
    # leak: adding to this list should require saying which of the three it is.
    ALLOWED = (
        "new Verify row",          # the record a promotion writes
        "rows to <code>screen_check",  # append-only history
        "sqlite3.Row",             # the type
        "screen_extracted row",    # an error naming the table
        "verify row",              # ditto, lowercase in an exception
        "{d['rows']} rows",        # the database switcher's own count
        "Row counts",              # the helper that produces it
    )

    # Not prose: an HTML attribute, and the blocks that are a stylesheet or a
    # script rather than anything a person reads as a sentence.
    NOT_PROSE = re.compile(r'rows\s*=\s*["\']?\d')
    NOT_PROSE_NAMES = ("_CSS", "_JS")

    def test_no_reader_facing_string_says_row_about_a_project(self):
        import ast
        root = Path(__file__).resolve().parent.parent / "webapp"
        bad = []
        for f in sorted(root.glob("*.py")):
            tree = ast.parse(f.read_text())

            # clean=False: get_docstring reindents by default, so the cleaned
            # text never equals the Constant the walk finds, and every
            # docstring came back as a finding.
            docs = {ast.get_docstring(tree, clean=False) or ""}
            skip_nodes = set()
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                    d = ast.get_docstring(n, clean=False)
                    if d:
                        docs.add(d)
                # A stylesheet or a script assigned to a module constant.
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Name) and t.id.endswith(self.NOT_PROSE_NAMES):
                            skip_nodes.update(id(x) for x in ast.walk(n.value))

            for n in ast.walk(tree):
                if id(n) in skip_nodes:
                    continue
                vals = []
                if isinstance(n, ast.Constant) and isinstance(n.value, str):
                    vals = [n.value]
                elif isinstance(n, ast.JoinedStr):
                    vals = [v.value for v in n.values
                            if isinstance(v, ast.Constant) and isinstance(v.value, str)]
                for v in vals:
                    if v in docs:
                        continue          # a note to the next maintainer, not UI
                    if "<" not in v and "{" not in v:
                        continue          # not markup a reader sees
                    for m in re.finditer(r"\brows?\b", v, re.I):
                        # Whitespace-normalised: these strings wrap across
                        # source lines, so an allowlisted phrase can arrive
                        # with a newline and indentation inside it.
                        ctx = " ".join(
                            v[max(0, m.start() - 40):m.start() + 40].split())
                        if any(a in ctx for a in self.ALLOWED):
                            continue
                        if self.NOT_PROSE.search(ctx):
                            continue
                        bad.append(f"{f.name}:{n.lineno}: …{ctx}…")
        self.assertEqual(bad, [], "\n  ".join(
            ["these say 'row' where a reader means a project:"] + bad))

    def test_the_word_the_schema_uses_is_project(self):
        """If the identity column is ever renamed, this vocabulary follows it
        rather than drifting."""
        from pipeline.schema_check import V0_COLUMNS
        self.assertIn("project", V0_COLUMNS)


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestVerifyButtonIsNeverGated(unittest.TestCase):
    """The verify button is never disabled by the checklist.

    The checklist is sessionStorage. Gating publication on it would mean that
    clearing a session, or opening the project in another browser, stops a
    person publishing. The command line has no such gate either, so disabling
    here would guard one of two doors.

    The deeper reason is what it would imply. A published project would read as
    "all six confirmed" when what happened is "six buttons were pressed in a
    browser that stored nothing". The artifact's whole claim is that a person
    read the sources, and that claim has to stay exactly as strong as the
    evidence for it.
    """

    def setUp(self):
        from pipeline import db as pdb, screen as pscreen, source as psource
        self.dir = tempfile.TemporaryDirectory(**_TMPDIR_KW)
        self.path = Path(self.dir.name) / "t.db"
        conn = pdb.connect(self.path)
        pdb.init_db(conn)
        row = {"project": "Test Fab", "sector": "Semiconductors", "state": "TX",
               "announced": "2022-01", "promised_capital_usd": 5_000_000_000,
               "promised_jobs": 1500, "promised_first_output": "2024",
               "actual_first_output": "pending", "current_status": "UNDER CONSTRUCTION",
               "promise_source": "https://example.com/p",
               "status_source": "https://example.com/s", "verification_tier": "P"}
        lead = psource.insert_lead(conn, promise_source="https://example.com/a",
                                   status_source="https://example.com/s", summary="x")
        self.rid = pscreen.insert_extracted(conn, row, source_collected_id=lead)
        pscreen.run_check(conn, self.rid)
        conn.commit(); conn.close()
        pdb.set_active_db(self.path)

    def tearDown(self):
        from pipeline import db as pdb
        pdb.set_active_db(None)
        self.dir.cleanup()

    def _inspect(self) -> str:
        from fastapi.testclient import TestClient
        from webapp.main import app
        return TestClient(app).get(f"/screen/{self.rid}/inspect").text

    def test_the_button_ships_without_disabled(self):
        """Server-rendered, so a reader with no JavaScript can still publish."""
        b = self._inspect()
        i = b.index('id="verifybtn"')
        self.assertNotIn("disabled", b[i:i + 160])

    def test_promotion_still_works_with_nothing_checked(self):
        """The gate is the check verdict and a person's judgement, not the
        scratchpad. This is the path the CLI takes too."""
        from pipeline import db as pdb, verify
        conn = pdb.connect(self.path)
        vid = verify.promote(conn, self.rid, verification_tier="V1")
        conn.commit()
        n = conn.execute("SELECT COUNT(*) FROM verify_verified").fetchone()[0]
        conn.close()
        self.assertTrue(vid)
        self.assertEqual(n, 1)

    def test_the_page_says_how_many_are_unchecked(self):
        """Not a gate, but not silent either: a button equally live at zero and
        at six would say the checklist is decoration."""
        self.assertIn('id="verifynote"', self._inspect())


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestModelCheckIsAnEscalation(unittest.TestCase):
    """The model check is reachable from a field, not a block on every page.

    It was a full-width panel at the foot of every inspect page, listing the
    same six fields the checklist already lists. Its one genuinely valuable
    case is narrow: the pane is a string matcher, so a value the page phrases
    differently looks identical to a value the page does not carry. Reading the
    page to tell those apart, and finding a URL that does carry it, is the part
    that is slow by hand -- and that is exactly the moment a reviewer marks a
    field "not in this source".
    """

    def setUp(self):
        from pipeline import db as pdb, screen as pscreen, source as psource
        self.dir = tempfile.TemporaryDirectory(**_TMPDIR_KW)
        self.path = Path(self.dir.name) / "t.db"
        conn = pdb.connect(self.path)
        pdb.init_db(conn)
        row = {"project": "Test Fab", "sector": "Semiconductors", "state": "TX",
               "announced": "2022-01", "promised_capital_usd": 5_000_000_000,
               "promised_jobs": 1500, "promised_first_output": "2024",
               "actual_first_output": "pending", "current_status": "UNDER CONSTRUCTION",
               "promise_source": "https://example.com/p",
               "status_source": "https://example.com/s", "verification_tier": "P"}
        lead = psource.insert_lead(conn, promise_source="https://example.com/a",
                                   status_source="https://example.com/s", summary="x")
        self.rid = pscreen.insert_extracted(conn, row, source_collected_id=lead)
        pscreen.run_check(conn, self.rid)
        conn.commit(); conn.close()
        pdb.set_active_db(self.path)

    def tearDown(self):
        from pipeline import db as pdb
        pdb.set_active_db(None)
        self.dir.cleanup()

    def _inspect(self) -> str:
        from fastapi.testclient import TestClient
        from webapp.main import app
        return TestClient(app).get(f"/screen/{self.rid}/inspect").text

    def test_the_panel_is_folded_not_open_on_the_page(self):
        b = self._inspect()
        i = b.index('id="agentbox"')
        self.assertTrue(b[i - 40:i].rstrip().endswith("<details class=\"byhand\""),
                        b[i - 60:i + 20])
        self.assertNotIn("<details class=\"byhand\" id=\"agentbox\" open", b)

    def test_every_field_carries_a_hidden_escalation(self):
        """Hidden in the markup, revealed by marking a field absent. Shipping
        it hidden rather than injecting it keeps the page working without
        JavaScript having to build controls."""
        b = self._inspect()
        self.assertEqual(b.count('class="ck-ask" hidden'), 6)

    def test_the_panel_still_exists_for_the_whole_project(self):
        """Folding is not deleting: someone who wants to ask about several
        fields at once can still open it."""
        self.assertIn('id="agentform"', self._inspect())


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestAttestRoute(unittest.TestCase):
    """Confirming a field writes a row, and only a named person can do it.

    The checklist used to live in the browser, so a tick was worth nothing the
    moment the tab closed and nothing could be said about who made it. These
    pin the two halves of the replacement: the identity cannot be invented, and
    the evidence about the page is taken from what the server rendered rather
    than from anything the page sends back.
    """

    WHO = "ashwin@industriousaf.org"
    PAGE = ("<html><body><p>The plant will create 1,500 jobs.</p>"
            "</body></html>")

    def setUp(self):
        from pipeline import db as pdb, source as psource, screen as pscreen
        from fastapi.testclient import TestClient
        from webapp.main import app
        self.dir = tempfile.TemporaryDirectory(**_TMPDIR_KW)
        self.path = Path(self.dir.name) / "t.db"
        conn = pdb.connect(self.path)
        pdb.init_db(conn)
        lead = psource.insert_lead(conn, promise_source="https://example.com/p",
                                   status_source="https://example.com/s", summary="x")
        self.sid = pscreen.insert_extracted(conn, {
            "project": "Attest Fab", "sector": "Semiconductors", "state": "TX",
            "announced": "2022-01", "promised_capital_usd": 5_000_000_000,
            "promised_jobs": 1500, "promised_first_output": "2024",
            "actual_first_output": "pending", "current_status": "UNDER CONSTRUCTION",
            "promise_source": "https://example.com/p",
            "status_source": "https://example.com/s",
            "verification_tier": "P"}, source_collected_id=lead)
        conn.commit(); conn.close()
        pdb.set_active_db(self.path)
        self.client = TestClient(app)
        # The pane fetches for real otherwise, and these tests must not leave
        # the machine. The stub is what render_document counts against.
        self._fetch = ev.fetch
        ev.fetch = lambda url, via="auto": {
            "ok": True, "via": "live", "final_url": url, "html": self.PAGE}

    def tearDown(self):
        from pipeline import db as pdb
        ev.fetch = self._fetch
        pdb.set_active_db(None)
        self.dir.cleanup()

    def _stored(self) -> list:
        from pipeline import db as pdb
        conn = pdb.connect(self.path)
        try:
            return conn.execute("SELECT * FROM screen_attested ORDER BY id").fetchall()
        finally:
            conn.close()

    def _attest(self, field="promised_jobs", state="confirmed", **extra):
        # "as" is the name the page shows, which the route checks against the
        # browser's choice.
        data = {"field": field, "state": state, "as": self.WHO, **extra}
        return self.client.post(f"/screen/{self.sid}/attest", data=data)

    def test_nobody_can_confirm_before_saying_who_they_are(self):
        r = self._attest()
        self.assertEqual(400, r.status_code)
        self.assertEqual([], self._stored())

    def test_an_address_off_the_list_cannot_be_chosen(self):
        """The buttons only offer listed addresses, so a hand-made request is the
        way to put a name into the record that the project never approved."""
        self.client.post(f"/screen/{self.sid}/verifier",
                         data={"verifier": "stranger@example.com"})
        self.assertEqual(400, self._attest(**{"as": "stranger@example.com"}).status_code)
        self.assertEqual([], self._stored())

    def test_confirming_records_who_and_which_page(self):
        self.client.post(f"/screen/{self.sid}/verifier", data={"verifier": self.WHO})
        self.client.get(f"/evidence/screen/{self.sid}?tab=0&field=promised_jobs")
        self.assertEqual(200, self._attest().status_code)
        row = self._stored()[0]
        self.assertEqual(self.WHO, row["attested_by"])
        self.assertEqual("promised_jobs", row["field"])
        self.assertEqual("https://example.com/p", row["source_url"])
        self.assertEqual("1500", row["value_at_time"])

    def test_a_confirmation_against_a_page_without_the_value_stores_zero(self):
        """The point of storing the count. The reviewer is not stopped -- a
        value phrased differently looks identical to one that is absent -- but
        the row says the page held no hits, and anyone can find those."""
        self.client.post(f"/screen/{self.sid}/verifier", data={"verifier": self.WHO})
        self.client.get(f"/evidence/screen/{self.sid}?tab=0&field=announced")
        self._attest(field="announced")
        self.assertEqual(0, self._stored()[0]["match_count"])

    def test_the_count_is_not_taken_from_the_browser(self):
        """The frame is sandboxed with no same-origin access, so nothing can be
        read out of it -- and anything the page sent instead would be a number
        the reviewer could choose. A field never opened stores no count at all
        rather than one the request supplied."""
        self.client.post(f"/screen/{self.sid}/verifier", data={"verifier": self.WHO})
        r = self.client.post(f"/screen/{self.sid}/attest",
                             data={"field": "promised_jobs", "state": "confirmed", "as": self.WHO,
                                   "match_count": "99", "source_url": "https://evil.test"})
        self.assertEqual(200, r.status_code)
        row = self._stored()[0]
        self.assertIsNone(row["match_count"])
        self.assertIsNone(row["source_url"])

    def test_the_page_comes_back_carrying_what_was_settled(self):
        """The reason closing the tab no longer loses your place."""
        self.client.post(f"/screen/{self.sid}/verifier", data={"verifier": self.WHO})
        self._attest()
        body = self.client.get(f"/screen/{self.sid}/inspect").text
        self.assertRegex(body, r"var ATTESTED = .*promised_jobs")
        self.assertIn("Verifying as", body)

    def test_the_page_does_not_still_call_the_checklist_unstored(self):
        """It said "It is not stored" for as long as that was true. A sentence
        telling a reviewer their ticks go nowhere, over a control that now
        publishes their name, is the worst of both."""
        body = self.client.get(f"/screen/{self.sid}/inspect").text
        self.assertNotIn("It is not stored", body)
        self.assertNotIn("scratchpad", body.lower())

    def test_another_databases_project_one_cannot_lend_its_evidence(self):
        """What the pane showed is remembered in the process, and ids restart at
        1 in every database. Before the key carried the database, opening a
        field on project #1 in one file left its URL and hit count sitting there
        for project #1 in the next -- and the app has a database switcher, so
        this is a click apart, not a contrivance."""
        from pipeline import db as pdb, source as psource, screen as pscreen
        self.client.post(f"/screen/{self.sid}/verifier", data={"verifier": self.WHO})
        self.client.get(f"/evidence/screen/{self.sid}?tab=0&field=promised_jobs")

        other = Path(self.dir.name) / "other.db"
        conn = pdb.connect(other); pdb.init_db(conn)
        lead = psource.insert_lead(conn, promise_source="https://elsewhere.test/p",
                                   status_source="https://elsewhere.test/s", summary="y")
        sid = pscreen.insert_extracted(conn, {
            "project": "Other Fab", "sector": "Semiconductors", "state": "AZ",
            "announced": "2023-01", "promised_capital_usd": 2_000_000_000,
            "promised_jobs": 800, "promised_first_output": "2026",
            "actual_first_output": "pending", "current_status": "ANNOUNCED",
            "promise_source": "https://elsewhere.test/p",
            "status_source": "https://elsewhere.test/s",
            "verification_tier": "P"}, source_collected_id=lead)
        conn.commit(); conn.close()
        self.assertEqual(self.sid, sid)            # the collision this is about
        pdb.set_active_db(other)

        self.client.post(f"/screen/{sid}/verifier", data={"verifier": self.WHO})
        self._attest()
        conn = pdb.connect(other)
        try:
            row = conn.execute("SELECT * FROM screen_attested").fetchone()
        finally:
            conn.close()
        self.assertIsNone(row["source_url"])
        self.assertIsNone(row["match_count"])

    def test_a_stale_field_has_to_be_opened_again(self):
        """Confirm a field, edit it, and the old look does not carry over. The
        browser still remembers opening it this visit, but what was on screen
        then is not the value being asked about now."""
        self.client.post(f"/screen/{self.sid}/verifier", data={"verifier": self.WHO})
        self._attest()
        from pipeline import db as pdb
        conn = pdb.connect(self.path)
        conn.execute("UPDATE screen_extracted SET promised_jobs = 4200 WHERE id = ?",
                     (self.sid,))
        conn.commit(); conn.close()
        body = self.client.get(f"/screen/{self.sid}/inspect").text
        self.assertRegex(body, r'"promised_jobs": \{"state": "confirmed", "stale": true')
        self.assertIn("delete looked[k]", body)

    def test_a_read_only_process_records_nothing(self):
        """$TRACKER_READONLY opens the file read-only, and the write would
        otherwise raise SystemExit from inside a request handler -- a refusal
        that reads as a crash. The route checks first and answers in words."""
        from webapp import screen as webscreen
        self.client.post(f"/screen/{self.sid}/verifier", data={"verifier": self.WHO})
        webscreen.READ_ONLY = True
        try:
            r = self._attest()
        finally:
            webscreen.READ_ONLY = False
        self.assertEqual(400, r.status_code)
        self.assertIn("TRACKER_READONLY", r.json()["error"])
        self.assertEqual([], self._stored())

    def test_the_checklist_and_the_writer_share_one_list_of_fields(self):
        """Two copies of a list like this is the bug this repository keeps
        rediscovering, and here it would mean the interface offering a field the
        writer rejects. It lives in this module and not test_pipeline.py because
        reading it imports the web app, which is the one part with dependencies."""
        from pipeline import screen as pscreen
        from webapp import screen as webscreen
        self.assertIs(webscreen.CHECKLIST_CELLS, pscreen.ATTESTABLE_FIELDS)

    def test_confirming_an_absence_is_refused_at_the_route(self):
        self.client.post(f"/screen/{self.sid}/verifier", data={"verifier": self.WHO})
        r = self.client.post(f"/screen/{self.sid}/attest",
                             data={"field": "promised_first_output", "state": "confirmed",
                                   "as": self.WHO, "value": "n/a"})
        self.assertEqual(400, r.status_code)
        self.assertIn("not in this source", r.json()["error"])
        self.assertEqual([], self._stored())

    def test_the_page_value_is_what_gets_recorded(self):
        """A typed correction is the claim; the stored value is what it replaces."""
        self.client.post(f"/screen/{self.sid}/verifier", data={"verifier": self.WHO})
        self.client.post(f"/screen/{self.sid}/attest",
                         data={"field": "promised_jobs", "state": "confirmed",
                               "as": self.WHO, "value": "1750"})
        self.assertEqual("1750", self._stored()[0]["value_at_time"])

    def test_the_page_knows_which_values_record_an_absence(self):
        """Sent from the server, so the checklist and the writer cannot disagree."""
        body = self.client.get(f"/screen/{self.sid}/inspect").text
        self.assertIn('var ABSENT = ["", "n/a"];', body)

    def test_with_nobody_chosen_the_page_asks_rather_than_names(self):
        """It read "You are verifying as:" followed by both addresses, which
        made the first one look already chosen."""
        from pipeline import settings
        body = self.client.get(f"/screen/{self.sid}/inspect").text
        self.assertIn("Who is verifying?", body)
        self.assertNotIn("You are verifying as", body)
        for p in settings.verifiers():
            self.assertIn(f"I am {p}", body)

    def test_a_link_cannot_choose_who_is_verifying(self):
        """A link can be bookmarked or shared, and opening one used to change
        the name on every settle after it."""
        self.client.get(f"/screen/{self.sid}/inspect", params={"verifier": self.WHO})
        self.assertEqual(400, self._attest().status_code)
        self.assertEqual([], self._stored())

    def test_nothing_switches_straight_to_someone_else(self):
        from pipeline import settings
        self.client.post(f"/screen/{self.sid}/verifier", data={"verifier": self.WHO})
        body = self.client.get(f"/screen/{self.sid}/inspect").text
        self.assertIn("Not you?", body)
        for p in settings.verifiers():
            if p != self.WHO:
                self.assertNotIn(f'value="{p}"', body)
                self.assertNotIn(f"verifier={p}", body)

    def test_not_you_clears_the_choice(self):
        self.client.post(f"/screen/{self.sid}/verifier", data={"verifier": self.WHO})
        self.client.post(f"/screen/{self.sid}/verifier", data={"verifier": ""})
        self.assertIn("Who is verifying?", self.client.get(f"/screen/{self.sid}/inspect").text)
        self.assertEqual(400, self._attest().status_code)

    def test_a_settle_is_refused_when_the_page_shows_someone_else(self):
        """Two tabs: choose again in one, and the other still shows the old
        name. Its settles would be stored under a name it never showed."""
        from pipeline import settings
        other = next(p for p in settings.verifiers() if p != self.WHO)
        self.client.post(f"/screen/{self.sid}/verifier", data={"verifier": self.WHO})
        r = self._attest(**{"as": other})
        self.assertEqual(400, r.status_code)
        self.assertIn("Reload the page", r.json()["error"])
        self.assertEqual([], self._stored())

    def test_a_settle_from_a_page_that_names_no_one_is_refused(self):
        """A page opened before this check sends no name at all."""
        self.client.post(f"/screen/{self.sid}/verifier", data={"verifier": self.WHO})
        r = self.client.post(f"/screen/{self.sid}/attest",
                             data={"state": "confirmed", "field": "promised_jobs"})
        self.assertEqual(400, r.status_code)
        self.assertEqual([], self._stored())

    def test_someone_elses_settle_leaves_the_field_open_for_you(self):
        """Ashwin's settles from 11 September showed as done when Lucas opened
        #1 and #5, so he published seven fields he never settled, and pressing
        "confirmed" on a field already showing it did nothing."""
        from pipeline import settings
        other = next(p for p in settings.verifiers() if p != self.WHO)
        self.client.post(f"/screen/{self.sid}/verifier", data={"verifier": other})
        self.assertEqual(200, self._attest(**{"as": other}).status_code)
        self.client.post(f"/screen/{self.sid}/verifier", data={"verifier": self.WHO})
        body = self.client.get(f"/screen/{self.sid}/inspect").text
        self.assertIn(f'"by": "{other}"', body)
        self.assertIn("if (a.by !== VERIFIER) { other[f] = a; return; }", body)
        self.assertEqual(200, self._attest().status_code)
        self.assertEqual([other, self.WHO], [r["attested_by"] for r in self._stored()])


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestProjectName(unittest.TestCase):
    """Every page says what the project is called.

    The masthead carried the organisation's wordmark and the database filename
    and nothing else, so the only place the project's own name appeared was the
    URL -- and after the rename the filename said `tracker.db` while no page
    said Tracker. A reader could not learn the name from the interface.
    """

    def setUp(self):
        from fastapi.testclient import TestClient
        from webapp.main import app
        self.client = TestClient(app)

    def test_the_masthead_carries_it_on_every_page(self):
        from webapp.shared import PROJECT_NAME
        for path in ("/", "/source", "/screen", "/verify"):
            with self.subTest(path=path):
                self.assertIn(PROJECT_NAME, self.client.get(path).text)

    def test_it_is_defined_once(self):
        """A name spelled out at four call sites is a name that will drift."""
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent / "webapp"
        hits = [f.name for f in sorted(root.glob("*.py"))
                if re.search(r'"Promised vs\. Produced', f.read_text())]
        self.assertEqual(["shared.py"], hits)


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestTheQueueIsWalkedTopDown(unittest.TestCase):
    """The interface walks the queue in the order the dashboard promises.

    The dashboard said "largest capital first, so wherever you stop, the
    Tracker above that point is complete". Its button opened a list sorted by
    id, and after each verification there was no next project, so the way back
    to work was always that list. The first thirty projects were verified as
    ids 1 to 30 exactly, while three projects over $10B waited below them. The
    reviewer did precisely what the interface showed; the interface was wrong.
    """

    def setUp(self):
        from pipeline import db as pdb, screen as pscreen, source as psource
        from fastapi.testclient import TestClient
        from webapp.main import app
        self.dir = tempfile.TemporaryDirectory(**_TMPDIR_KW)
        self.path = Path(self.dir.name) / "t.db"
        conn = pdb.connect(self.path)
        pdb.init_db(conn)
        base = {"sector": "Semiconductors", "state": "TX", "announced": "2022-01",
                "promised_jobs": 1500, "promised_first_output": "2024",
                "actual_first_output": "pending", "current_status": "UNDER CONSTRUCTION",
                "promise_source": "https://example.com/p",
                "status_source": "https://example.com/s", "verification_tier": "P"}
        made = {}
        # Inserted smallest first, so id order and capital order disagree. The
        # blocked one is the largest, so a capital sort that forgot to sink it
        # would put a dead end at the top of the list.
        for name, cap, extra in (("Small", 1_200_000_000, {}),
                                 ("Big", 20_000_000_000, {}),
                                 ("Blocked", 30_000_000_000,
                                  {"promised_date_source": "not-a-url"})):
            lead = psource.insert_lead(conn, promise_source=f"https://example.com/{name}",
                                       status_source="https://example.com/s", summary="x")
            made[name] = pscreen.insert_extracted(
                conn, dict(base, project=name, promised_capital_usd=cap, **extra),
                source_collected_id=lead)
            pscreen.run_check(conn, made[name])
        conn.commit(); conn.close()
        self.small, self.big, self.blocked = made["Small"], made["Big"], made["Blocked"]
        pdb.set_active_db(self.path)
        self.client = TestClient(app)

    def tearDown(self):
        from pipeline import db as pdb
        pdb.set_active_db(None)
        self.dir.cleanup()

    def _cards(self, path="/screen?show=pending"):
        # Only the per-project buttons. The lede above the list also links the
        # blocked projects, and reading those would test the wrong thing.
        return [int(i) for i in re.findall(
            r'href="/screen/(\d+)/inspect"><button type="button" class="primary">Inspect',
            self.client.get(path).text)]

    def test_the_list_is_largest_capital_first_with_blocked_last(self):
        self.assertEqual([self.big, self.small, self.blocked], self._cards())

    def test_first_up_on_the_dashboard_opens_that_project(self):
        self.assertIn(f'First up: <a href="/screen/{self.big}/inspect">',
                      self.client.get("/").text)

    def test_verifying_opens_the_next_project_by_capital(self):
        r = self.client.post(f"/screen/{self.big}/promote", data={"tier": "V1"},
                             follow_redirects=False)
        self.assertEqual(303, r.status_code)
        self.assertTrue(
            r.headers["location"].startswith(f"/screen/{self.small}/inspect?verified="),
            r.headers["location"])

    def test_the_next_page_links_back_to_the_record_just_published(self):
        """The old landing page was the published record, the one place to see
        what just went out. Moving on must not cost that."""
        body = self.client.post(f"/screen/{self.big}/promote", data={"tier": "V1"}).text
        self.assertIn("Open the record you just verified", body)
        self.assertIn("next project by capital", body)

    def test_the_last_one_lands_on_its_record(self):
        from urllib.parse import unquote
        self.client.post(f"/screen/{self.big}/promote", data={"tier": "V1"})
        r = self.client.post(f"/screen/{self.small}/promote", data={"tier": "V1"},
                             follow_redirects=False)
        loc = unquote(r.headers["location"])
        self.assertTrue(loc.startswith("/verify/"), loc)
        self.assertIn("last project waiting", loc)

    def test_a_project_name_with_an_ampersand_survives_the_redirect(self):
        """The message now names the project, and names like "Johnson & Johnson"
        would split a query string that was not encoded."""
        from pipeline import db as pdb
        conn = pdb.connect(self.path)
        conn.execute("UPDATE screen_extracted SET project = 'Big & Bold' WHERE id = ?",
                     (self.big,))
        conn.commit(); conn.close()
        body = self.client.post(f"/screen/{self.big}/promote", data={"tier": "V1"}).text
        self.assertIn("Verified Big &amp; Bold at tier V1.", body)


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestCorrectionsAreChecked(unittest.TestCase):
    """Typed corrections meet the checker, and the date fields offer their words.

    A correction used to be stored as typed at every door, so a misspelt
    `penidng` published a record the checker fails. The inspect page also
    published before it corrected, which meant any refusal there would have
    left the old value live. And the two date fields were free text beside a
    sector field that was already a picker.
    """

    def setUp(self):
        from pipeline import db as pdb, screen as pscreen, source as psource
        from fastapi.testclient import TestClient
        from webapp.main import app
        self.dir = tempfile.TemporaryDirectory(**_TMPDIR_KW)
        self.path = Path(self.dir.name) / "t.db"
        conn = pdb.connect(self.path)
        pdb.init_db(conn)
        lead = psource.insert_lead(conn, promise_source="https://example.com/p",
                                   status_source="https://example.com/s", summary="x")
        self.sid = pscreen.insert_extracted(conn, {
            "project": "Check Fab", "sector": "Semiconductors", "state": "TX",
            "announced": "2022-01", "promised_capital_usd": 5_000_000_000,
            "promised_jobs": 1500, "promised_first_output": "2024",
            "actual_first_output": "pending", "current_status": "UNDER CONSTRUCTION",
            "promise_source": "https://example.com/p",
            "status_source": "https://example.com/s", "verification_tier": "P"},
            source_collected_id=lead)
        pscreen.run_check(conn, self.sid)
        conn.commit(); conn.close()
        pdb.set_active_db(self.path)
        self.client = TestClient(app)

    def tearDown(self):
        from pipeline import db as pdb
        pdb.set_active_db(None)
        self.dir.cleanup()

    def _one(self, sql, *args):
        from pipeline import db as pdb
        conn = pdb.connect(self.path)
        try:
            return conn.execute(sql, args).fetchone()
        finally:
            conn.close()

    def test_a_typo_at_the_verify_button_publishes_nothing(self):
        from urllib.parse import unquote
        r = self.client.post(f"/screen/{self.sid}/promote",
                             data={"tier": "V1", "actual_first_output": "penidng",
                                   "edit_description": "typo"}, follow_redirects=False)
        self.assertEqual(0, self._one("SELECT count(*) FROM verify_verified")[0])
        self.assertIn("nothing was published", unquote(r.headers["location"]))

    def test_a_valid_correction_at_the_verify_button_publishes_with_it(self):
        self.client.post(f"/screen/{self.sid}/promote",
                         data={"tier": "V1", "promised_first_output": "2025-Q4",
                               "edit_description": "per the release"})
        self.assertEqual("2025-Q4", self._one(
            "SELECT promised_first_output FROM verify_verified")[0])

    def test_a_typo_on_the_verify_edit_page_is_not_saved(self):
        from urllib.parse import unquote
        self.client.post(f"/screen/{self.sid}/promote", data={"tier": "V1"})
        vid = self._one("SELECT id FROM verify_verified")[0]
        r = self.client.post(f"/verify/{vid}/edit",
                             data={"actual_first_output": "penidng", "edit_description": "typo"},
                             follow_redirects=False)
        self.assertEqual("pending", self._one(
            "SELECT actual_first_output FROM verify_verified WHERE id = ?", vid)[0])
        self.assertIn("Not saved", unquote(r.headers["location"]))
        self.assertIn("penidng", unquote(r.headers["location"]),
                      "the message must survive a quoted value in the URL")

    def test_each_date_field_offers_only_its_own_words(self):
        body = self.client.get(f"/screen/{self.sid}/inspect").text
        pick = lambda col: re.search(
            r'<select class="datekind" data-for="%s"[^>]*>(.*?)</select>' % col, body, re.S).group(1)
        self.assertIn('value="n/a"', pick("promised_first_output"))
        self.assertNotIn('value="pending"', pick("promised_first_output"))
        for word in ("pending", "unconfirmed", "never"):
            self.assertIn(f'value="{word}"', pick("actual_first_output"))
        self.assertNotIn('value="n/a"', pick("actual_first_output"))

    def test_the_picker_tells_pending_from_unconfirmed(self):
        """The two opposites, told apart at the moment of choice."""
        body = self.client.get(f"/screen/{self.sid}/inspect").text
        self.assertIn("pending: not producing yet", body)
        self.assertIn("unconfirmed: producing, but no source dates it", body)

    def test_the_picker_never_submits(self):
        """Only the text box is sent, so neither save handler had to change."""
        body = self.client.get(f"/screen/{self.sid}/inspect").text
        tags = re.findall(r'<select class="datekind"[^>]*>', body)
        self.assertEqual(2, len(tags))
        for tag in tags:
            self.assertNotIn("name=", tag)

    def test_the_verify_edit_page_has_the_picker_and_its_script(self):
        self.client.post(f"/screen/{self.sid}/promote", data={"tier": "V1"})
        vid = self._one("SELECT id FROM verify_verified")[0]
        body = self.client.get(f"/verify/{vid}").text
        self.assertIn('data-for="actual_first_output"', body)
        self.assertIn("select.datekind", body)

    def test_the_promised_hint_names_its_sentinel(self):
        """It explained dates and never mentioned `n/a`, the one word that field
        accepts."""
        body = self.client.get(f"/screen/{self.sid}/inspect").text
        self.assertIn("<code>n/a</code> if no source states one", body)


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestInspectShowsThePublishedRecord(unittest.TestCase):
    """A published project's inspect page shows, marks and settles what went out.

    It showed the Screen record, so a field corrected at verification could only
    be settled against the value the correction replaced. It also still offered
    the verify button, which on a published project can only fail.
    """

    WHO = "ashwin@industriousaf.org"
    PAGE = "<html><body><p>The plant will create 2,300 jobs.</p></body></html>"

    def setUp(self):
        from pipeline import db as pdb, screen as pscreen, source as psource, verify as pverify
        from fastapi.testclient import TestClient
        from webapp.main import app
        self.dir = tempfile.TemporaryDirectory(**_TMPDIR_KW)
        self.path = Path(self.dir.name) / "t.db"
        conn = pdb.connect(self.path)
        pdb.init_db(conn)
        lead = psource.insert_lead(conn, promise_source="https://example.com/p",
                                   status_source="https://example.com/s", summary="x")
        self.sid = pscreen.insert_extracted(conn, {
            "project": "Published Fab", "sector": "Semiconductors", "state": "TX",
            "announced": "2022-01", "promised_capital_usd": 5_000_000_000,
            "promised_jobs": 1500, "promised_first_output": "2024",
            "actual_first_output": "pending", "current_status": "UNDER CONSTRUCTION",
            "promise_source": "https://example.com/p",
            "status_source": "https://example.com/s", "verification_tier": "P"},
            source_collected_id=lead)
        pscreen.run_check(conn, self.sid)
        self.vid = pverify.promote(conn, self.sid, verification_tier="V1",
                                   flag="Resolved: checked.")
        pverify.edit(conn, self.vid, {"promised_jobs": "2300"},
                     edit_description="per the release")
        conn.commit(); conn.close()
        pdb.set_active_db(self.path)
        self.client = TestClient(app)
        self._fetch = ev.fetch
        ev.fetch = lambda url, via="auto": {
            "ok": True, "via": "live", "final_url": url, "html": self.PAGE}

    def tearDown(self):
        from pipeline import db as pdb
        ev.fetch = self._fetch
        pdb.set_active_db(None)
        self.dir.cleanup()

    def _inspect(self) -> str:
        return self.client.get(f"/screen/{self.sid}/inspect").text

    def test_the_boxes_hold_the_published_values(self):
        self.assertIn('name="promised_jobs" value="2300"', self._inspect())

    def test_it_is_read_only_and_offers_no_second_publish(self):
        body = self._inspect()
        self.assertNotIn('id="verifybtn"', body)
        self.assertIn('name="promised_jobs" value="2300" readonly', body)
        self.assertNotIn('class="datekind"', body)

    def test_corrections_are_sent_to_the_verify_page(self):
        self.assertIn(f'href="/verify/{self.vid}"', self._inspect())

    def test_the_pane_marks_the_published_value(self):
        """2,300 is on the page and 1,500 is not, so a count of one means the
        pane searched for what went out."""
        self.client.get(f"/evidence/screen/{self.sid}?tab=0&field=promised_jobs")
        self.assertEqual(1, ev.last_shown("screen", self.sid, "promised_jobs")["count"])

    def test_the_model_check_is_asked_about_the_published_value(self):
        """It read the Screen record, so on a corrected field the model was asked
        whether the page carries the value the correction replaced."""
        from webapp import agent as wa
        self.assertEqual("2300", str(wa._row_for("screen", self.sid)["promised_jobs"]))

    def test_a_settle_here_vouches_for_the_published_value(self):
        from pipeline import db as pdb
        self.client.post(f"/screen/{self.sid}/verifier", data={"verifier": self.WHO})
        self.client.post(f"/screen/{self.sid}/attest",
                         data={"field": "promised_jobs", "state": "confirmed", "as": self.WHO})
        conn = pdb.connect(self.path)
        try:
            got = conn.execute("SELECT value_at_time FROM screen_attested").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual("2300", got)


@unittest.skipUnless(HAVE_WEBAPP, "the web interface needs FastAPI installed")
class TestTheResettleFilter(unittest.TestCase):
    """The Screen list counts the fields to settle again and filters to them.

    Once the inspect page showed published values, a batch of fields needed a
    re-settle, and nothing in the interface listed them: finding them took a query.
    """

    WHO = "ashwin@industriousaf.org"

    def setUp(self):
        from pipeline import db as pdb, screen as pscreen, source as psource, verify as pverify
        from fastapi.testclient import TestClient
        from webapp.main import app
        self.dir = tempfile.TemporaryDirectory(**_TMPDIR_KW)
        self.path = Path(self.dir.name) / "t.db"
        conn = pdb.connect(self.path)
        pdb.init_db(conn)
        base = {"sector": "Semiconductors", "state": "TX", "announced": "2022-01",
                "promised_capital_usd": 5_000_000_000, "promised_jobs": 1500,
                "promised_first_output": "2024", "actual_first_output": "pending",
                "current_status": "UNDER CONSTRUCTION",
                "promise_source": "https://example.com/p",
                "status_source": "https://example.com/s", "verification_tier": "P"}
        ids = {}
        for name in ("Flagged Fab", "Quiet Fab"):
            lead = psource.insert_lead(conn, promise_source=f"https://example.com/{name[0]}",
                                       status_source="https://example.com/s", summary="x")
            ids[name] = pscreen.insert_extracted(conn, dict(base, project=name),
                                                 source_collected_id=lead)
            pscreen.run_check(conn, ids[name])
        self.flagged, self.quiet = ids["Flagged Fab"], ids["Quiet Fab"]
        # Settled against 2024, then published with a correction, so the settle
        # vouches for a value that did not go out.
        pscreen.attest(conn, self.flagged, "promised_first_output", "confirmed",
                       self.WHO, value="2024")
        vid = pverify.promote(conn, self.flagged, verification_tier="V1",
                              flag="Resolved: checked.")
        pverify.edit(conn, vid, {"promised_first_output": "2025-Q4"},
                     edit_description="per the release")
        conn.commit(); conn.close()
        pdb.set_active_db(self.path)
        self.client = TestClient(app)

    def tearDown(self):
        from pipeline import db as pdb
        pdb.set_active_db(None)
        self.dir.cleanup()

    def _cards(self, body):
        return [int(i) for i in re.findall(
            r'href="/screen/(\d+)/inspect"><button type="button" class="primary">Inspect', body)]

    def test_the_list_counts_them_and_links_to_the_filter(self):
        body = self.client.get("/screen").text
        self.assertIn("1 field(s) on 1 project(s) need a re-settle", body)
        self.assertIn('href="/screen?resettle=1"', body)

    def test_the_filter_shows_only_them_even_with_not_yet_verified_chosen(self):
        """Nearly every project to re-settle is already verified, so the "not yet
        verified" toggle would hide them all if the filter honoured it."""
        body = self.client.get("/screen?resettle=1&show=pending").text
        self.assertEqual([self.flagged], self._cards(body))

    def test_a_flagged_card_names_its_fields_and_offers_the_resettle(self):
        body = self.client.get("/screen?show=all").text
        self.assertIn("re-settle: promised_first_output", body)
        self.assertIn("Inspect &amp; re-settle", body)

    def test_nothing_to_resettle_means_no_count_and_no_link(self):
        from pipeline import db as pdb
        conn = pdb.connect(self.path)
        conn.execute("DELETE FROM screen_attested")
        conn.commit(); conn.close()
        body = self.client.get("/screen").text
        self.assertNotIn("need a re-settle", body)
        self.assertNotIn("resettle=1", body)

    def test_a_blocked_project_is_left_out_of_the_worklist(self):
        """A failing check means it cannot be verified, so re-settling it changes
        nothing a reader sees. Gulf Coast Growth Ventures was the case that showed
        this: blocked below the size floor, carrying a confirmed empty capital."""
        from pipeline import db as pdb, screen as pscreen, source as psource
        from pipeline.db import now_iso
        conn = pdb.connect(self.path)
        lead = psource.insert_lead(conn, promise_source="https://example.com/b",
                                   status_source="https://example.com/s", summary="x")
        blocked = pscreen.insert_extracted(conn, {
            "project": "Blocked Fab", "sector": "Semiconductors", "state": "TX",
            "announced": "2022-01", "promised_capital_usd": 5_000_000_000,
            "promised_jobs": 1500, "promised_first_output": "2024",
            "actual_first_output": "pending", "current_status": "UNDER CONSTRUCTION",
            "promise_source": "https://example.com/b",
            "status_source": "https://example.com/s",
            "promised_date_source": "not-a-url", "verification_tier": "P"},
            source_collected_id=lead)
        pscreen.run_check(conn, blocked)
        conn.execute(
            "INSERT INTO screen_attested (datetime, screen_extracted_id, field, state, "
            "value_at_time, attested_by) VALUES (?, ?, 'promised_jobs', 'confirmed', '', ?)",
            (now_iso(), blocked, self.WHO))
        conn.commit(); conn.close()
        self.assertIn("1 field(s) on 1 project(s) need a re-settle",
                      self.client.get("/screen").text)
        self.assertEqual([self.flagged],
                         self._cards(self.client.get("/screen?resettle=1").text))
