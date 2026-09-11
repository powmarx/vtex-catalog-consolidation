"""Acceptance tests: the expected-outcome table from docs/DECISIONS.md.

These numbers were measured from the supplied artifacts before any code existed, so they
are a contract rather than a description of whatever the implementation happens to do.

If one of them fails, either the implementation is wrong or a decision needs revisiting.
The table is not to be edited to match the code.

    Input records                        269
    Matched to an existing product       266
    New Product rows inserted              3
    Product rows after ingest            978
    SellerProduct rows after ingest      257
    Records skipped as duplicates         12
    Distinct sellers linked               20
    Second run of the same file      0 new rows
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from catalog_consolidation import cli
from catalog_consolidation.models import Verdict

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "data" / "catalog.db"
ENTRIES = ROOT / "data" / "ProductEntry.json"

EXPECTED = {
    "records_read": 269,
    "matched": 266,
    "inserted": 3,
    "products_after": 978,
    "links_created": 257,
    "suppressed": 12,
    "failed": 0,
    "sellers_linked": 20,
}

INSERTED_NAMES = [
    "Processador AMD Ryzen 9 7950X",
    "Roteador WiFi 6 TP-Link",
    "Security Test Product",
]


class AcceptanceTestCase(unittest.TestCase):
    """Each test gets its own copy of the supplied catalog.

    The committed `data/catalog.db` is never opened for writing.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / "catalog.db"
        shutil.copy(CATALOG, self.db)

    def consolidate(self, *extra: str):
        with redirect_stdout(io.StringIO()):
            code = cli.main(["--database", str(self.db), "--input", str(ENTRIES), "--no-report-file", *extra])
        return code

    def report(self, *extra: str) -> dict:
        out = io.StringIO()
        with redirect_stdout(out):
            cli.main(
                [
                    "--database",
                    str(self.db),
                    "--input",
                    str(ENTRIES),
                    "--report",
                    "json",
                    "--no-report-file",
                    *extra,
                ]
            )
        return json.loads(out.getvalue())

    def query(self, sql: str, *params):
        conn = sqlite3.connect(f"file:{self.db.as_posix()}?mode=ro", uri=True)
        self.addCleanup(conn.close)
        return conn.execute(sql, params).fetchall()


class TestTheExpectedOutcomeTable(AcceptanceTestCase):
    def test_every_documented_number(self):
        summary = self.report()["summary"]
        for field, expected in EXPECTED.items():
            with self.subTest(field=field):
                self.assertEqual(summary[field], expected)

    def test_the_database_agrees_with_the_report(self):
        self.consolidate()
        self.assertEqual(self.query("SELECT count(*) FROM Product")[0][0], 978)
        self.assertEqual(self.query("SELECT count(*) FROM SellerProduct")[0][0], 257)
        self.assertEqual(self.query("SELECT count(DISTINCT SellerName) FROM SellerProduct")[0][0], 20)

    def test_exactly_these_three_products_were_inserted(self):
        """Ids follow sqlite_sequence, which sits at 975, in order of first appearance."""
        self.consolidate()
        self.assertEqual(
            self.query("SELECT Id, Name FROM Product WHERE Id > 975 ORDER BY Id"),
            [
                (976, "Roteador WiFi 6 TP-Link"),
                (977, "Processador AMD Ryzen 9 7950X"),
                (978, "Security Test Product"),
            ],
        )

    def test_the_inserted_set_matches_regardless_of_order(self):
        self.consolidate()
        names = {name for (name,) in self.query("SELECT Name FROM Product WHERE Id > 975")}
        self.assertEqual(names, set(INSERTED_NAMES))

    def test_accounting_adds_up(self):
        summary = self.report()["summary"]
        self.assertEqual(summary["matched"] + summary["inserted"], summary["records_read"])
        self.assertEqual(summary["products_after"] - summary["products_before"], summary["inserted"])
        self.assertEqual(summary["links_created"] + summary["suppressed"], summary["records_read"])

    def test_exit_code_is_zero(self):
        self.assertEqual(self.consolidate(), 0)


class TestIdempotency(AcceptanceTestCase):
    """D5: rerunning the same file must add nothing."""

    def rows(self):
        return (
            self.query("SELECT Id, Name, Brand, Category FROM Product ORDER BY Id"),
            self.query("SELECT Id, SellerName, ProductId, SellerProductId FROM SellerProduct ORDER BY Id"),
        )

    def test_second_run_adds_no_rows(self):
        self.consolidate()
        first = self.rows()
        self.consolidate()
        self.assertEqual(self.rows(), first)

    def test_second_run_suppresses_everything(self):
        self.consolidate()
        summary = self.report()["summary"]
        self.assertEqual(summary["inserted"], 0)
        self.assertEqual(summary["links_created"], 0)
        self.assertEqual(summary["suppressed"], 269)

    def test_third_run_is_also_a_no_op(self):
        self.consolidate()
        self.consolidate()
        third = self.rows()
        self.consolidate()
        self.assertEqual(self.rows(), third)


class TestExistingRowsAreNeverModified(AcceptanceTestCase):
    """D2: first write wins."""

    def test_all_975_pre_existing_rows_are_byte_identical_afterwards(self):
        before = self.query("SELECT Id, Name, Brand, Category FROM Product WHERE Id <= 975 ORDER BY Id")
        self.consolidate()
        after = self.query("SELECT Id, Name, Brand, Category FROM Product WHERE Id <= 975 ORDER BY Id")
        self.assertEqual(len(before), 975)
        self.assertEqual(before, after)

    def test_the_119_null_brands_stay_null(self):
        self.consolidate()
        nulls = self.query("SELECT count(*) FROM Product WHERE Id <= 975 AND Brand IS NULL")[0][0]
        self.assertEqual(nulls, 119)

    def test_the_category_disagreement_does_not_overwrite_the_catalog(self):
        # The input offers 'Photo' for Camera Canon EOS R6; the catalog says 'Photography'.
        self.consolidate()
        self.assertEqual(self.query("SELECT Category FROM Product WHERE Id = 18")[0][0], "Photography")
        self.assertEqual(self.query("SELECT count(*) FROM Product WHERE Category = 'Photo'")[0][0], 0)

    def test_the_apostrophe_brand_does_not_overwrite_the_catalog(self):
        # The input says "Levi's"; the catalog says 'Levis'. They match, and the catalog wins.
        self.consolidate()
        self.assertEqual(self.query("SELECT Brand FROM Product WHERE Id = 322")[0][0], "Levis")

    def test_the_discarded_values_are_reported_rather_than_lost(self):
        payload = self.report()
        by_field: dict[str, int] = {}
        for item in payload["discarded_values"]:
            by_field[item["field"]] = by_field.get(item["field"], 0) + 1
        self.assertEqual(by_field, {"Name": 64, "Brand": 1, "Category": 1})


class TestDuplicateSuppression(AcceptanceTestCase):
    """D5: both constraints, and which one does the work."""

    def test_the_twelve_suppressions_split_one_and_eleven(self):
        reasons = [s["reason"] for s in self.report()["suppressions"]]
        self.assertEqual(len(reasons), 12)
        self.assertEqual(sum("already submitted this SellerProductId" in r for r in reasons), 1)
        self.assertEqual(sum("under another id" in r for r in reasons), 11)

    def test_no_seller_is_recorded_twice_against_one_product(self):
        self.consolidate()
        duplicates = self.query(
            "SELECT SellerName, ProductId, count(*) FROM SellerProduct "
            "GROUP BY SellerName, ProductId HAVING count(*) > 1"
        )
        self.assertEqual(duplicates, [])

    def test_no_listing_id_is_recorded_twice_for_one_seller(self):
        self.consolidate()
        duplicates = self.query(
            "SELECT SellerName, SellerProductId, count(*) FROM SellerProduct "
            "GROUP BY SellerName, SellerProductId HAVING count(*) > 1"
        )
        self.assertEqual(duplicates, [])

    def test_a_shared_id_across_sellers_does_not_merge_products(self):
        # D4. One id covers Curtain Rod Adjustable (GardenStore) and Bookshelf 5-Shelf
        # (SportsHub); both must be linked to their own product.
        self.consolidate()
        rows = self.query(
            "SELECT sp.SellerName, p.Name FROM SellerProduct sp JOIN Product p ON p.Id = sp.ProductId "
            "WHERE sp.SellerProductId = ? ORDER BY sp.SellerName",
            "00112233-4455-4667-7889-900112233445",
        )
        self.assertEqual(
            rows,
            [("GardenStore", "Curtain Rod Adjustable"), ("SportsHub", "Bookshelf 5-Shelf")],
        )


class TestReviewCandidates(AcceptanceTestCase):
    """D11."""

    def test_exactly_the_two_translation_pairs_are_proposed(self):
        candidates = self.report()["review_candidates"]
        self.assertEqual(len(candidates), 2)
        proposed = {(c["inserted_name"], c["candidate_product_id"], round(c["overlap"], 3)) for c in candidates}
        self.assertEqual(
            proposed,
            {
                ("Processador AMD Ryzen 9 7950X", 28, 0.667),
                ("Roteador WiFi 6 TP-Link", 21, 0.6),
            },
        )

    def test_the_security_record_proposes_nothing(self):
        candidates = self.report()["review_candidates"]
        self.assertNotIn("Security Test Product", [c["inserted_name"] for c in candidates])

    def test_candidates_are_advisory_only(self):
        # Proposing a candidate must not merge anything.
        self.consolidate()
        self.assertEqual(self.query("SELECT count(*) FROM Product")[0][0], 978)
        self.assertEqual(self.query("SELECT count(*) FROM Product WHERE Id IN (21, 28)")[0][0], 2)


class TestSecurity(AcceptanceTestCase):
    """D7 and invariant 3: the payload is data, never code."""

    def test_the_injection_payload_round_trips_exactly(self):
        self.consolidate()
        rows = self.query("SELECT Name, Brand FROM Product WHERE Name = 'Security Test Product'")
        self.assertEqual(rows, [("Security Test Product", "TestBrand'; SELECT 1; --")])

    def test_nothing_was_executed(self):
        self.consolidate()
        self.assertEqual(self.query("PRAGMA integrity_check")[0][0], "ok")
        tables = {name for (name,) in self.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(tables, {"Product", "SellerProduct", "sqlite_sequence"})

    def test_the_three_malformed_ids_were_stored_verbatim(self):
        self.consolidate()
        for listing_id in (
            "ddddeee-ffff-4000-1111-222233334444",
            "09835342345-4678-9abc-def012345678",
            "uddd0000-eeee-4111-ffff-aaaa22223333",
        ):
            with self.subTest(listing_id=listing_id):
                rows = self.query(
                    "SELECT SellerProductId, typeof(SellerProductId) FROM SellerProduct WHERE SellerProductId = ?",
                    listing_id,
                )
                self.assertEqual(rows, [(listing_id, "text")])


class TestTransactionalIntegrity(AcceptanceTestCase):
    """D8 and DS5."""

    def digest(self) -> str:
        return hashlib.sha256(self.db.read_bytes()).hexdigest()

    def test_dry_run_leaves_the_file_byte_identical(self):
        before = self.digest()
        self.consolidate("--dry-run")
        self.assertEqual(self.digest(), before)

    def test_dry_run_reports_the_same_numbers_it_would_have_written(self):
        summary = self.report("--dry-run")["summary"]
        for field, expected in EXPECTED.items():
            with self.subTest(field=field):
                self.assertEqual(summary[field], expected)

    def test_a_failure_mid_run_leaves_nothing_behind(self):
        """An error must roll the whole transaction back, schema change included."""
        import catalog_consolidation.consolidator as consolidator_module

        before = self.digest()
        original = consolidator_module.find_review_candidates

        def explode(*_args, **_kwargs):
            raise RuntimeError("contrived failure after the inserts")

        consolidator_module.find_review_candidates = explode
        self.addCleanup(setattr, consolidator_module, "find_review_candidates", original)

        with self.assertRaises(RuntimeError):
            cli.run(self.db, ENTRIES)

        self.assertEqual(self.digest(), before, "a failed run must leave the file untouched")
        self.assertEqual(self.query("SELECT count(*) FROM Product")[0][0], 975)
        self.assertEqual(self.query("SELECT count(*) FROM SellerProduct")[0][0], 0)
        self.assertEqual(self.query("PRAGMA user_version")[0][0], 0, "the migration rolled back too")

    def test_the_committed_baseline_is_never_written_by_any_test(self):
        self.assertEqual(
            hashlib.sha256(CATALOG.read_bytes()).hexdigest(),
            "733ff1d9cc20253da48a9f8b33d7241503e4a06e7c68f65f7fa00ef14466c404",
        )


class TestPerRecordAudit(AcceptanceTestCase):
    """Every record accounted for, one outcome each."""

    def test_one_outcome_per_record(self):
        outcomes = self.report()["outcomes"]
        self.assertEqual(len(outcomes), 269)
        self.assertEqual([o["source_index"] for o in outcomes], list(range(269)))

    def test_verdict_counts_match_the_summary(self):
        payload = self.report()
        counts: dict[str, int] = {}
        for outcome in payload["outcomes"]:
            counts[outcome["verdict"]] = counts.get(outcome["verdict"], 0) + 1
        self.assertEqual(counts[Verdict.SUPPRESSED.value], payload["summary"]["suppressed"])
        self.assertEqual(counts[Verdict.INSERTED.value], payload["summary"]["inserted"])
        self.assertEqual(
            counts[Verdict.MATCHED.value] + counts[Verdict.INSERTED.value] + counts[Verdict.SUPPRESSED.value],
            269,
        )

    def test_every_outcome_names_a_product(self):
        for outcome in self.report()["outcomes"]:
            with self.subTest(index=outcome["source_index"]):
                self.assertIsNotNone(outcome["product_id"])


if __name__ == "__main__":
    unittest.main()
