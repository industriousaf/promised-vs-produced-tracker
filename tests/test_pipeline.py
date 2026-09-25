"""Regression tests for the places where a mistake corrupts data silently.

Every test here is a real incident. The Tracker had no tests until 2026-09-05,
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
        "status": "under construction",
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
    def _published(self, **over):
        """One project, verified, which is where the findings are counted."""
        sid = screen.insert_extracted(self.conn, a_row(**over),
                                      source_collected_id=self.lead())
        screen.run_check(self.conn, sid)        # promotion is gated on a check
        verify.promote(self.conn, sid, verification_tier="V1")
        return sid

    def test_early_delivery_counts_as_measurable_slip(self):
        """The quality panel's own bug: `slip >= 0` discarded it."""
        self._published(announced="2025-01", promised_first_output="2026-07",
                        actual_first_output="2026-03", current_status="PRODUCING",
                        status="producing")
        self.assertEqual(quality.measure(self.conn)["findings"]["slip"]["n"], 1)

    def test_the_gates_are_a_funnel_in_pipeline_order(self):
        """Sourced, then checked, then verified. Read down, they say where the
        work is; read any other order they are five unrelated scores."""
        self.assertEqual([g["key"] for g in quality.measure(self.conn)["gates"]],
                         ["sourced", "clean", "publishable"])

    def test_the_findings_count_the_published_projects_only(self):
        """A project nobody has verified is not a finding yet, and the panel
        says the findings are the projects that cleared every gate."""
        screen.insert_extracted(self.conn, a_row(
            project="Unverified Fab", announced="2020-01",
            promised_first_output="2022-01", actual_first_output="2023-01",
            current_status="PRODUCING", status="producing"),
            source_collected_id=self.lead())
        fi = quality.measure(self.conn)["findings"]
        self.assertEqual((fi["published"], fi["lag"]["n"], fi["slip"]["n"]), (0, 0, 0))

    def test_what_cannot_be_measured_yet_is_counted_not_failed(self):
        """Three quarters of these plants have not opened. That is the finding,
        not a hole in the data, so the panel states it instead of scoring it."""
        self._published(project="Building", actual_first_output="pending")
        self._published(project="Cancelled", actual_first_output="never",
                        current_status="CANCELLED", status="cancelled")
        self._published(project="Producing, undated", actual_first_output="unconfirmed",
                        current_status="PRODUCING", status="producing")
        self._published(project="No promise", announced="2020-01",
                        promised_first_output="n/a", actual_first_output="2023-01",
                        current_status="PRODUCING", status="producing")
        fi = quality.measure(self.conn)["findings"]
        self.assertEqual(len(fi["pending"]), 1)
        self.assertEqual(len(fi["cancelled"]), 1)
        self.assertEqual(len(fi["undated"]), 1)
        # Produced without a promise: a lag, but nothing to slip against.
        self.assertEqual((fi["lag"]["n"], fi["slip"]["n"]), (1, 0))
        self.assertEqual(len(fi["no_promise"]), 1)

    def test_each_finding_says_what_it_is_measured_from(self):
        """A lag from the announcement is not the time-to-build of the
        literature, and a label alone cannot say which one it is."""
        fi = quality.measure(self.conn)["findings"]
        self.assertIn("announcement", fi["lag"]["tip"])
        self.assertIn("not from the start of construction", fi["lag"]["tip"])
        self.assertIn("Negative means early", fi["slip"]["tip"])

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

    def test_out_of_scope_is_separated_from_blocked(self):
        """Both FAIL and neither can be verified, but only one is work.

        A malformed row can be corrected. A row whose two size figures are both
        known and both under the floor is correctly reporting a project that
        does not belong in this phase, and no edit resolves it. Eleven rows were
        listed as blocked when three of them could never be unblocked, which
        sent a person looking for a defect that was not there."""
        malformed = screen.insert_extracted(
            self.conn, a_row(project="Malformed", promised_date_source="not-a-url"),
            source_collected_id=self.lead())
        small = screen.insert_extracted(
            self.conn, a_row(project="Small", promised_capital_usd=900_000_000,
                             promised_jobs=1100),
            source_collected_id=self.lead())
        self.assertEqual(screen.run_check(self.conn, malformed)["result_status"], "FAIL")
        self.assertEqual(screen.run_check(self.conn, small)["result_status"], "OUT_OF_SCOPE")

        q = screen.review_queue(self.conn)
        self.assertEqual([r["id"] for r in q["blocked"]], [malformed])
        self.assertEqual([r["id"] for r in q["out_of_scope"]], [small])

    def test_an_unestablished_floor_is_work_not_an_exclusion(self):
        """The other half of the same distinction. A row whose capital cell is
        EMPTY has not been measured, so it is blocked -- someone can go and find
        the figure (that is what size_source is for). Only a row where both
        figures are known is out of scope."""
        unknown = screen.insert_extracted(
            self.conn, a_row(project="Unknown", promised_capital_usd=None,
                             promised_jobs=600),
            source_collected_id=self.lead())
        self.assertEqual(screen.run_check(self.conn, unknown)["result_status"], "SIZE_UNKNOWN")
        q = screen.review_queue(self.conn)
        self.assertEqual([r["id"] for r in q["size_unknown"]], [unknown])
        self.assertEqual(q["blocked"], [], "nothing here is malformed")
        self.assertEqual(q["out_of_scope"], [])

    def test_an_out_of_scope_row_still_cannot_be_published(self):
        """Separating it from `blocked` is a reporting change and must not
        become a way in. The gate is unchanged: it is a FAIL, so promote
        refuses it."""
        small = screen.insert_extracted(
            self.conn, a_row(project="Small", promised_capital_usd=900_000_000,
                             promised_jobs=1100),
            source_collected_id=self.lead())
        self.assertEqual(screen.run_check(self.conn, small)["result_status"], "OUT_OF_SCOPE")
        with self.assertRaises(verify.PromotionBlocked):
            verify.promote(self.conn, small, verification_tier="V1")

    def test_the_reader_asks_the_stored_report_not_the_live_rule(self):
        """sc.is_out_of_scope reads the verdict the checker recorded. Deciding
        it again later would re-grade old rows against a threshold that has
        since moved -- the thing criteria_id exists to prevent."""
        self.assertTrue(sc.is_out_of_scope(
            [{"level": "ERROR", "message": f"{sc.OUT_OF_SCOPE}: phase 'x' requires ..."}]))
        self.assertFalse(sc.is_out_of_scope(
            [{"level": "ERROR", "message": "size floor cannot be established: ..."}]))
        self.assertFalse(sc.is_out_of_scope(
            [{"level": "WARN", "message": f"{sc.OUT_OF_SCOPE}: ..."}]))
        self.assertFalse(sc.is_out_of_scope([]))

    def test_ready_is_largest_capital_first_with_no_figure_last(self):
        """The order the Tracker's completeness claim rests on. The dashboard
        says "largest capital first, so wherever you stop, the Tracker above
        that point is complete", and every surface that offers a next project
        asks this for it -- so if this order is wrong, all of them are.

        Inserted smallest first, so id order and capital order disagree: the
        first thirty projects were verified in id order once already."""
        small = screen.insert_extracted(
            self.conn, a_row(project="Small", promised_capital_usd=1_200_000_000),
            source_collected_id=self.lead())
        big = screen.insert_extracted(
            self.conn, a_row(project="Big", promised_capital_usd=20_000_000_000),
            source_collected_id=self.lead())
        blank = screen.insert_extracted(
            self.conn, a_row(project="Blank", promised_capital_usd=None, promised_jobs=5000),
            source_collected_id=self.lead())
        for rid in (small, big, blank):
            screen.run_check(self.conn, rid)
        ready = [r["id"] for r in screen.review_queue(self.conn)["ready"]]
        self.assertEqual([big, small, blank], ready)

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
            current_status="IN FULL OPERATION", status="producing",
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
        rid = screen.insert_extracted(self.conn, a_row(actual_first_output="2023-05", status="producing"),
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
        the published Tracker reading 'unconfirmed'. Two of the twenty-two
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
        on the published Tracker."""
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

    def test_size_source_is_provenance_and_url_checked(self):
        """The same contract, for the column that cites a size figure found
        outside the lead's own links. It matters more here than anywhere else
        on the row: this is the cell the inclusion rule reads, so a value it
        carries is what admits a project to the Tracker."""
        self.assertIn("size_source", sc.PROVENANCE_COLUMNS)
        self.assertIn("size_source", sc.V0_COLUMNS)
        bad = sc.check_row(a_row(size_source="the trade press"))
        self.assertEqual(bad["result_status"], "FAIL")
        ok = sc.check_row(a_row(size_source="https://example.com/x"))
        self.assertNotEqual(ok["result_status"], "FAIL")
        # Empty is the ordinary row: the figure is usually on promise_source.
        blank = sc.check_row(a_row(size_source=""))
        self.assertNotEqual(blank["result_status"], "FAIL")


# --------------------------------------------------------------------------- #
class TestSentinelsCollideWithRealValues(unittest.TestCase):
    """A stored lag/slip number cannot say whether it is a measurement or a code.

    slip is signed -- negative means the plant beat its promised date -- and the
    codes are -1, -2, -3 and -4, so they sit inside the range of real answers.
    Diamond Green Diesel Port Arthur promised the second half of 2023, produced in
    late 2022, and is stored as -1.0, which is also "not produced yet". It was
    reported as "to be completed" for months. Coarse dates resolve to the middle
    of their period, so whole-year gaps are ordinary, not freakish.
    """

    def test_the_real_row_that_collides(self):
        """The exact figures from the Tracker, so this stays a test about a
        project rather than about arithmetic."""
        _, _, _, lag, slip = dates.compute_lag_slip(
            "2021-01", "2023 (second half)", "2022 (late)")
        self.assertGreater(lag, 0, "the plant produced, so lag is a real span")
        self.assertEqual(slip, dates.TO_BE_COMPLETED,
                         "a real one-year-early slip lands exactly on the "
                         "'not produced yet' code -- if this ever stops being "
                         "true the codes have been moved and this test retires")

    def test_the_resolved_dates_tell_them_apart(self):
        measured = {"announced_dt": "2021-01-15",
                    "promised_first_output_dt": "2023-10-01",
                    "actual_first_output_dt": "2022-10-01"}
        sentinel = {"announced_dt": "2021-01-15",
                    "promised_first_output_dt": "2023-10-01",
                    "actual_first_output_dt": None}
        self.assertTrue(dates.measured_spans(measured)["slip_years"])
        self.assertFalse(dates.measured_spans(sentinel)["slip_years"])

    def test_the_label_stops_lying_when_it_is_told(self):
        self.assertEqual(dates.lag_label(-1.0), "to be completed")
        self.assertEqual(dates.lag_label(-1.0, True), "-1")
        self.assertEqual(dates.lag_label(-1.0, False), "to be completed")
        # A number that was never ambiguous is unaffected either way.
        self.assertEqual(dates.lag_label(1.7), "1.7")
        self.assertEqual(dates.lag_label(1.7, True), "1.7")

    def test_a_row_missing_the_dt_columns_keeps_the_old_reading(self):
        """Older databases predate the *_dt columns. Absent evidence must mean
        "cannot tell", which is the conservative answer, not "measured"."""
        self.assertFalse(dates.measured_spans({})["slip_years"])


class TestAFlagBeginningWithNoneIsStillAFlag(unittest.TestCase):
    """"None of the three cited sources states a capital figure..." is a flag.

    The open-flag rule tested the null token as a PREFIX, so any sentence opening
    with the word "none" was swallowed. Gulf Coast Growth Ventures carried a
    paragraph of real caveats and reported CLEAN, the best verdict the checker
    has. A literal null token in the cell is a different rule and already errors.
    """

    def test_a_sentence_beginning_with_none_warns(self):
        r = sc.check_row(a_row(
            flag="None of the three cited sources states a capital figure, so it is empty"))
        warns = [i for i in r["report"] if i["level"] == "WARN" and i["column"] == "flag"]
        self.assertEqual(len(warns), 1, "the flag must reach the reviewer")

    def test_a_bare_null_token_is_still_not_a_flag(self):
        for token in ("None", "none", "n/a", "-", "None."):
            r = sc.check_row(a_row(flag=token))
            warns = [i for i in r["report"] if i["level"] == "WARN" and i["column"] == "flag"]
            self.assertEqual(warns, [], f"{token!r} is an absence, not a flag")

    def test_a_resolution_record_is_still_not_an_open_flag(self):
        r = sc.check_row(a_row(flag="Resolved: human-verified and promoted."))
        warns = [i for i in r["report"] if i["level"] == "WARN" and i["column"] == "flag"]
        self.assertEqual(warns, [])


class TestFailMeansBroken(Base):
    """FAIL used to mean three unrelated things, and the word said "fault" for
    all three.

    A malformed cell, a project under the size floor, and a figure nobody ever
    published want completely different acts -- fix it, leave it alone, go and
    look -- and the dashboard reported 8 failures on a Tracker in which nothing
    was broken. These tests pin each word to one meaning.
    """

    def row(self, **over):
        return a_row(**over)

    def test_fail_is_now_only_a_malformed_row(self):
        v = sc.check_row(self.row(announced="2022"))["result_status"]
        self.assertEqual(v, "FAIL", "a date in the wrong shape is a real fault")

    def test_a_project_under_the_floor_is_not_a_fault(self):
        v = sc.check_row(self.row(promised_capital_usd=900_000_000,
                                  promised_jobs=1100))["result_status"]
        self.assertEqual(v, "OUT_OF_SCOPE")

    def test_a_figure_nobody_published_is_not_a_fault_either(self):
        v = sc.check_row(self.row(promised_capital_usd=None,
                                  promised_jobs=600))["result_status"]
        self.assertEqual(v, "SIZE_UNKNOWN")

    def test_out_of_scope_outranks_a_malformed_cell(self):
        """Correcting a date on a project that is not going into the Tracker is
        work that buys nothing, so the verdict says the thing worth acting on."""
        v = sc.check_row(self.row(promised_capital_usd=900_000_000,
                                  promised_jobs=1100,
                                  announced="2022"))["result_status"]
        self.assertEqual(v, "OUT_OF_SCOPE")

    def test_a_malformed_cell_outranks_an_unknown_size(self):
        """The other way round: a bad date is fixable today, and an unpublished
        figure may never be. The fixable thing is the one to surface."""
        v = sc.check_row(self.row(promised_capital_usd=None, promised_jobs=600,
                                  announced="2022"))["result_status"]
        self.assertEqual(v, "FAIL")

    def test_splitting_the_word_did_not_open_a_door(self):
        """Three of the five verdicts are not faults. None of them publishes."""
        for cells, expected in (
            ({"announced": "2022"}, "FAIL"),
            ({"promised_capital_usd": 900_000_000, "promised_jobs": 1100}, "OUT_OF_SCOPE"),
            ({"promised_capital_usd": None, "promised_jobs": 600}, "SIZE_UNKNOWN"),
        ):
            rid = screen.insert_extracted(self.conn, a_row(project=f"P{expected}", **cells),
                                          source_collected_id=self.lead())
            self.assertEqual(screen.run_check(self.conn, rid)["result_status"], expected)
            with self.assertRaises(verify.PromotionBlocked, msg=expected):
                verify.promote(self.conn, rid, verification_tier="V1")

    def test_only_clean_and_pass_are_promotable(self):
        self.assertEqual(sorted(sc.PROMOTABLE), ["CLEAN", "PASS"])
        for v in sc.VERDICTS:
            self.assertEqual(sc.blocks_promotion(v), v not in sc.PROMOTABLE)
        self.assertTrue(sc.blocks_promotion(None), "an unchecked row is not promotable")

    def test_the_refusal_names_the_act_the_verdict_calls_for(self):
        """A reader who is told "fix it" about a project that is merely too
        small goes looking for a defect that is not there."""
        small = screen.insert_extracted(
            self.conn, a_row(project="Small", promised_capital_usd=900_000_000,
                             promised_jobs=1100), source_collected_id=self.lead())
        screen.run_check(self.conn, small)
        with self.assertRaises(verify.PromotionBlocked) as caught:
            verify.promote(self.conn, small, verification_tier="V1")
        self.assertIn("nothing to fix", str(caught.exception))

        unknown = screen.insert_extracted(
            self.conn, a_row(project="Unknown", promised_capital_usd=None,
                             promised_jobs=600), source_collected_id=self.lead())
        screen.run_check(self.conn, unknown)
        with self.assertRaises(verify.PromotionBlocked) as caught:
            verify.promote(self.conn, unknown, verification_tier="V1")
        self.assertIn("screen-size", str(caught.exception),
                      "the refusal should name the command that records either outcome")

    def test_the_queue_routes_each_verdict_to_the_right_pile(self):
        ids = {}
        for cells, key in (
            ({"announced": "2022"}, "FAIL"),
            ({"promised_capital_usd": 900_000_000, "promised_jobs": 1100}, "OUT_OF_SCOPE"),
            ({"promised_capital_usd": None, "promised_jobs": 600}, "SIZE_UNKNOWN"),
            ({}, "CLEAN"),
        ):
            rid = screen.insert_extracted(self.conn, a_row(project=f"Q{key}", **cells),
                                          source_collected_id=self.lead())
            screen.run_check(self.conn, rid)
            ids[key] = rid
        q = screen.review_queue(self.conn)
        self.assertEqual([r["id"] for r in q["out_of_scope"]], [ids["OUT_OF_SCOPE"]])
        self.assertEqual([r["id"] for r in q["blocked"]], [ids["FAIL"]],
                         "blocked is malformed-and-fixable, nothing else")
        self.assertEqual([r["id"] for r in q["size_unknown"]], [ids["SIZE_UNKNOWN"]])
        self.assertIn(ids["CLEAN"], [r["id"] for r in q["ready"]])


class TestSizeBackfill(Base):
    """screen-size -- the narrow writer for the rows whose size floor cannot be
    established.

    The floor is an OR over capital and jobs, but 2,000 DIRECT manufacturing
    jobs is a bar almost nothing meets, so capital decides in practice: of the
    first 162 published rows, 159 cleared on capital and 3 on jobs. A lead whose
    two links never print a dollar figure therefore produces a row that cannot
    be shown to be in scope however large the plant is, and seven sat blocked
    exactly that way. The risk under test is the one `screen-date` was built
    against -- a writer that repairs one cell and quietly moves another -- plus
    one this writer has and that one does not: this is the cell the inclusion
    rule reads, so a figure written here without a citation admits a project to
    the Tracker on faith.
    """

    def unestablished(self, **over) -> int:
        """A row in the size-backfill population: jobs below the floor, no
        capital figure, so the floor cannot be established either way."""
        sid = self.lead()
        cells = {"promised_capital_usd": None, "promised_jobs": 600,
                 "flag": "no cited source states a capital figure."}
        cells.update(over)
        rid = screen.insert_extracted(self.conn, a_row(**cells),
                                      source_collected_id=sid)
        self.assertEqual(screen.run_check(self.conn, rid)["result_status"], "SIZE_UNKNOWN")
        return rid

    def test_a_found_figure_clears_the_check(self):
        rid = self.unestablished()
        screen.set_size(self.conn, rid, source="https://example.com/capital",
                        capital=10_000_000_000,
                        raw="a $10 billion petrochemical complex")
        row = screen.get_extracted(self.conn, rid)
        self.assertEqual(row["promised_capital_usd"], 10_000_000_000)
        self.assertEqual(row["size_source"], "https://example.com/capital")
        self.assertNotEqual(screen.run_check(self.conn, rid)["result_status"], "FAIL")

    def test_jobs_can_settle_it_too(self):
        """The column is `size_source`, not `capital_source`, because either
        leg of the OR can be the figure that was missing."""
        rid = self.unestablished(promised_jobs=None)
        screen.set_size(self.conn, rid, source="https://example.com/jobs", jobs=3000)
        row = screen.get_extracted(self.conn, rid)
        self.assertEqual(row["promised_jobs"], 3000)
        self.assertIsNone(row["promised_capital_usd"], "capital must stay empty")
        self.assertNotEqual(screen.run_check(self.conn, rid)["result_status"], "FAIL")

    def test_no_other_cell_moves(self):
        """The whole argument for a narrow writer. In particular lag/slip and
        the *_dt cells must not move: they are derived from dates, and nothing
        derived reads capital or jobs, so putting this through `enrich` would
        risk rewriting a date nobody asked to change."""
        rid = self.unestablished()
        before = dict(screen.get_extracted(self.conn, rid))
        screen.set_size(self.conn, rid, source="https://example.com/c",
                        capital=2_500_000_000)
        after = dict(screen.get_extracted(self.conn, rid))
        moved = {k for k in before if before[k] != after[k]}
        self.assertEqual(moved, {"promised_capital_usd", "size_source", "flag"},
                         f"unexpected cells changed: {moved}")

    def test_the_old_flag_survives(self):
        """The extractor's note says WHY the cell is empty -- and for this rule
        it often says which wrong number it refused. Overwriting it to record
        the fix would delete the reasoning the fix rests on."""
        rid = self.unestablished(
            flag="the only dollar figure covers two facilities, so it is not this one's.")
        before = screen.get_extracted(self.conn, rid)["flag"]
        screen.set_size(self.conn, rid, source="https://e.com/c", capital=3_000_000_000)
        after = screen.get_extracted(self.conn, rid)["flag"]
        self.assertIn(before, after)
        self.assertIn("Resolved", after)

    def test_a_figure_needs_a_url(self):
        """This is the cell the inclusion rule reads. An uncited figure here
        does not merely weaken a row -- it admits a project to the Tracker."""
        rid = self.unestablished()
        for bad in ("", "the company press release", "   "):
            with self.assertRaises(ValueError):
                screen.set_size(self.conn, rid, source=bad, capital=2_000_000_000)
        self.assertIsNone(screen.get_extracted(self.conn, rid)["promised_capital_usd"])

    def test_a_figure_must_be_a_positive_whole_number(self):
        rid = self.unestablished()
        for bad in ("2.5 billion", "$2500000000", -5, 0, "lots"):
            with self.assertRaises(ValueError):
                screen.set_size(self.conn, rid, source="https://e.com/c", capital=bad)

    def test_nothing_to_cite_is_refused(self):
        rid = self.unestablished()
        with self.assertRaises(ValueError):
            screen.set_size(self.conn, rid, source="https://e.com/c")

    def test_an_existing_figure_is_protected(self):
        """Every row in the queue has the cell empty, so landing on a filled
        one means the id is wrong -- and the stored figure is real research."""
        sid = self.lead()
        rid = screen.insert_extracted(self.conn, a_row(promised_capital_usd=5_000_000_000),
                                      source_collected_id=sid)
        with self.assertRaises(screen.SizeOverwriteBlocked):
            screen.set_size(self.conn, rid, source="https://e.com/c", capital=9)
        self.assertEqual(screen.get_extracted(self.conn, rid)["promised_capital_usd"],
                         5_000_000_000)
        screen.set_size(self.conn, rid, source="https://e.com/c",
                        capital=6_000_000_000, force=True)
        self.assertEqual(screen.get_extracted(self.conn, rid)["promised_capital_usd"],
                         6_000_000_000)

    def test_unresolved_leaves_the_figures_alone_and_the_row_failing(self):
        """Exit (b). The floor still cannot be established, so the row must keep
        failing -- what changes is that it now fails having been looked for."""
        rid = self.unestablished()
        screen.mark_size_unresolved(self.conn, rid, "searched the IR release and three trade reports")
        row = screen.get_extracted(self.conn, rid)
        self.assertIsNone(row["promised_capital_usd"])
        self.assertEqual(row["promised_jobs"], 600)
        self.assertIn(screen.SIZE_UNRESOLVED_MARKER, row["flag"])
        self.assertEqual(screen.run_check(self.conn, rid)["result_status"], "SIZE_UNKNOWN",
                         "still not publishable, and still not a fault")

    def test_unresolved_needs_a_reason(self):
        rid = self.unestablished()
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                screen.mark_size_unresolved(self.conn, rid, bad)

    def test_the_queue_excludes_a_row_that_was_measured_out_of_scope(self):
        """The distinction the whole rule rests on. A row stating $700M and 400
        jobs has been measured and is out of scope; offering it here would be
        inviting someone to go find a number that lets it in. Only a row with an
        EMPTY deciding cell is unestablished."""
        measured = screen.insert_extracted(
            self.conn, a_row(project="Measured Fab", promised_capital_usd=700_000_000,
                             promised_jobs=400), source_collected_id=self.lead())
        self.assertEqual(screen.run_check(self.conn, measured)["result_status"], "OUT_OF_SCOPE")
        unestablished = self.unestablished(project="Unestablished Fab")
        self.assertEqual([r["id"] for r in screen.unestablished_size(self.conn)],
                         [unestablished])

    def test_the_queue_drops_what_is_done(self):
        """Both exits must remove a row, or the loop pays for the same dead end
        on every run. Only --all brings the searched ones back."""
        resolved = self.unestablished(project="Resolved Fab")
        searched = self.unestablished(project="Searched Fab")
        untouched = self.unestablished(project="Untouched Fab")
        screen.set_size(self.conn, resolved, source="https://e.com/c", capital=4_000_000_000)
        screen.mark_size_unresolved(self.conn, searched, "nothing states it")

        self.assertEqual([r["id"] for r in screen.unestablished_size(self.conn)],
                         [untouched])
        self.assertCountEqual(
            [r["id"] for r in screen.unestablished_size(self.conn, include_searched=True)],
            [searched, untouched])

    def test_a_published_row_is_frozen(self):
        """verify_verified holds a COPY of the cells, so a Screen write under a
        published row fixes the staging table and leaves the published one
        unchanged. Both exits must refuse, and the refusal must name the size
        cells rather than the date ones it was originally written for."""
        rid = self.unestablished()
        screen.set_size(self.conn, rid, source="https://e.com/c", capital=4_000_000_000)
        screen.run_check(self.conn, rid)
        verify.promote(self.conn, rid, verification_tier="V1", flag="Resolved: checked.")
        with self.assertRaises(screen.RemovalBlocked) as caught:
            screen.set_size(self.conn, rid, source="https://e.com/d",
                            capital=9_000_000_000, force=True)
        self.assertIn("size_source", str(caught.exception))
        self.assertNotIn("actual_first_output", str(caught.exception))
        with self.assertRaises(screen.RemovalBlocked):
            screen.mark_size_unresolved(self.conn, rid, "nothing states it")
        self.assertEqual(screen.get_extracted(self.conn, rid)["promised_capital_usd"],
                         4_000_000_000)

    def test_the_queue_leaves_published_rows_out(self):
        """A row can only be in this queue AND published if someone forced the
        promotion past the failing check -- which is exactly when the queue must
        stay quiet, because the Screen write it would invite is refused."""
        rid = self.unestablished(project="Published Fab")
        screen.run_check(self.conn, rid)
        verify.promote(self.conn, rid, verification_tier="V1",
                       flag="Resolved: checked.", force=True)
        self.assertEqual(screen.unestablished_size(self.conn), [])


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
    without the section, the failure is silent and shows up as a Tracker with a
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

    def test_it_carries_the_size_search_rule(self):
        """The second and last search past the lead's links. Without it a row
        whose two pages never print a dollar figure cannot be shown to be in
        scope at all -- seven were blocked that way, including an
        ExxonMobil-SABIC cracker and a 1 bcf/day hydrogen plant."""
        self.assertIn("When the two sources do not state capital or jobs", self.prompt)
        self.assertIn("size_source", self.prompt)

    def test_the_size_search_carries_its_exits_and_traps(self):
        """Exit (c) is the common one, and each trap below is a real blocked
        row. Dropping them is how the search stops being a search for THIS
        project's capital and becomes a search for any nearby large number."""
        for exit_text in ("You found a figure",
                          "You searched and no source states it",
                          "The figures you can find are not this project's"):
            self.assertIn(exit_text, self.prompt)
        for trap in ("A combined figure is not this project's capital",
                     "Financing is not capital",
                     "A transaction price is not capital",
                     "Group guidance is not project capital"):
            self.assertIn(trap, self.prompt)

    def test_the_size_search_still_defers_to_re_announcement_discipline(self):
        """A figure found in 2026 is often the third re-announcement. The row's
        anchor is the ORIGINAL promise, and a search that ignores that swaps a
        missing number for a wrong one."""
        self.assertIn("re-announcement discipline below still governs", self.prompt)


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

    def test_the_promised_sentinel_is_refused_in_the_actual_slot(self):
        """The mirror of the defect. `n/a` there would say no promise was made
        about a column that records what happened."""
        self.assertEqual("FAIL", self._check(actual_first_output="n/a")["result_status"])
        for token in ("tbd", "open"):
            with self.subTest(token=token):
                self.assertEqual("FAIL",
                                 self._check(actual_first_output=token)["result_status"])

    def test_a_date_with_a_word_the_reader_does_not_know_is_refused(self):
        """"2019 (pending permits)" used to pass here as a dated promise while
        dates.py saw "pending" and resolved it to no date at all. A token the
        checker accepts has to be one the date reader reads the same way."""
        self.assertTrue(self._messages("promised_first_output",
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
class TestStatusIsCountable(Base):
    """status: one of six words, agreeing with actual_first_output.

    current_status is free text, and the first 85 published projects began it
    32 different ways, so nothing could count how many were producing.
    """

    def _errors(self, **over):
        return [i["message"] for i in sc.check_row(a_row(**over))["report"]
                if i["column"] == "status" and i["level"] == "ERROR"]

    def test_each_word_passes_with_a_first_output_that_fits(self):
        for status, first in (("announced", "pending"), ("under construction", "pending"),
                              ("paused", "pending"), ("producing", "2025-03"),
                              ("producing", "unconfirmed"), ("closed", "2019"),
                              ("cancelled", "never")):
            with self.subTest(status=status, first=first):
                self.assertEqual([], self._errors(status=status, actual_first_output=first))

    def test_a_word_off_the_list_fails(self):
        for word in ("delayed", "OPERATIONAL", ""):
            with self.subTest(word=word):
                self.assertTrue(self._errors(status=word))

    def test_a_status_that_contradicts_first_output_fails(self):
        """The two state one fact. A disagreement counts the project in the
        wrong bar."""
        for status, first in (("producing", "pending"), ("under construction", "2024"),
                              ("cancelled", "unconfirmed"), ("closed", "never")):
            with self.subTest(status=status, first=first):
                msgs = self._errors(status=status, actual_first_output=first)
                self.assertTrue(any("disagrees with actual_first_output" in m for m in msgs), msgs)

    def test_it_is_stored_in_one_spelling_and_carried_to_verify(self):
        rid = screen.insert_extracted(self.conn, a_row(status="Under  Construction"),
                                      source_collected_id=self.lead())
        self.assertEqual("under construction", screen.get_extracted(self.conn, rid)["status"])
        screen.run_check(self.conn, rid)
        vid = verify.promote(self.conn, rid, verification_tier="V1", flag="Resolved: checked.")
        self.assertEqual("under construction", verify.get_verified(self.conn, vid)["status"])

    def test_dating_first_output_needs_the_status_to_move_with_it(self):
        """Otherwise a correction publishes a producing plant counted as under
        construction."""
        rid = screen.insert_extracted(self.conn, a_row(), source_collected_id=self.lead())
        screen.run_check(self.conn, rid)
        vid = verify.promote(self.conn, rid, verification_tier="V1", flag="Resolved: checked.")
        with self.assertRaises(verify.CorrectionRefused):
            verify.edit(self.conn, vid, {"actual_first_output": "2025-06"},
                        edit_description="dated")
        verify.edit(self.conn, vid, {"actual_first_output": "2025-06", "status": "producing"},
                    edit_description="dated")
        self.assertEqual("producing", verify.get_verified(self.conn, vid)["status"])


class TestDateQualifiers(unittest.TestCase):
    """A date token carries only words the date reader knows.

    "2026 (end)" and "2024 (fall)" were both read as July 1: a word the reader
    did not know fell through to the bare-year rule, and the checker accepted
    any token with a year in it.
    """

    def test_end_and_fall_resolve_inside_their_window(self):
        self.assertEqual(("2026-11-15", "date"), dates.interpret_date("2026 (end)"))
        self.assertEqual(("2024-10-15", "date"), dates.interpret_date("2024 (fall)"))

    def test_the_qualifiers_already_stored_still_read_the_same(self):
        for token, iso in (("2025 (first half)", "2025-04-01"),
                           ("2025 (second half)", "2025-10-01"),
                           ("2023 (late)", "2023-10-01"), ("2021 (mid)", "2021-07-01"),
                           ("2024 (early)", "2024-03-01"), ("2025-Q1", "2025-02-15"),
                           ("2025", "2025-07-01"), ("2025-03", "2025-03-15"),
                           ("2022-12-30", "2022-12-30")):
            with self.subTest(token=token):
                self.assertEqual((iso, "date"), dates.interpret_date(token))
                self.assertEqual("", dates.unrecognized_words(token))
                self.assertNotEqual("FAIL", sc.check_row(
                    a_row(promised_first_output=token))["result_status"])

    def test_a_word_the_reader_does_not_know_fails_the_row(self):
        for token in ("2026 (spring)", "2027 (target)", "2019 (pending permits)"):
            with self.subTest(token=token):
                self.assertEqual("FAIL", sc.check_row(
                    a_row(promised_first_output=token))["result_status"])


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
            current_status="IN FULL OPERATION", status="producing", **over),
            source_collected_id=sid)
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

    The Tracker is built by sweeping at a high threshold and lowering it in a
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
        self.assertEqual(sc.check_row(small, settings.PHASES["1B-or-2000-jobs"])["result_status"],
                         "OUT_OF_SCOPE")

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
                     "EFFORT", "LEADS_PER_CALL", "MAX_STALL", "VERBOSE", "ITERS_PER_ROW",
                     "PRELOAD_ARTICLES"):
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
    import time: the suite passed while `tracker.py collect` -- the entry
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
    """`tracker.py collect --dry-run` runs end to end.

    The CLI hands off to collect/all.sh, which calls back into the CLI for the
    model, effort and run settings. Two regressions in one week broke that
    handoff while every unit test passed, because nothing ran the entry point.
    Runs against an empty temporary database with the log and the auth probe off.
    """

    def test_dry_run_plans_a_source_stage(self):
        import subprocess
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, TRACKER_DB=str(Path(tmp) / "empty.db"), LOG="0", PREFLIGHT="0")
            env.pop("MEDALLION_DB", None)
            r = subprocess.run(
                [sys.executable, "tracker.py", "collect", "--n", "1", "--only", "source", "--dry-run"],
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

    def test_clearing_neither_floor_is_out_of_scope_not_a_fault(self):
        """Both figures known, both under the floor. The row is not broken -- the
        project is too small -- so the verdict must not be the one that means a
        person has something to repair."""
        _, v = self._verdict(_n=3, promised_capital_usd=700_000_000, promised_jobs=400)
        self.assertEqual(v, "OUT_OF_SCOPE")

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

    def test_every_unpromotable_verdict_blocks_but_force_gets_through(self):
        """Splitting FAIL into three words must not open a door. Out of scope is
        not a fault and is still not publishable."""
        rid, v = self._verdict(_n=5, promised_capital_usd=700_000_000, promised_jobs=400)
        self.assertEqual(v, "OUT_OF_SCOPE")
        with self.assertRaises(verify.PromotionBlocked):
            verify.promote(self.conn, rid, verification_tier="V1")
        self.assertTrue(verify.promote(self.conn, rid, verification_tier="V1", force=True))

    def test_no_flag_editing_command_exists_at_screen(self):
        """If one is ever added, the docs claiming promotion is the only writer
        become wrong and this test should be the thing that says so."""
        import os
        import subprocess
        # A temporary database. Without one this opens outputs/tracker.db, and
        # any migration waiting in the code runs on the real file.
        with tempfile.TemporaryDirectory() as tmp:
            out = subprocess.run(
                [sys.executable, "tracker.py", "--help"],
                cwd=str(Path(__file__).resolve().parent.parent),
                env=dict(os.environ, TRACKER_DB=str(Path(tmp) / "t.db")),
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
        """tracker.db is committed and git cannot diff a binary, so the
        audit trail only reaches a reader through the CSVs."""
        from pipeline.export_tables import ALL_TABLES
        self.assertEqual("screen_attested", ALL_TABLES["screen_attested"])

    # ---- the sentinel rule: the buttons record what the page shows -------- #

    def test_an_absence_cannot_be_confirmed(self):
        """`n/a` and an empty field record that no source stated the value, so
        "confirmed" would say a page shows a value the field says does not
        exist. Nine were confirmed while no rule said otherwise."""
        for value in ("n/a", " N/A ", ""):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    screen.attest(self.conn, self.sid, "promised_first_output",
                                  "confirmed", self.WHO, value=value)
        self.assertEqual(0, self.conn.execute(
            "SELECT count(*) FROM screen_attested").fetchone()[0])

    def test_not_in_source_is_how_an_absence_is_verified(self):
        screen.attest(self.conn, self.sid, "promised_first_output", "not_in_source",
                      self.WHO, value="n/a")
        got = screen.attestation_state(self.conn, self.sid)["promised_first_output"]
        self.assertEqual("not_in_source", got["state"])
        self.assertFalse(got["confirmed_absence"])

    def test_the_event_sentinels_are_confirmed_like_any_value(self):
        """pending, never and unconfirmed each describe something a page shows."""
        for value in ("pending", "never", "unconfirmed"):
            with self.subTest(value=value):
                screen.attest(self.conn, self.sid, "actual_first_output", "confirmed",
                              self.WHO, value=value)

    def test_a_correction_is_what_gets_confirmed(self):
        """The defect this rule exposed. Confirming recorded the stored Screen
        value, so a reviewer who corrected a field and confirmed it left a record
        vouching for the value they had just replaced."""
        screen.attest(self.conn, self.sid, "promised_jobs", "confirmed", self.WHO,
                      value="1750")
        self.assertEqual("1750", self.conn.execute(
            "SELECT value_at_time FROM screen_attested").fetchone()["value_at_time"])

    def test_a_confirmed_absence_from_before_the_rule_is_flagged(self):
        """Append-only, so old rows are shown for re-settling, not rewritten.
        attest() refuses these now, so write one the old way."""
        from pipeline.db import now_iso
        self.conn.execute(
            "INSERT INTO screen_attested (datetime, screen_extracted_id, field, state, "
            "value_at_time, attested_by) VALUES (?, ?, 'promised_first_output', "
            "'confirmed', 'n/a', ?)", (now_iso(), self.sid, self.WHO))
        self.conn.commit()
        self.assertTrue(screen.attestation_state(
            self.conn, self.sid)["promised_first_output"]["confirmed_absence"])

    def test_absence_is_exactly_empty_or_the_promised_sentinel(self):
        """Built from the checker's own sentinel set, so the two cannot drift."""
        from pipeline import schema_check as sc
        self.assertEqual(frozenset({""} | set(sc.PROMISED_SENTINELS)),
                         screen.ABSENCE_VALUES)
        self.assertTrue(screen.is_absence(None))
        self.assertFalse(screen.is_absence("pending"))


# --------------------------------------------------------------------------- #
class TestVerifierList(unittest.TestCase):
    """The addresses are published beside the data, so they are askable.

    settings.py opens by saying to ask `config` what is in effect rather than
    reading the file. A list that decides whose name goes into public data, and
    that `config` does not mention, breaks that promise in the one place it
    matters most."""

    def test_config_names_who_may_verify(self):
        import os
        import subprocess
        # A temporary database, for the reason given in the --help test above:
        # adding the status column ran on outputs/tracker.db from here.
        with tempfile.TemporaryDirectory() as tmp:
            out = subprocess.run(
                [sys.executable, "tracker.py", "config"],
                cwd=str(Path(__file__).resolve().parent.parent),
                env=dict(os.environ, TRACKER_DB=str(Path(tmp) / "t.db")),
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


# --------------------------------------------------------------------------- #
class TestPreloadSetting(unittest.TestCase):
    """Preloading downloads a hundred or more pages from other people's sites,
    so it happens only on a machine that asked for it."""

    def test_off_unless_this_machine_asks(self):
        from unittest import mock
        with mock.patch.dict(os.environ):
            os.environ.pop("PRELOAD_ARTICLES", None)
            self.assertFalse(settings.preload_articles())
            self.assertEqual(settings.preload_source(), "settings.py")

    def test_the_variable_decides_once_it_is_set(self):
        from unittest import mock
        for value, want in (("1", True), ("on", True), ("0", False), ("no", False)):
            with mock.patch.dict(os.environ, {"PRELOAD_ARTICLES": value}):
                self.assertEqual(settings.preload_articles(), want, value)
                self.assertEqual(settings.preload_source(), "$PRELOAD_ARTICLES")

    def test_config_says_whether_pages_are_preloaded(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            out = subprocess.run(
                [sys.executable, "tracker.py", "config"],
                cwd=str(Path(__file__).resolve().parent.parent),
                env=dict(os.environ, TRACKER_DB=str(Path(tmp) / "t.db"),
                         PRELOAD_ARTICLES="1"),
                capture_output=True, text=True).stdout
        self.assertRegex(out, r"preload articles\s+on\s+<- \$PRELOAD_ARTICLES")


# --------------------------------------------------------------------------- #
class TestConfigEnvWriter(unittest.TestCase):
    """The preload checkbox writes config.env, and config.env holds the API key.

    A writer that rewrote the file from what it understood would drop the
    comments, reorder the lines, or leave the key readable by everyone. The
    checkbox has to change one line and nothing else.
    """

    def setUp(self):
        from unittest import mock
        import pipeline
        self.pipeline = pipeline
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "config.env"
        self.env = mock.patch.dict(os.environ)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def test_one_line_changes_and_the_key_stays(self):
        self.path.write_text("# mine\nANTHROPIC_API_KEY=sk-test\n"
                             "PRELOAD_ARTICLES=0\nMODEL=m\n")
        os.chmod(self.path, 0o600)
        self.pipeline.set_config_env("PRELOAD_ARTICLES", "1", path=self.path)
        self.assertEqual(self.path.read_text(), "# mine\nANTHROPIC_API_KEY=sk-test\n"
                                                "PRELOAD_ARTICLES=1\nMODEL=m\n")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(os.environ["PRELOAD_ARTICLES"], "1")
        self.assertFalse(Path(str(self.path) + ".tmp").exists())

    def test_a_missing_file_is_created_readable_by_its_owner_only(self):
        self.pipeline.set_config_env("PRELOAD_ARTICLES", "1", path=self.path)
        self.assertEqual(self.path.read_text(), "PRELOAD_ARTICLES=1\n")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_a_comment_is_not_a_setting(self):
        """And a second copy goes, because the loader only ever reads the first."""
        self.path.write_text("# PRELOAD_ARTICLES=1\nPRELOAD_ARTICLES=0\nPRELOAD_ARTICLES=0\n")
        self.pipeline.set_config_env("PRELOAD_ARTICLES", "1", path=self.path)
        self.assertEqual(self.path.read_text(), "# PRELOAD_ARTICLES=1\nPRELOAD_ARTICLES=1\n")


# --------------------------------------------------------------------------- #
class TestMissingDatabaseDirectory(unittest.TestCase):
    """A stale $..._DB export says so, instead of looking like a corrupt file.

    The incident: the working directory was renamed, a $SCOREBOARD_DB left over
    from before it kept pointing at the old absolute path, and every command
    died on `sqlite3.OperationalError: unable to open database file`. That reads
    like the database is damaged. It was fine; a variable set weeks earlier was
    aiming at a directory that no longer existed.

    A missing FILE is not the failure -- sqlite creates it, which is how initdb
    works on a fresh path. A missing DIRECTORY is, and sqlite reports both with
    the same six words.
    """

    def setUp(self):
        from pipeline import db as pdb
        self.dir = tempfile.TemporaryDirectory()
        self._was = pdb._FROM_DB_FLAG

    def tearDown(self):
        from pipeline import db as pdb
        for v in pdb.DB_PATH_VARS:
            os.environ.pop(v, None)
        pdb._FROM_DB_FLAG = self._was
        self.dir.cleanup()

    def _gone(self) -> str:
        return str(Path(self.dir.name) / "no-such-dir" / "t.db")

    def test_it_names_the_directory_the_variable_and_the_fix(self):
        from pipeline import db as pdb
        os.environ["SCOREBOARD_DB"] = self._gone()
        with self.assertRaises(SystemExit) as e:
            pdb.connect()
        msg = str(e.exception)
        self.assertIn("no-such-dir", msg)
        self.assertIn("$SCOREBOARD_DB", msg)
        self.assertIn("unset SCOREBOARD_DB", msg)

    def test_the_db_flag_is_not_blamed_on_a_variable_nobody_set(self):
        """--db is implemented by exporting $TRACKER_DB so child processes
        inherit it, which makes the flag and a real export look identical by the
        time anything reads them. Telling someone who typed --db to unset a
        variable sends them looking for something that is not there."""
        from pipeline import db as pdb
        os.environ["TRACKER_DB"] = self._gone()
        pdb.note_db_from_flag()
        with self.assertRaises(SystemExit) as e:
            pdb.connect()
        msg = str(e.exception)
        self.assertIn("--db", msg)
        self.assertNotIn("unset", msg)

    def test_a_missing_file_in_a_real_directory_is_still_created(self):
        """The guard must not break initdb, whose whole job is a path with no
        database at it yet."""
        from pipeline import db as pdb
        fresh = Path(self.dir.name) / "fresh.db"
        conn = pdb.connect(fresh)
        pdb.init_db(conn)
        conn.close()
        self.assertTrue(fresh.exists())

    def test_the_variables_are_read_in_the_documented_order(self):
        """Two renames means three generations of this variable. Which one wins
        decides what the error tells you to unset."""
        from pipeline import db as pdb
        self.assertEqual(("TRACKER_DB", "SCOREBOARD_DB", "MEDALLION_DB"),
                         pdb.DB_PATH_VARS)
        os.environ["MEDALLION_DB"] = "/a/m.db"
        self.assertEqual("$MEDALLION_DB", pdb.db_path_source())
        os.environ["SCOREBOARD_DB"] = "/a/s.db"
        self.assertEqual("$SCOREBOARD_DB", pdb.db_path_source())
        os.environ["TRACKER_DB"] = "/a/t.db"
        self.assertEqual("$TRACKER_DB", pdb.db_path_source())
        self.assertEqual(Path("/a/t.db"), pdb.db_path())

    def test_an_export_that_comes_back_points_at_the_startup_file(self):
        """`unset` fixes one terminal. The second time this message was hit the
        variable was back, because a shell startup file exports it, so the
        message has to say where to look or it sends people round the same loop."""
        from pipeline import db as pdb
        os.environ["SCOREBOARD_DB"] = self._gone()
        with self.assertRaises(SystemExit) as e:
            pdb.connect()
        msg = str(e.exception)
        self.assertIn("new terminal", msg)
        self.assertIn("grep -n SCOREBOARD_DB", msg)


# --------------------------------------------------------------------------- #
class TestCorrectionsMeetTheChecker(Base):
    """A correction is checked before it is saved, at every door.

    verify.edit used to skip the checker. On a published project a misspelt
    `penidng`, an `n/a` in the actual-output field and a `pending` in the
    promised field were each stored as typed, and each left a published record
    the checker fails. The inspect page, the Verify edit page and
    `tracker.py verify-edit` all publish corrections through it.
    """

    def published(self, **over) -> int:
        rid = screen.insert_extracted(self.conn, a_row(**over), source_collected_id=self.lead())
        screen.run_check(self.conn, rid)
        return verify.promote(self.conn, rid, verification_tier="V1",
                              flag="Resolved: checked.")

    def _edits(self, vid) -> int:
        return self.conn.execute("SELECT count(*) FROM verify_edits WHERE verify_verified_id = ?",
                                 (vid,)).fetchone()[0]

    def test_a_misspelt_sentinel_is_refused_and_nothing_is_saved(self):
        vid = self.published()
        with self.assertRaises(verify.CorrectionRefused):
            verify.edit(self.conn, vid, {"actual_first_output": "penidng"},
                        edit_description="typo")
        self.assertEqual("pending", verify.get_verified(self.conn, vid)["actual_first_output"])
        self.assertEqual(0, self._edits(vid))

    def test_a_sentinel_in_the_wrong_field_is_refused(self):
        vid = self.published()
        for col, word in (("actual_first_output", "n/a"), ("promised_first_output", "pending")):
            with self.subTest(col=col):
                with self.assertRaises(verify.CorrectionRefused):
                    verify.edit(self.conn, vid, {col: word}, edit_description="wrong field")

    def test_a_capitalised_sentinel_is_stored_in_one_spelling(self):
        """It passes the checker either way, which is why it needed doing: the
        published CSV would otherwise carry "N/A" beside "n/a"."""
        vid = self.published()
        verify.edit(self.conn, vid, {"promised_first_output": "N/A"},
                    edit_description="no promise stated")
        self.assertEqual("n/a", verify.get_verified(self.conn, vid)["promised_first_output"])

    def test_a_valid_correction_still_lands(self):
        vid = self.published()
        verify.edit(self.conn, vid, {"promised_first_output": "2025-Q4"},
                    edit_description="per the release")
        self.assertEqual("2025-Q4", verify.get_verified(self.conn, vid)["promised_first_output"])

    def test_an_error_the_record_already_had_does_not_block_an_unrelated_fix(self):
        """A record published with force=True that already fails can still be
        corrected. Only errors the correction introduces are refused."""
        rid = screen.insert_extracted(
            self.conn, a_row(project="Forced", promised_date_source="not-a-url"),
            source_collected_id=self.lead())
        screen.run_check(self.conn, rid)
        vid = verify.promote(self.conn, rid, verification_tier="V1",
                             flag="Resolved: forced.", force=True)
        verify.edit(self.conn, vid, {"current_status": "PRODUCING"},
                    edit_description="status moved")
        self.assertEqual("PRODUCING", verify.get_verified(self.conn, vid)["current_status"])

    def test_a_correction_can_be_checked_before_anything_is_published(self):
        """The inspect page publishes first and corrects second, so it asks this
        before promoting. Otherwise a refusal leaves the old value live."""
        rid = screen.insert_extracted(self.conn, a_row(project="Unpublished"),
                                      source_collected_id=self.lead())
        with self.assertRaises(verify.CorrectionRefused):
            verify.prepare_changes(screen.get_extracted(self.conn, rid),
                                   {"actual_first_output": "penidng"})
        self.assertEqual(0, self.conn.execute(
            "SELECT count(*) FROM verify_verified").fetchone()[0])

    def test_the_command_line_says_so_instead_of_crashing(self):
        import subprocess
        vid = self.published()
        self.conn.commit()
        r = subprocess.run(
            [sys.executable, "tracker.py", "--db", str(self.path), "verify-edit",
             "--id", str(vid), "--set", "actual_first_output=penidng", "--desc", "typo"],
            cwd=str(Path(__file__).resolve().parent.parent), capture_output=True, text=True)
        self.assertNotEqual(0, r.returncode)
        self.assertIn("not saved", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


# --------------------------------------------------------------------------- #
class TestSettlesFollowThePublishedRecord(Base):
    """Once a project is published, the checklist vouches for the published record.

    The inspect page of a published project showed the Screen record, which is
    what the extractor wrote, not what went out. A correction made at
    verification reaches only the published record, so a corrected field could be
    settled only against the value it replaced, and a settle on that value never
    read as stale, because staleness was compared with Screen too.
    """

    WHO = "ashwin@industriousaf.org"

    def setUp(self):
        super().setUp()
        self.sid = screen.insert_extracted(self.conn, a_row(), source_collected_id=self.lead())
        screen.run_check(self.conn, self.sid)

    def _publish_with_correction(self) -> int:
        vid = verify.promote(self.conn, self.sid, verification_tier="V1",
                             flag="Resolved: checked.")
        verify.edit(self.conn, vid, {"promised_first_output": "2025-Q4"},
                    edit_description="per the release")
        return vid

    def test_an_unpublished_project_is_seen_as_its_screen_record(self):
        view, vid = screen.review_view(self.conn, self.sid)
        self.assertIsNone(vid)
        self.assertEqual("2024", view["promised_first_output"])

    def test_a_published_project_is_seen_as_what_went_out_under_its_screen_id(self):
        vid = self._publish_with_correction()
        view, got = screen.review_view(self.conn, self.sid)
        self.assertEqual(vid, got)
        self.assertEqual("2025-Q4", view["promised_first_output"])
        self.assertEqual(self.sid, view["id"], "every screen-keyed address depends on this")
        self.assertEqual("2024", screen.get_extracted(self.conn, self.sid)["promised_first_output"],
                         "the Screen record itself is untouched")

    def test_a_settle_on_the_replaced_value_is_stale_once_published(self):
        """The defect this pins: Chobani read "confirmed 2027" while its
        published value was n/a, and nothing showed it."""
        screen.attest(self.conn, self.sid, "promised_first_output", "confirmed",
                      self.WHO, value="2024")
        self.assertFalse(screen.attestation_state(
            self.conn, self.sid)["promised_first_output"]["stale"])
        self._publish_with_correction()
        self.assertTrue(screen.attestation_state(
            self.conn, self.sid)["promised_first_output"]["stale"])

    def test_a_settle_with_no_value_sent_vouches_for_the_published_value(self):
        self._publish_with_correction()
        screen.attest(self.conn, self.sid, "promised_first_output", "confirmed", self.WHO)
        self.assertEqual("2025-Q4", self.conn.execute(
            "SELECT value_at_time FROM screen_attested").fetchone()["value_at_time"])

    def test_needs_resettle_names_both_kinds_and_nothing_else(self):
        """The Screen list's filter asks this, so it has to catch a settle on a
        value that did not go out and a confirmed empty field, and leave a sound
        settle alone."""
        other = screen.insert_extracted(self.conn, a_row(project="Settled Fab"),
                                        source_collected_id=self.lead())
        screen.run_check(self.conn, other)
        screen.attest(self.conn, other, "promised_jobs", "confirmed", self.WHO, value="1500")
        self.assertEqual({}, screen.needs_resettle(self.conn))

        screen.attest(self.conn, self.sid, "promised_first_output", "confirmed",
                      self.WHO, value="2024")
        self._publish_with_correction()
        from pipeline.db import now_iso
        self.conn.execute(
            "INSERT INTO screen_attested (datetime, screen_extracted_id, field, state, "
            "value_at_time, attested_by) VALUES (?, ?, 'promised_capital_usd', "
            "'confirmed', '', ?)", (now_iso(), other, self.WHO))
        self.conn.commit()
        self.assertEqual({self.sid: ["promised_first_output"],
                          other: ["promised_capital_usd"]},
                         screen.needs_resettle(self.conn))
