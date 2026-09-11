"""Regression tests for the places where a mistake corrupts data silently.

Every test here is a real incident. The Scoreboard had no tests until 2026-09-05,
and the defects found by hand in the days before that are the specification: a
duplicate row that made a run report success one project short, a slip sentinel
that marked produced projects as censored, an export that overwrote the real
CSVs from a scratch database, the literal string "None" stored as data.

Two of them were introduced while fixing the others, which is the actual
argument for this file. Hand-verification caught both, but only because the
right thing happened to be checked.

Run:  python3 -m unittest discover -s tests
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import dates, quality, screen, source, verify  # noqa: E402
from pipeline import settings  # noqa: E402
from pipeline import schema_check as sc  # noqa: E402
from pipeline.db import connect, init_db  # noqa: E402
from pipeline.export_tables import export_dir  # noqa: E402


def a_row(**over) -> dict:
    """A complete, valid v0 row. Override one cell to test that cell."""
    row = {
        "project": "Test Fab", "sector": "Semiconductors", "state": "TX",
        "announced": "2022-01", "promised_capital_usd": 5_000_000_000,
        "promised_jobs": 1500, "promised_first_output": "2024",
        "actual_first_output": "pending", "current_status": "UNDER CONSTRUCTION",
        "promise_source": "https://example.com/promise",
        "status_source": "https://example.com/status",
        # Stored rows always carry this: insert_extracted forces it. check_row
        # sees rows as they are in the database, so the fixture must too.
        "verification_tier": "P",
    }
    row.update(over)
    return row


class Base(unittest.TestCase):
    """Each test gets its own database. Never the real one -- three of this
    project's incidents were verification steps writing to the live database."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "t.db"
        self.conn = connect(self.path)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.dir.cleanup()

    def lead(self, **over) -> int:
        return source.insert_lead(
            self.conn,
            promise_source=over.get("promise_source", "https://example.com/p"),
            status_source=over.get("status_source", "https://example.com/s"),
            promised_date_source=over.get("promised_date_source"),
            summary=over.get("summary", "a test lead"))


# --------------------------------------------------------------------------- #
class TestLagAndSlip(Base):
    """dates.compute_lag_slip -- the two numbers the paper is about."""

    def test_no_promise_is_not_censored(self):
        """A project that PRODUCED but never had a promised date is not
        'to be completed'. Four rows of the first batch were marked censored
        while carrying real first-output dates."""
        *_, lag, slip = dates.compute_lag_slip("2022-01", "unconfirmed", "2024-06")
        self.assertEqual(slip, dates.NO_PROMISE)
        self.assertNotEqual(slip, dates.TO_BE_COMPLETED)
        self.assertGreater(lag, 0, "lag is announcement-anchored and must survive")

    def test_not_produced_is_censored(self):
        _, _, _, lag, slip = dates.compute_lag_slip("2022-01", "2025", "pending")
        self.assertEqual(slip, dates.TO_BE_COMPLETED)
        self.assertEqual(lag, dates.TO_BE_COMPLETED)

    def test_early_delivery_is_a_real_measurement(self):
        """Negative slip means EARLY. Treating `slip >= 0` as 'measurable'
        discards genuine observations -- a bug written into the quality panel
        and caught only by a row that delivered four months ahead."""
        *_, slip = dates.compute_lag_slip("2025-01", "2026-07", "2026-03")
        self.assertLess(slip, 0)
        self.assertNotIn(slip, (dates.TO_BE_COMPLETED, dates.CANCELLED, dates.NO_PROMISE))

    def test_produced_but_undated_is_not_censored(self):
        """'unconfirmed' means the sources say it produced and give no date --
        an event with a missing date. Five of fifty-three rows were sitting in
        the censored set with a current_status reading "IN FULL OPERATION"."""
        *_, lag, slip = dates.compute_lag_slip("2019-07", "2021", "unconfirmed")
        self.assertEqual(lag, dates.PRODUCED_UNDATED)
        self.assertEqual(slip, dates.PRODUCED_UNDATED)
        self.assertNotEqual(lag, dates.TO_BE_COMPLETED)

    def test_pending_is_still_censored(self):
        """The other half of the split: 'pending' must keep meaning censored."""
        *_, lag, _ = dates.compute_lag_slip("2019-07", "2021", "pending")
        self.assertEqual(lag, dates.TO_BE_COMPLETED)

    def test_lag_ignores_the_promise(self):
        """Gestation is announced -> actual. Re-promising must not move it."""
        _, _, _, lag_a, _ = dates.compute_lag_slip("2021-09", "2025", "2026-03")
        _, _, _, lag_b, _ = dates.compute_lag_slip("2021-09", "2029", "2026-03")
        self.assertEqual(lag_a, lag_b)


# --------------------------------------------------------------------------- #
class TestCoercion(Base):
    """screen._coerce -- a missing value that arrived as text."""

    def test_stringified_nulls_are_blanked(self):
        """`promised_date_source` held the four characters "None" and was caught
        only because that column happens to be URL-checked."""
        for col in ("flag", "current_status", "promised_date_source", "project"):
            for bad in ("None", "null", "undefined", "nan"):
                self.assertIsNone(screen._coerce(col, bad), f"{col}={bad!r}")

    def test_date_sentinels_survive(self):
        """'n/a' is a documented DATE_SENTINEL in the two first-output columns:
        a real answer, not an absence."""
        self.assertEqual(screen._coerce("promised_first_output", "n/a"), "n/a")
        self.assertEqual(screen._coerce("actual_first_output", "pending"), "pending")

    def test_text_nulls_blanked_in_date_columns_too(self):
        """Exempting those columns wholesale was the first attempt, and it let
        'None' stand in a date cell."""
        self.assertIsNone(screen._coerce("promised_first_output", "None"))
        self.assertIsNone(screen._coerce("actual_first_output", "undefined"))


# --------------------------------------------------------------------------- #
class TestOneLeadOneRow(Base):
    """screen.insert_extracted -- the duplicate that made a run lie."""

    def test_second_extraction_is_refused(self):
        lead = self.lead()
        screen.insert_extracted(self.conn, a_row(), source_collected_id=lead)
        with self.assertRaises(screen.DuplicateExtraction):
            screen.insert_extracted(self.conn, a_row(), source_collected_id=lead)

    def test_replace_supersedes(self):
        lead = self.lead()
        first = screen.insert_extracted(self.conn, a_row(), source_collected_id=lead)
        second = screen.insert_extracted(
            self.conn, a_row(project="Corrected"), source_collected_id=lead, replace=True)
        self.assertNotEqual(first, second)
        self.assertEqual(screen.extracted_for_source(self.conn, lead), [second])

    def test_failed_replace_leaves_the_original(self):
        """Removing before inserting looked tidier and was wrong: the insert can
        still fail, and the row being corrected would already be gone."""
        lead = self.lead()
        first = screen.insert_extracted(self.conn, a_row(), source_collected_id=lead)
        with self.assertRaises(sqlite3.IntegrityError):
            screen.insert_extracted(self.conn, {"project": "incomplete"},
                                    source_collected_id=lead, replace=True)
        self.assertEqual(screen.extracted_for_source(self.conn, lead), [first])

    def test_distinct_count_is_projects_not_rows(self):
        """The loop stops on this number, and counting rows let a duplicate tick
        it -- an N=20 run reported success one real project short.

        The duplicate is inserted with raw SQL on purpose: the guard above now
        prevents it through the normal path, so the only way to test what this
        function defends against is to recreate the state the guard was added
        for. Three leads, four rows, three projects."""
        leads = [self.lead() for _ in range(3)]
        for lead in leads:
            screen.insert_extracted(self.conn, a_row(), source_collected_id=lead)
        self.conn.execute(
            "INSERT INTO screen_extracted (datetime, source_collected_id, project, "
            "sector, state, announced, current_status, verification_tier) "
            "VALUES ('2026-01-01T00:00:00Z', ?, 'Dup', 'Semiconductors', 'TX', "
            "'2022-01', 'X', 'P')", (leads[0],))
        self.conn.commit()
        rows = self.conn.execute("SELECT COUNT(*) FROM screen_extracted").fetchone()[0]
        self.assertEqual(rows, 4, "fixture must actually contain a duplicate")
        self.assertEqual(screen.distinct_project_count(self.conn), 3)

    def test_rows_without_lineage_each_count_once(self):
        """A hand-added row has no source_collected_id and cannot be compared to
        anything, so COALESCE(source_collected_id, -id) gives each its own key."""
        for _ in range(2):
            screen.insert_extracted(self.conn, a_row())
        self.assertEqual(screen.distinct_project_count(self.conn), 2)


# --------------------------------------------------------------------------- #
class TestRemoval(Base):
    def test_published_rows_cannot_be_removed(self):
        """Deleting one would leave published research data citing nothing."""
        sid = screen.insert_extracted(
            self.conn, a_row(), source_collected_id=self.lead())
        screen.run_check(self.conn, sid)   # promote refuses an unchecked row
        verify.promote(self.conn, sid, verification_tier="V1", flag="test")
        with self.assertRaises(screen.RemovalBlocked):
            screen.remove_extracted(self.conn, sid)

    def test_unpublished_row_and_its_checks_go(self):
        sid = screen.insert_extracted(
            self.conn, a_row(), source_collected_id=self.lead())
        screen.run_check(self.conn, sid)
        gone = screen.remove_extracted(self.conn, sid)
        self.assertEqual(gone["checks"], 1)
        self.assertIsNone(screen.get_extracted(self.conn, sid))


# --------------------------------------------------------------------------- #
class TestChecker(Base):
    def test_text_null_in_a_provenance_cell_is_an_error(self):
        result = sc.check_row(a_row(promised_date_source="None"))
        self.assertEqual(result["result_status"], "FAIL")

    def test_a_clean_row_passes(self):
        self.assertIn(sc.check_row(a_row())["result_status"], ("PASS", "CLEAN"))

    def test_tier_is_forced_to_P_on_insert(self):
        sid = screen.insert_extracted(
            self.conn, a_row(verification_tier="V2"), source_collected_id=self.lead())
        self.assertEqual(screen.get_extracted(self.conn, sid)["verification_tier"], "P")


# --------------------------------------------------------------------------- #
class TestQualityAndQueue(Base):
    def test_early_delivery_counts_as_measurable_slip(self):
        """The quality panel's own bug: `slip >= 0` discarded it."""
        screen.insert_extracted(self.conn, a_row(
            announced="2025-01", promised_first_output="2026-07",
            actual_first_output="2026-03", current_status="PRODUCING"),
            source_collected_id=self.lead())
        bars = {b["key"]: b for b in quality.measure(self.conn)["bars"]}
        self.assertEqual(bars["slip"]["n"], 1)

    def test_failing_rows_are_blocked_not_ready(self):
        ok = screen.insert_extracted(self.conn, a_row(), source_collected_id=self.lead())
        bad = screen.insert_extracted(
            self.conn, a_row(project="Bad", promised_date_source="not-a-url"),
            source_collected_id=self.lead())
        screen.run_check(self.conn, ok)
        screen.run_check(self.conn, bad)
        q = screen.review_queue(self.conn)
        self.assertEqual([r["id"] for r in q["blocked"]], [bad])
        self.assertIn(ok, [r["id"] for r in q["ready"]])

    def test_flag_split_separates_access_from_substance(self):
        self.assertEqual(quality.classify_flag("status_source returned HTTP 403"),
                         "provenance")
        self.assertEqual(quality.classify_flag("the sources disagree on the date"),
                         "substantive")
        self.assertIsNone(quality.classify_flag(""))


# --------------------------------------------------------------------------- #
class TestFirstOutputBackfill(Base):
    """screen-date -- the narrow writer for the 22 produced-but-undated rows.

    It exists because the only other way to change a stored row is
    `screen-add --replace`, which takes the whole row. These rows are correct
    everywhere except one cell, so the risk being tested is collateral damage:
    a writer that repairs one cell and quietly moves another.
    """

    def undated(self, **over) -> int:
        """A row in the backfill population: produced, no date, -4.0."""
        sid = self.lead()
        rid = screen.insert_extracted(self.conn, a_row(
            actual_first_output="unconfirmed",
            current_status="IN FULL OPERATION",
            flag="status source confirms operation but states no first-output date.",
            **over), source_collected_id=sid)
        self.assertEqual(screen.get_extracted(self.conn, rid)["lag_years"],
                         dates.PRODUCED_UNDATED)
        return rid

    def test_a_date_produces_lag_and_slip(self):
        rid = self.undated()
        screen.set_first_output(self.conn, rid, date="2024-09",
                                source="https://example.com/first-coil",
                                raw="produced its first coil in September 2024")
        row = screen.get_extracted(self.conn, rid)
        self.assertEqual(row["actual_first_output"], "2024-09")
        self.assertEqual(row["actual_date_source"], "https://example.com/first-coil")
        self.assertEqual(row["actual_first_output_dt"], "2024-09-15")
        self.assertGreater(row["lag_years"], 0)
        self.assertGreater(row["slip_years"], 0)

    def test_no_other_cell_moves(self):
        """The whole argument for a narrow writer. Everything outside the
        actual-side date cells must be byte-identical afterwards."""
        rid = self.undated()
        before = dict(screen.get_extracted(self.conn, rid))
        screen.set_first_output(self.conn, rid, date="2024-09",
                                source="https://example.com/x")
        after = dict(screen.get_extracted(self.conn, rid))
        expected = {"actual_first_output", "actual_first_output_raw",
                    "actual_first_output_dt", "actual_date_source",
                    "lag_years", "slip_years", "flag"}
        moved = {k for k in before if before[k] != after[k]}
        self.assertEqual(moved, expected, f"unexpected cells changed: {moved - expected}")

    def test_the_old_flag_survives(self):
        """The extractor's note explains WHY the date was missing. Overwriting
        it to record the fix would delete the evidence the fix was needed."""
        rid = self.undated()
        before = screen.get_extracted(self.conn, rid)["flag"]
        screen.set_first_output(self.conn, rid, date="2024", source="https://e.com/x")
        after = screen.get_extracted(self.conn, rid)["flag"]
        self.assertIn(before, after)
        self.assertIn("Resolved", after)

    def test_a_sentinel_is_not_a_date(self):
        """Writing 'unconfirmed' here would record 'we found the date' while
        leaving the row undated -- the exact state being repaired."""
        rid = self.undated()
        for token in ("unconfirmed", "pending", "tbd", "n/a"):
            with self.assertRaises(ValueError):
                screen.set_first_output(self.conn, rid, date=token,
                                        source="https://example.com/x")

    def test_a_date_needs_a_url(self):
        rid = self.undated()
        with self.assertRaises(ValueError):
            screen.set_first_output(self.conn, rid, date="2024",
                                    source="the company press release")
        with self.assertRaises(ValueError):
            screen.set_first_output(self.conn, rid, date="2024", source="")

    def test_an_existing_date_is_protected(self):
        """Every row in the population is undated, so landing on a dated one
        means the id is wrong -- and the stored date is real research data."""
        sid = self.lead()
        rid = screen.insert_extracted(self.conn, a_row(actual_first_output="2023-05"),
                                      source_collected_id=sid)
        with self.assertRaises(screen.DateOverwriteBlocked):
            screen.set_first_output(self.conn, rid, date="2024",
                                    source="https://example.com/x")
        self.assertEqual(screen.get_extracted(self.conn, rid)["actual_first_output"],
                         "2023-05")
        screen.set_first_output(self.conn, rid, date="2024",
                                source="https://example.com/x", force=True)
        self.assertEqual(screen.get_extracted(self.conn, rid)["actual_first_output"],
                         "2024")

    def test_unresolved_leaves_the_dates_alone(self):
        rid = self.undated()
        screen.mark_first_output_unresolved(self.conn, rid, "searched 2022-2024, nothing dates it")
        row = screen.get_extracted(self.conn, rid)
        self.assertEqual(row["actual_first_output"], "unconfirmed")
        self.assertEqual(row["lag_years"], dates.PRODUCED_UNDATED)
        self.assertIn(screen.UNRESOLVED_MARKER, row["flag"])

    def test_the_queue_drops_what_is_done(self):
        """Both exits must remove a row from the queue, or the loop pays for
        the same dead end on every run. Only RETRY_UNRESOLVED brings it back."""
        resolved = self.undated(project="Resolved Fab")
        searched = self.undated(project="Searched Fab")
        untouched = self.undated(project="Untouched Fab")

        screen.set_first_output(self.conn, resolved, date="2024",
                                source="https://example.com/x")
        screen.mark_first_output_unresolved(self.conn, searched, "nothing dates it")

        ids = [r["id"] for r in screen.undated_produced(self.conn)]
        self.assertEqual(ids, [untouched])
        with_searched = [r["id"] for r in screen.undated_produced(self.conn,
                                                                 include_searched=True)]
        self.assertCountEqual(with_searched, [searched, untouched])

    def test_a_published_row_is_frozen(self):
        """verify_verified holds a COPY of the cells, not a live reference. A
        Screen write under a published row fixes the staging table and leaves
        the published Scoreboard reading 'unconfirmed'. Two of the twenty-two
        were published before the backfill was ever run."""
        rid = self.undated()
        screen.run_check(self.conn, rid)
        verify.promote(self.conn, rid, verification_tier="V1", flag="Resolved: checked.")
        with self.assertRaises(screen.RemovalBlocked):
            screen.set_first_output(self.conn, rid, date="2024",
                                    source="https://example.com/x")
        with self.assertRaises(screen.RemovalBlocked):
            screen.mark_first_output_unresolved(self.conn, rid, "nothing dates it")
        self.assertEqual(screen.get_extracted(self.conn, rid)["actual_first_output"],
                         "unconfirmed")

    def test_published_rows_leave_the_queue_and_are_reported(self):
        """The loop must not pay for a search it cannot record -- but the row
        still has to be surfaced, because 'unconfirmed' in verify_verified is
        on the published Scoreboard."""
        published = self.undated(project="Published Fab")
        open_row = self.undated(project="Open Fab")
        screen.run_check(self.conn, published)
        verify.promote(self.conn, published, verification_tier="V1", flag="Resolved: checked.")

        self.assertEqual([r["id"] for r in screen.undated_produced(self.conn)],
                         [open_row])
        self.assertEqual(
            [r["id"] for r in screen.undated_produced(self.conn, include_searched=True)],
            [open_row], "published rows stay out even under RETRY_UNRESOLVED")
        pub = screen.published_undated(self.conn)
        self.assertEqual(len(pub), 1)
        self.assertEqual(pub[0][0]["project"], "Published Fab")

    def test_a_fixed_verify_row_stops_being_reported(self):
        """published_undated keys on the Verify row, not the Screen row it came
        from. Verify holds a copy, so verify-edit leaves the Screen row at -4.0
        forever -- and keying on Screen kept naming rows already fixed. TSMC
        Fab 1 was reported as outstanding while verify #8 held a 2024-Q4 date."""
        rid = self.undated(project="Fixed Fab")
        screen.run_check(self.conn, rid)
        vid = verify.promote(self.conn, rid, verification_tier="V1",
                             flag="Resolved: checked.")
        self.assertEqual(len(screen.published_undated(self.conn)), 1)

        verify.edit(self.conn, vid, {"actual_first_output": "2024-Q4"},
                    edit_description="dated from a source")
        self.assertEqual(screen.published_undated(self.conn), [],
                         "a fixed published row must drop out of the report")
        self.assertEqual(screen.get_extracted(self.conn, rid)["lag_years"],
                         dates.PRODUCED_UNDATED,
                         "the Screen row stays -4.0; that is why Verify is the key")

    def test_the_new_column_is_provenance_and_url_checked(self):
        """It joined the v0 shape rather than sitting outside it, so the
        checker must hold it to the same rule as the other source columns."""
        self.assertIn("actual_date_source", sc.PROVENANCE_COLUMNS)
        self.assertIn("actual_date_source", sc.V0_COLUMNS)
        bad = sc.check_row(a_row(actual_date_source="not a url"))
        self.assertEqual(bad["result_status"], "FAIL")
        ok = sc.check_row(a_row(actual_date_source="https://example.com/x"))
        self.assertNotEqual(ok["result_status"], "FAIL")
        # Empty stays legal: 90 of the 112 rows never need it.
        blank = sc.check_row(a_row(actual_date_source=""))
        self.assertNotEqual(blank["result_status"], "FAIL")


# --------------------------------------------------------------------------- #
class TestSizeFloor(Base):
    """The inclusion rule is an OR, and the checker used to enforce an AND.

    capital >= the capital floor OR jobs >= the jobs floor puts a row in scope,
    so either figure alone settles it. validate_row required BOTH cells to parse
    before it would look at the floor, so a project clearly over the jobs line
    failed for want of a dollar figure no source had printed. Two rows of the
    N=100 run went that way: ES Foundry Greenwood and Meyer Burger Goodyear.

    Every figure below is derived from the phase rather than written out,
    because these are tests of the OR, not of where the line sits. Hard-coded
    amounts made the whole class fail the day the floor was raised from
    $100M/200 to $1B/2,000 — the rule under test had not changed at all.
    """

    # Pinned to a NAMED phase, and every figure derived from it. Two reasons,
    # and the class needs both: pinning means the test does not change meaning
    # when ACTIVE does, and deriving means it does not need editing when a
    # threshold moves. What is under test is the shape of the rule -- either
    # figure alone settles it -- which holds at any threshold.
    PHASE = settings.PHASES["100M-or-200-jobs"]
    CAP = PHASE.capital_usd
    JOBS = PHASE.jobs

    def verdict(self, **over):
        return sc.check_row(a_row(**over), self.PHASE)["result_status"]

    def errors(self, **over):
        return [i for i in sc.check_row(a_row(**over), self.PHASE)["report"]
                if i["level"] == "ERROR"]

    def test_jobs_alone_carries_the_row(self):
        """ES Foundry, in the shape of the incident: comfortably over the jobs
        floor, and no capital figure in any source."""
        self.assertNotEqual(
            self.verdict(promised_capital_usd="",
                         promised_jobs=str(self.JOBS * 2)), "FAIL")

    def test_capital_alone_carries_the_row(self):
        self.assertNotEqual(
            self.verdict(promised_capital_usd=str(self.CAP * 2),
                         promised_jobs=""), "FAIL")

    def test_a_missing_figure_is_fatal_only_when_it_would_settle_the_floor(self):
        """Röhm and SunOpta are under the line with no capital figure, so
        nothing shows them in scope -- and the error has to say that rather than
        'required numeric cell is empty', which named the symptom."""
        errs = self.errors(promised_capital_usd="",
                           promised_jobs=str(self.JOBS // 4))
        self.assertTrue(errs)
        self.assertIn("size floor cannot be established", errs[0]["message"])

    def test_both_below_still_fails_on_the_inclusion_rule(self):
        errs = self.errors(promised_capital_usd=str(self.CAP // 100),
                           promised_jobs=str(self.JOBS // 100))
        self.assertTrue(any("inclusion rule fails" in e["message"] for e in errs))

    def test_an_unreadable_figure_is_still_an_error(self):
        """A cell holding something nobody can parse is a bad value, not a
        missing one, and a healthy jobs count does not excuse it."""
        errs = self.errors(promised_capital_usd="about $500 million",
                           promised_jobs=str(self.JOBS * 2))
        self.assertTrue(any(e["column"] == "promised_capital_usd" for e in errs))

    def test_exactly_at_the_floor_is_in_scope(self):
        """The rule reads '>=', not '>'. Worth pinning: a floor written as a
        constant is easy to move, and easy to move to a '>' while moving it."""
        self.assertNotEqual(
            self.verdict(promised_capital_usd=str(self.CAP),
                         promised_jobs=""), "FAIL")
        self.assertNotEqual(
            self.verdict(promised_capital_usd="",
                         promised_jobs=str(self.JOBS)), "FAIL")

    def test_both_empty_names_both_cells(self):
        cols = {e["column"] for e in self.errors(promised_capital_usd="",
                                                 promised_jobs="")}
        self.assertEqual(cols, {"promised_capital_usd", "promised_jobs"})


# --------------------------------------------------------------------------- #
class TestScreenPromptContract(unittest.TestCase):
    """The rendered Screen prompt must still carry the rules a row depends on.

    These are not tests of prose. Each string below is load-bearing: if the
    renderer stops including the operating prompt, or someone rewrites it
    without the section, the failure is silent and shows up as a Scoreboard with a
    fifth of its rows undated -- which is exactly the state the N=100 run left
    behind before this was folded in.
    """

    def setUp(self):
        from pipeline import llm
        self.prompt = llm.render_screen_prompt({
            "id": 1, "promise_source": "https://example.com/p",
            "status_source": "https://example.com/s",
            "promised_date_source": None, "summary": "a test lead"})

    def test_it_carries_the_first_output_search_rule(self):
        self.assertIn("When the two sources do not date first output", self.prompt)
        self.assertIn("actual_date_source", self.prompt)

    def test_it_carries_all_three_exits(self):
        """Finding a date, finding none, and the row where the question itself
        is wrong. Dropping the third is how a restart or a cancelled product
        line silently acquires a date that measures something else."""
        for exit_text in ("You found a dated source",
                          "no source dates it",
                          "The question is wrong for this row"):
            self.assertIn(exit_text, self.prompt)

    def test_it_says_an_empty_capital_figure_is_legal(self):
        """Paired with the size-floor fix: either figure alone puts a row in
        scope, so the extractor must not reach for a number no source states."""
        self.assertIn("Either figure alone settles it", self.prompt)

    def test_the_search_is_scoped_to_one_cell(self):
        self.assertIn("Do **not** search for", self.prompt)


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
class TestSentinelVocabulary(unittest.TestCase):
    """One word per fact, and the prompt teaches only words the parser knows.

    The prompt offered six sentinels -- pending, never, unconfirmed, n/a, tbd,
    open -- for three behaviours the parser actually distinguishes, so four of
    them were undocumented synonyms. The model picked from the list by feel and
    put `unconfirmed` in `promised_first_output` 40 times out of the 44 projects
    with no promised date, which is the one slot dates.py says it cannot mean:
    there it reads as "produced but undated", about a promise that was never an
    event. Nothing computed a wrong number, because a promise with no date is
    "no promise recorded" either way -- but 40 published rows asserted something
    their own source did not.
    """

    def setUp(self):
        from pipeline import llm
        self.prompt = llm.render_screen_prompt({
            "id": 1, "promise_source": "https://example.com/p",
            "status_source": "https://example.com/s",
            "promised_date_source": None, "summary": "a test lead"})

    def test_each_sentinel_resolves_to_the_fact_the_prompt_claims(self):
        """The prompt is a contract with dates.py. A token taught here that the
        parser reads differently is worse than no token at all."""
        from pipeline import dates
        for token, kind in (("n/a", "to_be_completed"),      # no promise stated
                            ("pending", "to_be_completed"),  # not produced yet
                            ("never", "cancelled"),
                            ("unconfirmed", "produced_undated")):
            iso, got = dates.interpret_date(token)
            self.assertIsNone(iso, token)
            self.assertEqual(kind, got, token)

    def test_it_defines_all_four_and_names_the_field_each_belongs_to(self):
        self.assertIn("The four sentinels", self.prompt)
        for token in ("`n/a`", "`pending`", "`never`", "`unconfirmed`"):
            self.assertIn(token, self.prompt)

    def test_it_forbids_unconfirmed_in_the_promised_slot(self):
        """The actual defect, pinned. 40 of 173 projects carry it."""
        self.assertIn("Never `unconfirmed` here", self.prompt)
        self.assertIn("cannot** appear in `promised_first_output`", self.prompt)

    def test_it_no_longer_offers_the_synonyms(self):
        """tbd and open may still be parsed, for rows already written, but the
        prompt must stop presenting them as choices."""
        self.assertNotIn("`pending`, `never`, `unconfirmed`, `n/a`, `tbd`, `open`",
                         self.prompt)

    def test_the_parser_still_reads_the_retired_tokens(self):
        """Narrowing what is taught must not change what already parses: 4 rows
        hold `tbd` and rerunning the checker on them has to give the same
        answer it gave the day they were written."""
        from pipeline import dates
        for token in ("tbd", "open", "unknown"):
            self.assertEqual((None, "to_be_completed"), dates.interpret_date(token))

    # ---- and the checker enforces it, so the prompt is not the only guard ---- #

    def _check(self, **over):
        return sc.check_row(a_row(**over))

    def _messages(self, col, **over):
        return [i["message"] for i in self._check(**over)["report"] if i["column"] == col]

    def test_unconfirmed_in_the_promised_slot_fails_the_row(self):
        """The defect this rule exists for. It is an ERROR and not a warning
        because docs/schema.md defines ERROR as a value outside a closed
        vocabulary, and the promised column's sentinel set is now closed."""
        res = self._check(promised_first_output="unconfirmed")
        self.assertEqual("FAIL", res["result_status"])
        self.assertTrue(any("cannot be true of a promise" in m
                            for m in self._messages("promised_first_output",
                                                    promised_first_output="unconfirmed")))

    def test_n_a_in_the_promised_slot_is_clean(self):
        self.assertEqual([], self._messages("promised_first_output",
                                            promised_first_output="n/a"))

    def test_the_retired_synonyms_no_longer_pass_the_promised_slot(self):
        for token in ("tbd", "open", "pending", "never"):
            with self.subTest(token=token):
                self.assertEqual("FAIL",
                                 self._check(promised_first_output=token)["result_status"])

    def test_the_actual_slot_keeps_its_own_three(self):
        """Narrowing one column must not narrow the other: 121 stored projects
        carry one of these."""
        for token in ("pending", "never", "unconfirmed"):
            with self.subTest(token=token):
                self.assertEqual([], self._messages("actual_first_output",
                                                    actual_first_output=token))

    def test_a_real_date_wins_over_a_stray_sentinel_word(self):
        """A qualifier is not a sentinel. "2019 (pending permits)" is a dated
        promise and has to stay promotable in either column."""
        self.assertEqual([], self._messages("promised_first_output",
                                            promised_first_output="2019 (pending permits)"))

    def test_an_empty_cell_is_told_which_sentinel_its_own_column_takes(self):
        """The message used to suggest 'pending'/'never' in both columns, which
        in the promised slot recommended exactly what is now refused."""
        promised = self._messages("promised_first_output", promised_first_output="")
        self.assertTrue(any("'n/a'" in m for m in promised), promised)
        self.assertFalse(any("'pending'" in m for m in promised), promised)

    def test_the_flag_rule_does_not_ban_the_sentinel_it_now_requires(self):
        """"Do not write n/a" was written about free-text cells. Unqualified, it
        contradicts the promised_first_output rule directly above it."""
        self.assertIn("`None`, `null`, or `n/a` into `flag` or any other free-text cell",
                      self.prompt)


# --------------------------------------------------------------------------- #
class TestVerbatimQuotesAtVerify(Base):
    """A human at the Verify gate must be able to correct the *_raw cells.

    EDITABLE_COLUMNS was derived from V0_COLUMNS alone, and the *_raw columns
    are a pipeline-stage addition outside the v0 shape -- so they were locked,
    not by decision but by inheritance. The result was that correcting a date
    left the one field claiming to be its source text saying something else.
    Verify #8 (TSMC Fab 1) read actual_first_output = '2024-Q4' with
    actual_first_output_raw = 'unconfirmed'.
    """

    def published(self, **over) -> int:
        sid = self.lead()
        rid = screen.insert_extracted(self.conn, a_row(
            actual_first_output="unconfirmed",
            actual_first_output_raw="unconfirmed",
            current_status="IN FULL OPERATION", **over), source_collected_id=sid)
        screen.run_check(self.conn, rid)
        return verify.promote(self.conn, rid, verification_tier="V1",
                              flag="Resolved: checked.")

    def test_a_raw_cell_can_be_corrected(self):
        vid = self.published()
        verify.edit(self.conn, vid,
                    {"actual_first_output": "2024-Q4",
                     "actual_first_output_raw": "began high-volume production in Q4 2024"},
                    edit_description="dated from the company's own page")
        row = verify.get_verified(self.conn, vid)
        self.assertEqual(row["actual_first_output"], "2024-Q4")
        self.assertEqual(row["actual_first_output_raw"],
                         "began high-volume production in Q4 2024")
        self.assertEqual(row["actual_first_output_dt"], "2024-11-15")

    def test_a_token_moved_without_its_quote_is_reported(self):
        """Not refused -- both readings are legitimate. The quote may have been
        misread (raw right, token wrong) or the source may have changed (raw
        needs replacing), and only the editor knows which."""
        vid = self.published()
        notices = verify.edit(self.conn, vid, {"actual_first_output": "2024-Q4"},
                              edit_description="dated")
        self.assertEqual(len(notices), 1)
        self.assertIn("actual_first_output_raw", notices[0])
        self.assertIn("unconfirmed", notices[0])
        # The edit still lands: this is a notice, not a veto.
        self.assertEqual(
            verify.get_verified(self.conn, vid)["actual_first_output"], "2024-Q4")

    def test_no_notice_when_both_move_together(self):
        vid = self.published()
        notices = verify.edit(self.conn, vid,
                              {"actual_first_output": "2024-Q4",
                               "actual_first_output_raw": "production began in Q4 2024"},
                              edit_description="dated")
        self.assertEqual(notices, [])

    def test_derived_cells_are_still_locked(self):
        """The *_dt and lag/slip cells are COMPUTED. Opening *_raw must not have
        opened those -- they would drift from the strings they summarise."""
        vid = self.published()
        for col in ("lag_years", "slip_years", "actual_first_output_dt"):
            with self.assertRaises(ValueError):
                verify.edit(self.conn, vid, {col: "3"}, edit_description="x")


# --------------------------------------------------------------------------- #
class TestCriteria(Base):
    """The inclusion rules are one setting, and rows remember which one made them.

    The Scoreboard is built by sweeping at a high threshold and lowering it in a
    later phase. Without a stamp on each row, the second sweep is
    indistinguishable from the first and "no $300M plants in 2019" cannot be told
    apart from "we were not looking for $300M plants in 2019".
    """

    def test_a_row_is_judged_by_the_phase_that_admitted_it(self):
        """The one that matters. A row admitted under a loose phase must not be
        re-graded when a tighter one becomes active, or lowering a threshold
        would silently invalidate everything collected above it."""
        small = a_row(promised_capital_usd=300_000_000, promised_jobs=500)
        self.assertEqual(sc.check_row(small, settings.PHASES["100M-or-200-jobs"])["result_status"], "CLEAN")
        self.assertEqual(sc.check_row(small, settings.PHASES["1B-or-2000-jobs"])["result_status"], "FAIL")

    def test_stored_rows_carry_their_phase(self):
        sid = self.lead()
        rid = screen.insert_extracted(self.conn, a_row(), source_collected_id=sid)
        row = screen.get_extracted(self.conn, rid)
        self.assertEqual(row["criteria_id"], settings.active().id)
        self.assertEqual(
            self.conn.execute("SELECT criteria_id FROM source_collected WHERE id=?",
                              (sid,)).fetchone()[0], settings.active().id)

    def test_the_phase_is_forced_not_taken_from_the_extractor(self):
        """An extractor reports on a project, not on which sweep it belongs to.
        A row that could name its own phase could misreport the sampling frame."""
        sid = self.lead()
        rid = screen.insert_extracted(self.conn, a_row(criteria_id="1B-or-2000-jobs"),
                                      source_collected_id=sid)
        self.assertEqual(screen.get_extracted(self.conn, rid)["criteria_id"],
                         settings.active().id)

    def test_country_defaults_to_the_phase(self):
        sid = self.lead()
        rid = screen.insert_extracted(self.conn, a_row(), source_collected_id=sid)
        self.assertEqual(screen.get_extracted(self.conn, rid)["country"], "US")

    def test_the_announced_window_is_enforced(self):
        """README stated "January 2017 or later" for the whole life of the
        project and no code checked it."""
        early = sc.check_row(a_row(announced="2015-06"))
        self.assertEqual(early["result_status"], "FAIL")
        self.assertTrue(any("before" in i["message"] for i in early["report"]))

    def test_where_is_enforced_against_the_phase(self):
        self.assertEqual(sc.check_row(a_row(state="ON"))["result_status"], "FAIL")
        self.assertEqual(sc.check_row(a_row(country="CA"))["result_status"], "FAIL")
        self.assertNotEqual(sc.check_row(a_row(country="US"))["result_status"], "FAIL")

    def test_or_and_and_differ(self):
        both = settings.Criteria(id="t", capital_usd=1_000_000_000, jobs=2_000,
                                 op="AND", announced_from="2017-01", countries=("US",))
        either = settings.Criteria(id="t", capital_usd=1_000_000_000, jobs=2_000,
                                   op="OR", announced_from="2017-01", countries=("US",))
        self.assertTrue(either.clears(5_000_000_000, 100))
        self.assertFalse(both.clears(5_000_000_000, 100))
        # Under AND a missing figure can never settle it; under OR it can.
        self.assertTrue(either.clears(5_000_000_000, None))
        self.assertFalse(both.clears(5_000_000_000, None))

    def test_an_unknown_phase_is_refused_loudly(self):
        import os
        os.environ["CRITERIA"] = "nope"
        try:
            with self.assertRaises(SystemExit):
                settings.active()
        finally:
            del os.environ["CRITERIA"]

    def test_the_sector_registry_is_gone(self):
        """A vocabulary that could change at runtime could move what counts as
        in scope mid-run with nothing in git recording it. Adding a sector is a
        commit now."""
        self.assertFalse(hasattr(sc, "register_sector"))
        self.assertFalse((Path(__file__).resolve().parent.parent
                          / "pipeline" / "sector_registry.json").exists())


# --------------------------------------------------------------------------- #
class TestLoopSettings(unittest.TestCase):
    """One definition per loop setting, shared by both shell scripts.

    Each of these lived twice, once in source.sh and once in all.sh, because
    all.sh launches source.sh with an explicit environment and had to supply a
    value for every knob. That is this repository's most repeated bug and it had
    already happened here: all.sh's flat MAX_ITERS of 200 shadowed source.sh's
    scaling default, so a run asking for 300 rows capped at 200 in silence.
    """

    def tearDown(self):
        for k in ("MAX_STALL", "SOURCE_MAX_STALL", "LEADS_PER_CALL", "VERBOSE"):
            os.environ.pop(k, None)

    def test_max_iters_scales_and_has_a_floor(self):
        self.assertEqual(settings.max_iters(100), 300)
        self.assertEqual(settings.max_iters(400), 1200)
        self.assertEqual(settings.max_iters(5), settings.MIN_ITERS)

    def test_a_bad_row_target_cannot_cap_the_loop_at_zero(self):
        """BSD `seq 1 0` counts DOWN, so a cap of 0 would run two turns with
        i=1 then i=0 rather than none."""
        for bad in ("", "abc", None, "-4"):
            self.assertGreaterEqual(settings.max_iters(bad), settings.MIN_ITERS)

    def test_the_global_override_wins_over_the_default(self):
        os.environ["MAX_STALL"] = "9"
        self.assertEqual(settings.max_stall(), 9)

    def test_a_stage_override_wins_over_the_global(self):
        os.environ["MAX_STALL"] = "9"
        os.environ["SOURCE_MAX_STALL"] = "7"
        self.assertEqual(settings.max_stall("SOURCE"), 7)
        self.assertEqual(settings.max_stall("SCREEN"), 9, "another stage still sees the global")

    def test_it_reports_what_decided_each_value(self):
        self.assertEqual(settings.run_source("MAX_STALL"), "settings.py")
        os.environ["MAX_STALL"] = "9"
        self.assertEqual(settings.run_source("MAX_STALL"), "$MAX_STALL")
        os.environ["SOURCE_MAX_STALL"] = "7"
        self.assertEqual(settings.run_source("MAX_STALL", "SOURCE"), "$SOURCE_MAX_STALL")

    def test_the_two_methodological_settings_are_present(self):
        """leads_per_call and max_stall decide what a run MEANS, so both must be
        reportable -- they belong in the run header and the write-up."""
        eff = settings.run_in_effect()
        self.assertIn("leads_per_call", eff)
        self.assertIn("max_stall", eff)


# --------------------------------------------------------------------------- #
class TestPathReferences(unittest.TestCase):
    """Every path-shaped token in the repo's code and prose names a real file.

    Three commits in a row left references to files that had been deleted or
    moved -- 33 the first time, then 3 more that a grep filter hid, then
    `collect.sh` in a doc. Each was found by hand. This is the mechanism that
    finds the next one the moment it lands.
    """
    ROOT = Path(__file__).resolve().parent.parent
    SKIP_DIRS = {".git", "logs", "scratch", "outputs", "__pycache__", ".venv", "venv"}
    TOKEN = re.compile(
        r"(?<![\w/])((?:\.\./)*(?:pipeline|collect|tools|webapp|tests|docs)"
        r"/[A-Za-z0-9_./-]+?\.(?:py|sh|md|json|txt))\b")

    def test_every_referenced_path_exists(self):
        bad = []
        for path in self.ROOT.rglob("*"):
            if path.suffix not in (".py", ".sh", ".md") or not path.is_file():
                continue
            if any(part in self.SKIP_DIRS for part in path.relative_to(self.ROOT).parts):
                continue
            for m in self.TOKEN.finditer(path.read_text(errors="ignore")):
                tok = m.group(1)
                base = path.parent if tok.startswith("../") else self.ROOT
                if not (base / tok).exists():
                    bad.append(f"{path.relative_to(self.ROOT)}: {tok}")
        self.assertEqual(bad, [], "references to files that do not exist:\n  " + "\n  ".join(bad))


class TestConfigCitations(unittest.TestCase):
    """`config` cites a line for every value, and the lines are real."""

    def tearDown(self):
        for k in ("MAX_ITERS", "SOURCE_MAX_ITERS"):
            os.environ.pop(k, None)

    def test_every_constant_config_cites_resolves(self):
        for name in ("ACTIVE", "SECTORS", "SOURCE", "SCREEN", "API", "AGENT",
                     "EFFORT", "LEADS_PER_CALL", "MAX_STALL", "VERBOSE", "ITERS_PER_ROW"):
            self.assertRegex(settings.where(name), r"^settings\.py:\d+$", name)

    def test_the_thresholds_have_a_line(self):
        """The values a person actually turns are inside PHASES, which a regex on
        `^NAME =` could never reach -- so config used to print '<- the phase'
        for exactly the rows its docstring said mattered most."""
        for phase in settings.PHASES:
            self.assertRegex(settings.where_phase(phase), r"^settings\.py:\d+$", phase)

    def test_an_unknown_name_fails_loudly(self):
        with self.assertRaises(KeyError):
            settings.where("NO_SUCH_CONSTANT")

    def test_a_max_iters_override_is_visible(self):
        """config printed the formula while the shell honoured $MAX_ITERS -- the
        shadowing incident this file exists to expose, hidden by the tool built
        to show it."""
        os.environ["MAX_ITERS"] = "500"
        v, src = settings.run_in_effect(None, 100)["max_iters"]
        self.assertEqual((v, src), (500, "$MAX_ITERS"))
        os.environ["SOURCE_MAX_ITERS"] = "7"
        self.assertEqual(settings.run_in_effect("SOURCE", 100)["max_iters"], (7, "$SOURCE_MAX_ITERS"))


class TestImportTargets(unittest.TestCase):
    """Every `from pipeline.x import name` in the repo names something that exists.

    Eleven of these imports sit inside function bodies, so nothing runs them at
    import time: the suite passed while `scoreboard.py collect` -- the entry
    point -- raised ImportError on a name the settings merge had removed from
    db.py. This reads the source instead of waiting for the call. Targets are
    limited to `pipeline` so the check never imports the web app.
    """
    ROOT = Path(__file__).resolve().parent.parent
    SKIP_DIRS = {".git", "old", "scratch", "outputs", "logs", "__pycache__", ".venv", "venv"}

    def test_every_imported_pipeline_name_exists(self):
        import ast
        import importlib
        bad = []
        for path in sorted(self.ROOT.rglob("*.py")):
            rel = path.relative_to(self.ROOT)
            if any(part in self.SKIP_DIRS for part in rel.parts):
                continue
            for node in ast.walk(ast.parse(path.read_text(errors="ignore"))):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                if node.module.split(".")[0] != "pipeline":
                    continue
                try:
                    mod = importlib.import_module(node.module)
                except ImportError as e:
                    bad.append(f"{rel}:{node.lineno}: {node.module} ({e})")
                    continue
                for alias in node.names:
                    if alias.name == "*" or hasattr(mod, alias.name):
                        continue
                    try:  # `from pipeline import coverage` names a submodule
                        importlib.import_module(f"{node.module}.{alias.name}")
                    except ImportError:
                        bad.append(f"{rel}:{node.lineno}: {node.module} has no {alias.name}")
        self.assertEqual(bad, [], "imports of names that do not exist:\n  " + "\n  ".join(bad))


class TestCollectEntryPoint(unittest.TestCase):
    """`scoreboard.py collect --dry-run` runs end to end.

    The CLI hands off to collect/all.sh, which calls back into the CLI for the
    model, effort and run settings. Two regressions in one week broke that
    handoff while every unit test passed, because nothing ran the entry point.
    Runs against an empty temporary database with the log and the auth probe off.
    """

    def test_dry_run_plans_a_source_stage(self):
        import subprocess
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, SCOREBOARD_DB=str(Path(tmp) / "empty.db"), LOG="0", PREFLIGHT="0")
            env.pop("MEDALLION_DB", None)
            r = subprocess.run(
                [sys.executable, "scoreboard.py", "collect", "--n", "1", "--only", "source", "--dry-run"],
                cwd=root, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"model=claude-\S+ effort=\S+ max_iters=\d+ max_stall=\d+")


class TestCheckVerdicts(Base):
    """CLEAN > PASS > FAIL, and only FAIL blocks the gate.

    The names do not sort that way. Three places in this repository once said a
    PASS row was "not yet publishable" and told a person to clear the warning
    before promoting, while both gates promoted PASS and no command anywhere
    edits a flag on a Screen row. This pins the ordering and the gate together
    so the prose cannot drift away from the behaviour again.
    """

    def _verdict(self, **over) -> str:
        lead = self.lead(promise_source=f"https://example.com/{over.pop('_n', 1)}")
        rid = screen.insert_extracted(self.conn, a_row(**over), source_collected_id=lead)
        screen.run_check(self.conn, rid)
        return rid, verify.latest_check(self.conn, rid)["result_status"]

    def test_no_issues_is_clean(self):
        _, v = self._verdict(_n=1)
        self.assertEqual(v, "CLEAN")

    def test_open_flag_is_pass_not_fail(self):
        _, v = self._verdict(_n=2, flag="announcement states no production-start date")
        self.assertEqual(v, "PASS")

    def test_clearing_neither_floor_is_fail(self):
        _, v = self._verdict(_n=3, promised_capital_usd=700_000_000, promised_jobs=400)
        self.assertEqual(v, "FAIL")

    def test_pass_is_promotable_and_promotion_resolves_the_flag(self):
        """The reason PASS must not block: promotion is the only thing that can
        write a flag, so 'clear the warning first' names a step that does not
        exist."""
        rid, v = self._verdict(_n=4, flag="first output not yet confirmed")
        self.assertEqual(v, "PASS")
        vid = verify.promote(self.conn, rid, verification_tier="V1")
        got = self.conn.execute(
            "SELECT flag FROM verify_verified WHERE id = ?", (vid,)).fetchone()["flag"]
        self.assertTrue(got.lower().startswith("resolved"), got)
        self.assertIn("first output not yet confirmed", got)

    def test_fail_blocks_promotion_but_force_gets_through(self):
        rid, v = self._verdict(_n=5, promised_capital_usd=700_000_000, promised_jobs=400)
        self.assertEqual(v, "FAIL")
        with self.assertRaises(verify.PromotionBlocked):
            verify.promote(self.conn, rid, verification_tier="V1")
        self.assertTrue(verify.promote(self.conn, rid, verification_tier="V1", force=True))

    def test_no_flag_editing_command_exists_at_screen(self):
        """If one is ever added, the docs claiming promotion is the only writer
        become wrong and this test should be the thing that says so."""
        import subprocess
        out = subprocess.run(
            [sys.executable, "scoreboard.py", "--help"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, timeout=120).stdout
        self.assertNotIn("screen-flag", out)

    def test_prose_does_not_call_a_pass_unpublishable(self):
        """The retired claim, in any file. It contradicted both gates."""
        root = Path(__file__).resolve().parent.parent
        skip = {".git", "logs", "outputs", "scratch", "__pycache__", ".venv", "venv", "old"}
        retired = re.compile(
            r"clear warnings before promoting"
            r"|PASS[^.\n]{0,40}not yet publishable"
            r"|only WARNs\s*->\s*PASS\s*\(admissible, not yet publishable\)",
            re.IGNORECASE)
        bad = []
        for f in root.rglob("*"):
            if f.suffix not in (".py", ".md", ".sh") or not f.is_file():
                continue
            if any(part in skip for part in f.relative_to(root).parts):
                continue
            if f.name == Path(__file__).name:
                continue
            for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                if retired.search(line):
                    bad.append(f"{f.relative_to(root)}:{i}: {line.strip()[:80]}")
        self.assertEqual(bad, [], "a PASS row is promotable; this text says otherwise:\n  "
                                 + "\n  ".join(bad))


class TestConfig(unittest.TestCase):
    """Facts written down twice eventually disagree with themselves."""

    def tearDown(self):
        for k in ("MODEL", "SCREEN_MODEL", "SOURCE_MODEL"):
            os.environ.pop(k, None)

    def test_export_goes_beside_its_own_database(self):
        """A variable source and a fixed destination: exporting a scratch
        database overwrote the real database's CSVs."""
        self.assertEqual(export_dir(db="/tmp/scratch/x.db"), Path("/tmp/scratch/csv_tables"))

    def test_out_dir_still_wins(self):
        self.assertEqual(export_dir(out_dir="/tmp/elsewhere", db="/tmp/x/y.db"),
                         Path("/tmp/elsewhere"))

    def test_model_precedence(self):
        self.assertEqual(settings.screen(), settings.SCREEN)
        os.environ["MODEL"] = "global-model"
        self.assertEqual(settings.screen(), "global-model")
        os.environ["SCREEN_MODEL"] = "stage-model"
        self.assertEqual(settings.screen(), "stage-model")
        self.assertEqual(settings.source(), "global-model")


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------- #
class TestAttestation(Base):
    """The human gate, stored.

    Before this table the interface kept a checklist in sessionStorage, so what
    a person had confirmed vanished when the tab closed and the published claim
    could only ever be "a human looked at this project". These tests pin the
    three properties that make the stored version worth more than that: it is
    append-only, it notices when a confirmed value is edited afterwards, and it
    records what the cited page actually contained beside what the person said.
    """

    WHO = "ashwin@industriousaf.org"

    def setUp(self):
        super().setUp()
        self.sid = screen.insert_extracted(self.conn, a_row(),
                                           source_collected_id=self.lead())

    def test_the_table_exists_on_a_database_made_before_it_did(self):
        """init_db is additive: the DDL runs on every command, so a database
        created last week gets the table without a rebuild and keeps its rows."""
        self.conn.execute("DROP TABLE screen_attested")
        self.conn.commit()
        init_db(self.conn)
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("screen_attested", names)
        self.assertEqual(
            1, self.conn.execute("SELECT count(*) FROM screen_extracted").fetchone()[0])

    def test_changing_your_mind_keeps_both_rows(self):
        """Append-only, like screen_check and verify_edits. Overwriting would
        hide a reviewer reversing themselves, which is the one movement in this
        table a sceptical reader would most want to see."""
        screen.attest(self.conn, self.sid, "promised_jobs", "confirmed", self.WHO)
        screen.attest(self.conn, self.sid, "promised_jobs", "not_in_source", self.WHO)
        self.assertEqual(2, self.conn.execute(
            "SELECT count(*) FROM screen_attested").fetchone()[0])
        state = screen.attestation_state(self.conn, self.sid)
        self.assertEqual("not_in_source", state["promised_jobs"]["state"])

    def test_editing_a_confirmed_field_makes_it_stale(self):
        """A confirmation is about a value, not about a field. Edit the value
        and the interface has to ask again -- otherwise the tick stays on screen
        vouching for a number nobody ever read."""
        screen.attest(self.conn, self.sid, "promised_jobs", "confirmed", self.WHO)
        self.assertFalse(
            screen.attestation_state(self.conn, self.sid)["promised_jobs"]["stale"])
        self.conn.execute("UPDATE screen_extracted SET promised_jobs = 9999 WHERE id = ?",
                          (self.sid,))
        self.conn.commit()
        self.assertTrue(
            screen.attestation_state(self.conn, self.sid)["promised_jobs"]["stale"])

    def test_an_integer_field_read_back_is_not_reported_as_changed(self):
        """The staleness check compares strings, and `str(2000)` at the write
        end against `2000` at the read end would call every numeric field stale
        the moment the page reloaded."""
        screen.attest(self.conn, self.sid, "promised_capital_usd", "confirmed", self.WHO)
        self.assertFalse(screen.attestation_state(
            self.conn, self.sid)["promised_capital_usd"]["stale"])

    def test_it_stores_what_the_page_contained(self):
        """The count is the anti-gaming signal: a confirmation against a page
        holding zero hits is visible to anyone reading the table later."""
        screen.attest(self.conn, self.sid, "promised_jobs", "confirmed", self.WHO,
                      source_url="https://example.com/p", tab_index=0, match_count=0)
        row = self.conn.execute("SELECT * FROM screen_attested").fetchone()
        self.assertEqual(0, row["match_count"])
        self.assertEqual("https://example.com/p", row["source_url"])

    def test_it_refuses_a_field_that_is_not_on_the_checklist(self):
        with self.assertRaises(ValueError):
            screen.attest(self.conn, self.sid, "notes", "confirmed", self.WHO)

    def test_it_refuses_an_answer_that_is_not_one_of_the_two(self):
        with self.assertRaises(ValueError):
            screen.attest(self.conn, self.sid, "promised_jobs", "yes", self.WHO)

    def test_it_refuses_anyone_not_on_the_verifier_list(self):
        """The whole reason the addresses are a list and not a text box. A
        writer that accepted any string would make the identity in this column
        worth exactly as much as a typed name, which is nothing."""
        with self.assertRaises(ValueError):
            screen.attest(self.conn, self.sid, "promised_jobs", "confirmed",
                          "stranger@example.com")
        self.assertEqual(0, self.conn.execute(
            "SELECT count(*) FROM screen_attested").fetchone()[0])

    def test_the_export_carries_it(self):
        """scoreboard.db is committed and git cannot diff a binary, so the
        audit trail only reaches a reader through the CSVs."""
        from pipeline.export_tables import ALL_TABLES
        self.assertEqual("screen_attested", ALL_TABLES["screen_attested"])


# --------------------------------------------------------------------------- #
class TestVerifierList(unittest.TestCase):
    """The addresses are published beside the data, so they are askable.

    settings.py opens by saying to ask `config` what is in effect rather than
    reading the file. A list that decides whose name goes into public data, and
    that `config` does not mention, breaks that promise in the one place it
    matters most."""

    def test_config_names_who_may_verify(self):
        import subprocess
        out = subprocess.run(
            [sys.executable, "scoreboard.py", "config"],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True).stdout
        self.assertIn("WHO MAY VERIFY", out)
        for who in settings.verifiers():
            self.assertIn(who, out)

    def test_an_address_off_the_list_is_refused(self):
        self.assertFalse(settings.may_verify("stranger@example.com"))
        self.assertFalse(settings.may_verify(""))

    def test_surrounding_space_does_not_make_a_new_person(self):
        """The address arrives from a cookie and a form, and " a@b " matching
        nothing would be a confusing refusal; matching a second time under a
        different string would be worse."""
        self.assertTrue(settings.may_verify("  " + settings.VERIFIERS[0] + "  "))
