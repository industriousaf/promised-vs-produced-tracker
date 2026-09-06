"""Regression tests for the places where a mistake corrupts data silently.

Every test here is a real incident. The Scoreboard had no tests until 2026-09-05,
and the defects found by hand in the days before that are the specification: a
duplicate row that made a run report success one project short, a slip sentinel
that marked produced projects as censored, an export that overwrote the real
corpus from a scratch database, the literal string "None" stored as data.

Two of them were introduced while fixing the others, which is the actual
argument for this file. Hand-verification caught both, but only because the
right thing happened to be checked.

Run:  python3 -m unittest discover -s tests
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import dates, models, quality, screen, source, verify  # noqa: E402
from pipeline import schema_check as sc  # noqa: E402
from pipeline.db import connect, init_db  # noqa: E402
from tools.export_tables import export_dir  # noqa: E402


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
    project's incidents were verification steps writing to the live corpus."""

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
class TestConfig(unittest.TestCase):
    """Facts written down twice eventually disagree with themselves."""

    def tearDown(self):
        for k in ("MODEL", "SCREEN_MODEL", "SOURCE_MODEL"):
            os.environ.pop(k, None)

    def test_export_goes_beside_its_own_database(self):
        """A variable source and a fixed destination: exporting a scratch
        database overwrote the real corpus's CSVs."""
        self.assertEqual(export_dir(db="/tmp/scratch/x.db"), Path("/tmp/scratch/csv_tables"))

    def test_out_dir_still_wins(self):
        self.assertEqual(export_dir(out_dir="/tmp/elsewhere", db="/tmp/x/y.db"),
                         Path("/tmp/elsewhere"))

    def test_model_precedence(self):
        self.assertEqual(models.screen(), models.SCREEN)
        os.environ["MODEL"] = "global-model"
        self.assertEqual(models.screen(), "global-model")
        os.environ["SCREEN_MODEL"] = "stage-model"
        self.assertEqual(models.screen(), "stage-model")
        self.assertEqual(models.source(), "global-model")


if __name__ == "__main__":
    unittest.main()
