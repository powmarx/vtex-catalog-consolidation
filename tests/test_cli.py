"""Tests for the command line surface (DS4, DS5, DS9)."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from catalog_consolidation import cli

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "data" / "catalog.db"
ENTRIES = ROOT / "data" / "ProductEntry.json"


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / "catalog.db"
        shutil.copy(CATALOG, self.db)
        self.reports = self.tmp / "reports"

    def invoke(self, *extra: str) -> tuple[int, str, str]:
        argv = [
            "--database",
            str(self.db),
            "--input",
            str(ENTRIES),
            "--reports-dir",
            str(self.reports),
            *extra,
        ]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def digest(self) -> str:
        return hashlib.sha256(self.db.read_bytes()).hexdigest()

    def counts(self) -> tuple[int, int]:
        conn = sqlite3.connect(str(self.db))
        self.addCleanup(conn.close)
        return (
            conn.execute("SELECT count(*) FROM Product").fetchone()[0],
            conn.execute("SELECT count(*) FROM SellerProduct").fetchone()[0],
        )

    def rows(self) -> tuple[list, list]:
        """Every row of both tables, for comparing logical state between runs."""
        conn = sqlite3.connect(str(self.db))
        self.addCleanup(conn.close)
        return (
            conn.execute("SELECT Id, Name, Brand, Category FROM Product ORDER BY Id").fetchall(),
            conn.execute(
                "SELECT Id, SellerName, ProductId, SellerProductId FROM SellerProduct ORDER BY Id"
            ).fetchall(),
        )


class TestArguments(unittest.TestCase):
    def test_database_and_input_are_both_required(self):
        for argv in ([], ["--database", "x"], ["--input", "y"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as ctx:
                    with redirect_stderr(io.StringIO()):
                        cli.build_parser().parse_args(argv)
                self.assertEqual(ctx.exception.code, 2)

    def test_there_is_no_default_database(self):
        # DS4: a default would make it possible to mutate the committed baseline by
        # forgetting an argument.
        action = next(a for a in cli.build_parser()._actions if a.dest == "database")
        self.assertTrue(action.required)
        self.assertIsNone(action.default)

    def test_report_choices(self):
        action = next(a for a in cli.build_parser()._actions if a.dest == "report")
        self.assertEqual(set(action.choices), {"text", "json"})
        self.assertEqual(action.default, "text")


class TestDryRun(CliTestCase):
    def test_dry_run_leaves_the_file_byte_identical(self):
        before = self.digest()
        code, out, _ = self.invoke("--dry-run", "--no-report-file")
        self.assertEqual(code, 0)
        self.assertEqual(self.digest(), before, "a dry run must not write anything")
        self.assertIn("DRY RUN", out)

    def test_dry_run_rolls_back_the_migration_too(self):
        # DS5: committing the schema change while rolling back inserts would mutate the
        # file during an operation named dry run.
        self.invoke("--dry-run", "--no-report-file")
        conn = sqlite3.connect(str(self.db))
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
        declared = next(
            r[2] for r in conn.execute('PRAGMA table_info("SellerProduct")') if r[1] == "SellerProductId"
        )
        self.assertEqual(declared, "INTEGER")
        self.assertEqual(list(conn.execute('PRAGMA index_list("SellerProduct")')), [])

    def test_dry_run_still_reports_what_would_happen(self):
        _, out, _ = self.invoke("--dry-run", "--no-report-file")
        self.assertIn("records read", out)
        self.assertIn("269", out)
        self.assertIn("978", out)

    def test_dry_run_reports_the_review_candidates(self):
        _, out, _ = self.invoke("--dry-run", "--no-report-file")
        self.assertIn("Roteador WiFi 6 TP-Link", out)
        self.assertIn("Processador AMD Ryzen 9 7950X", out)


class TestRealRun(CliTestCase):
    def test_exit_zero_and_the_expected_counts(self):
        code, _, _ = self.invoke("--no-report-file")
        self.assertEqual(code, 0)
        self.assertEqual(self.counts(), (978, 257))

    def test_migration_is_applied(self):
        self.invoke("--no-report-file")
        conn = sqlite3.connect(str(self.db))
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_running_twice_changes_no_data(self):
        """Idempotency is about rows, not bytes.

        A second run does open a write transaction: it attempts all 269 inserts and has
        every one refused. That leaves the data identical but bumps SQLite's file change
        counter, and because AUTOINCREMENT allocates a rowid before the ON CONFLICT check
        the suppressed inserts still advance `sqlite_sequence`. So the file differs in
        four bytes while containing exactly the same rows. Byte equality is only
        guaranteed for a rollback, which the dry-run test asserts separately.
        """
        self.invoke("--no-report-file")
        first = self.rows()

        code, _, _ = self.invoke("--no-report-file")
        self.assertEqual(code, 0)
        self.assertEqual(self.counts(), (978, 257))
        self.assertEqual(self.rows(), first, "a second run must not change any row")

    def test_a_second_run_suppresses_every_record(self):
        self.invoke("--no-report-file")
        _, out, _ = self.invoke("--report", "json", "--no-report-file")
        summary = json.loads(out)["summary"]
        self.assertEqual(summary["inserted"], 0)
        self.assertEqual(summary["links_created"], 0)
        self.assertEqual(summary["suppressed"], 269)
        self.assertEqual(summary["products_before"], summary["products_after"])

    def test_json_output_is_machine_readable(self):
        code, out, _ = self.invoke("--report", "json", "--no-report-file")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["summary"]["records_read"], 269)
        self.assertEqual(payload["summary"]["matched"], 266)
        self.assertEqual(payload["summary"]["inserted"], 3)
        self.assertEqual(payload["summary"]["links_created"], 257)
        self.assertEqual(payload["summary"]["suppressed"], 12)
        self.assertEqual(len(payload["review_candidates"]), 2)

    def test_candidates_can_be_skipped(self):
        _, out, _ = self.invoke("--report", "json", "--no-report-file", "--no-candidates")
        self.assertEqual(json.loads(out)["review_candidates"], [])


class TestReportFiles(CliTestCase):
    """DS9: written after the transaction closes, never during it."""

    def test_both_files_are_written(self):
        code, out, _ = self.invoke()
        self.assertEqual(code, 0)
        written = sorted(p.suffix for p in self.reports.iterdir())
        self.assertEqual(written, [".json", ".md"])
        self.assertIn("findings:", out)

    def test_json_and_markdown_agree(self):
        self.invoke()
        payload = json.loads(next(self.reports.glob("*.json")).read_text(encoding="utf-8"))
        markdown = next(self.reports.glob("*.md")).read_text(encoding="utf-8")
        self.assertIn(str(payload["summary"]["records_read"]), markdown)
        self.assertIn(str(payload["summary"]["links_created"]), markdown)
        for candidate in payload["review_candidates"]:
            self.assertIn(candidate["inserted_name"], markdown)

    def test_filename_carries_the_input_stem(self):
        self.invoke()
        names = [p.name for p in self.reports.glob("*.json")]
        self.assertTrue(all("ProductEntry" in n for n in names), names)

    def test_a_dry_run_report_is_marked_as_such(self):
        self.invoke("--dry-run")
        payload = json.loads(next(self.reports.glob("*.json")).read_text(encoding="utf-8"))
        self.assertTrue(payload["dry_run"])
        self.assertIn("dry run", next(self.reports.glob("*.md")).read_text(encoding="utf-8"))

    def test_no_report_file_writes_nothing(self):
        self.invoke("--no-report-file")
        self.assertFalse(self.reports.exists())

    def test_report_records_the_suppression_split(self):
        self.invoke()
        payload = json.loads(next(self.reports.glob("*.json")).read_text(encoding="utf-8"))
        reasons = [s["reason"] for s in payload["suppressions"]]
        self.assertEqual(len(reasons), 12)
        self.assertEqual(sum("already submitted this SellerProductId" in r for r in reasons), 1)
        self.assertEqual(sum("under another id" in r for r in reasons), 11)

    def test_report_records_the_discarded_values(self):
        self.invoke()
        payload = json.loads(next(self.reports.glob("*.json")).read_text(encoding="utf-8"))
        by_field: dict[str, int] = {}
        for item in payload["discarded_values"]:
            by_field[item["field"]] = by_field.get(item["field"], 0) + 1
        self.assertEqual(by_field, {"Name": 64, "Brand": 1, "Category": 1})

    def test_report_has_one_outcome_per_record(self):
        self.invoke()
        payload = json.loads(next(self.reports.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(len(payload["outcomes"]), 269)


class TestFailureModes(CliTestCase):
    def test_missing_input_file_exits_two(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(
                ["--database", str(self.db), "--input", str(self.tmp / "absent.json"), "--no-report-file"]
            )
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("cannot read", err.getvalue())
        self.assertEqual(self.counts(), (975, 0), "nothing was written")

    def test_missing_database_exits_two(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(
                ["--database", str(self.tmp / "absent.db"), "--input", str(ENTRIES), "--no-report-file"]
            )
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("database not found", err.getvalue())

    def test_malformed_input_exits_two(self):
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--database", str(self.db), "--input", str(bad), "--no-report-file"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("not valid JSON", err.getvalue())

    def test_a_rejected_record_exits_one_but_still_ingests_the_rest(self):
        payload = json.loads(ENTRIES.read_text(encoding="utf-8"))
        payload.append({"Id": "x", "SellerName": "MegaStore", "Name": None, "Brand": None, "Category": None})
        mixed = self.tmp / "mixed.json"
        mixed.write_text(json.dumps(payload), encoding="utf-8")

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(
                ["--database", str(self.db), "--input", str(mixed), "--reports-dir", str(self.reports)]
            )
        self.assertEqual(code, cli.EXIT_RECORD_FAILURES)
        self.assertIn("1 record(s) rejected", out.getvalue())
        self.assertEqual(self.counts(), (978, 257), "the good records still landed")

        report = json.loads(next(self.reports.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(report["summary"]["failed"], 1)
        self.assertEqual(report["summary"]["records_read"], 270)
        self.assertEqual(len(report["rejections"]), 1)
        self.assertIn("Name", report["rejections"][0]["reason"])

    def test_suppressing_duplicates_is_not_a_failure(self):
        self.invoke("--no-report-file")
        code, _, _ = self.invoke("--no-report-file")
        self.assertEqual(code, 0, "a run where every record is a duplicate still succeeded")


if __name__ == "__main__":
    unittest.main()
