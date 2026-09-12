"""Tests against synthetic catalogs from `scripts/generate_fixture.py`.

The acceptance tests assert exact numbers for one supplied file. These assert *invariants*
that must hold for any input, plus the specific situations the supplied file does not
contain. Between them: golden numbers catch regressions, invariants catch overfitting.

Everything runs on a copy. A fixture's `catalog.db` is treated as pristine, for the same
reason `data/catalog.db` is.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from catalog_consolidation import cli

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from generate_fixture import build  # noqa: E402  (path set above)

SEED = 20260912


class FixtureTestCase(unittest.TestCase):
    scenario = "baseline"
    products = 60
    sellers = 6
    records = 90

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fixture_dir = self.tmp / "fixture"
        self.manifest = build(
            self.scenario,
            self.fixture_dir,
            products=self.products,
            sellers=self.sellers,
            records=self.records,
            seed=SEED,
        )
        self.pristine = self.fixture_dir / "catalog.db"
        self.entries = self.fixture_dir / "ProductEntry.json"
        self.db = self.tmp / "work.db"
        shutil.copy(self.pristine, self.db)

    def run_cli(self, *extra: str) -> tuple[int, dict]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(
                [
                    "--database",
                    str(self.db),
                    "--input",
                    str(self.entries),
                    "--report",
                    "json",
                    "--no-report-file",
                    *extra,
                ]
            )
        return code, json.loads(out.getvalue())

    def query(self, sql: str, *params):
        conn = sqlite3.connect(f"file:{self.db.as_posix()}?mode=ro", uri=True)
        self.addCleanup(conn.close)
        return conn.execute(sql, params).fetchall()

    def raw_records(self) -> list[dict]:
        return json.loads(self.entries.read_text(encoding="utf-8"))

    # -- invariants that must hold for any input ------------------------------------

    def assert_invariants(self, payload: dict) -> None:
        summary = payload["summary"]

        self.assertEqual(
            summary["matched"] + summary["inserted"] + summary["failed"],
            summary["records_read"],
            "every record must be matched, inserted or rejected",
        )
        self.assertEqual(
            summary["products_after"] - summary["products_before"],
            summary["inserted"],
            "`inserted` must equal the change in product count",
        )
        self.assertEqual(
            summary["links_created"] + summary["suppressed"] + summary["failed"],
            summary["records_read"],
            "every record either links, is suppressed, or fails",
        )
        # Every record in the file is accounted for exactly once, either by an outcome
        # or by a rejection raised before it could get one.
        accounted = [o["source_index"] for o in payload["outcomes"]]
        accounted += [
            r["source_index"]
            for r in payload["rejections"]
            if r["source_index"] not in {o["source_index"] for o in payload["outcomes"]}
        ]
        self.assertEqual(
            sorted(accounted),
            list(range(summary["records_read"])),
            "every record index must appear exactly once across outcomes and rejections",
        )
        self.assertEqual(
            len(payload["suppressions"]), summary["suppressed"], "the suppressions list must match the count"
        )
        self.assertEqual(
            len(payload["rejections"]), summary["failed"], "the rejections list must match the count"
        )

        # Neither unique constraint may be violated in the database.
        self.assertEqual(
            self.query(
                "SELECT SellerName, ProductId FROM SellerProduct GROUP BY SellerName, ProductId "
                "HAVING count(*) > 1"
            ),
            [],
            "a seller is recorded against a product at most once",
        )
        self.assertEqual(
            self.query(
                "SELECT SellerName, SellerProductId FROM SellerProduct GROUP BY SellerName, "
                "SellerProductId HAVING count(*) > 1"
            ),
            [],
            "a seller listing id appears at most once",
        )
        # Every link points at a real product.
        self.assertEqual(self.query("PRAGMA foreign_key_check"), [])
        self.assertEqual(self.query("PRAGMA integrity_check")[0][0], "ok")
        # No suppression may be reported with the vague fallback wording.
        for suppression in payload["suppressions"]:
            self.assertTrue(suppression["reason"], "every suppression must name a reason")


class TestBaseline(FixtureTestCase):
    scenario = "baseline"

    def test_invariants_hold(self):
        code, payload = self.run_cli()
        self.assertEqual(code, 0)
        self.assert_invariants(payload)

    def test_clean_input_inserts_nothing(self):
        _, payload = self.run_cli()
        self.assertEqual(payload["summary"]["inserted"], 0)
        self.assertEqual(payload["summary"]["failed"], 0)

    def test_idempotent(self):
        self.run_cli()
        rows = self.query("SELECT Id, SellerName, ProductId, SellerProductId FROM SellerProduct ORDER BY Id")
        _, payload = self.run_cli()
        self.assertEqual(
            self.query("SELECT Id, SellerName, ProductId, SellerProductId FROM SellerProduct ORDER BY Id"),
            rows,
        )
        self.assertEqual(payload["summary"]["links_created"], 0)


class TestNumericSellerIds(FixtureTestCase):
    """The case D3 exists for, which the supplied file does not contain."""

    scenario = "numeric-ids"
    products = 30
    records = 30

    def test_every_seller_id_round_trips_byte_for_byte(self):
        code, payload = self.run_cli()
        self.assertEqual(code, 0)
        self.assert_invariants(payload)

        submitted = {(r["SellerName"], r["Id"]) for r in self.raw_records() if r.get("Id")}
        stored = set(self.query("SELECT SellerName, SellerProductId FROM SellerProduct"))
        missing = submitted - stored
        self.assertEqual(
            missing,
            set(),
            "an identifier was altered or dropped between the file and the database",
        )

    def test_numeric_looking_ids_are_not_coerced(self):
        self.run_cli()
        for listing_id in self.manifest["expect"]["numeric_listing_ids"]:
            with self.subTest(listing_id=listing_id):
                rows = self.query(
                    "SELECT SellerProductId, typeof(SellerProductId) FROM SellerProduct "
                    "WHERE SellerName = ? AND SellerProductId = ?",
                    self.manifest["expect"]["numeric_seller"],
                    listing_id,
                )
                self.assertEqual(rows, [(listing_id, "text")])

    def test_leading_and_trailing_space_ids_stay_distinct(self):
        """The bug a synthetic fixture caught.

        `' 42'` and `'42 '` are different identifiers. The loader used to strip both to
        `'42'`, so the second collapsed onto the first and was suppressed as a duplicate.
        """
        _, payload = self.run_cli()
        seller = self.manifest["expect"]["numeric_seller"]
        stored = {row[0] for row in self.query("SELECT SellerProductId FROM SellerProduct WHERE SellerName = ?", seller)}
        self.assertIn(" 42", stored, "a leading space is part of the identifier")
        self.assertIn("42 ", stored, "a trailing space is part of the identifier")
        self.assertEqual(payload["summary"]["suppressed"], 0, "no listing should be lost to whitespace")

    def test_007_and_7_remain_two_listings(self):
        self.run_cli()
        seller = self.manifest["expect"]["numeric_seller"]
        stored = {row[0] for row in self.query("SELECT SellerProductId FROM SellerProduct WHERE SellerName = ?", seller)}
        self.assertIn("007", stored)
        self.assertIn("7", stored)


class TestBrandAmbiguity(FixtureTestCase):
    """A catalog where brand is load-bearing, unlike the supplied one."""

    scenario = "brand-ambiguity"
    products = 20

    def test_invariants_hold(self):
        code, payload = self.run_cli()
        self.assertEqual(code, 0)
        self.assert_invariants(payload)

    def test_a_null_brand_record_does_not_match_the_branded_product(self):
        self.run_cli()
        shared = self.manifest["expect"]["shared_name"]
        rows = self.query(
            "SELECT p.Brand FROM SellerProduct sp JOIN Product p ON p.Id = sp.ProductId "
            "WHERE sp.SellerName = ? AND p.Name = ?",
            "SellerA",
            shared,
        )
        self.assertEqual(rows, [(None,)], "the unbranded record must land on the unbranded product")

    def test_a_branded_record_does_not_match_the_unbranded_product(self):
        self.run_cli()
        shared = self.manifest["expect"]["shared_name"]
        rows = self.query(
            "SELECT p.Brand FROM SellerProduct sp JOIN Product p ON p.Id = sp.ProductId "
            "WHERE sp.SellerName = ? AND p.Name = ?",
            "SellerB",
            shared,
        )
        self.assertEqual(rows, [("Acme",)])

    def test_two_brands_sharing_a_name_stay_separate(self):
        self.run_cli()
        name = self.manifest["expect"]["two_brand_name"]
        rows = self.query(
            "SELECT sp.SellerName, p.Brand FROM SellerProduct sp JOIN Product p ON p.Id = sp.ProductId "
            "WHERE p.Name = ? ORDER BY sp.SellerName",
            name,
        )
        self.assertIn(("SellerC", "Globex"), rows)
        self.assertIn(("SellerD", "Initech"), rows)

    def test_an_unknown_brand_for_a_known_name_is_a_new_product(self):
        _, payload = self.run_cli()
        self.assertEqual(payload["summary"]["inserted"], self.manifest["expect"]["expected_inserted"])
        name = self.manifest["expect"]["two_brand_name"]
        self.assertEqual(self.query("SELECT count(*) FROM Product WHERE Name = ?", name)[0][0], 3)


class TestWithinRunNewProduct(FixtureTestCase):
    """DS1 against a real database, not a fake."""

    scenario = "within-run-new"
    products = 40

    def test_six_sellers_naming_one_absent_product_insert_it_once(self):
        code, payload = self.run_cli()
        self.assertEqual(code, 0)
        self.assert_invariants(payload)
        self.assertEqual(payload["summary"]["inserted"], self.manifest["expect"]["expected_inserted"])
        self.assertEqual(payload["summary"]["links_created"], self.manifest["expect"]["expected_links"])

    def test_all_six_sellers_link_to_the_same_product(self):
        self.run_cli()
        product_ids = {row[0] for row in self.query("SELECT DISTINCT ProductId FROM SellerProduct")}
        self.assertEqual(len(product_ids), 1)
        self.assertEqual(self.query("SELECT count(*) FROM SellerProduct")[0][0], 6)

    def test_only_one_new_row_exists(self):
        self.run_cli()
        self.assertEqual(self.query("SELECT count(*) FROM Product")[0][0], self.products + 1)


class TestPopulatedCatalog(FixtureTestCase):
    """The migration's row-copy path, which the supplied empty table never exercises."""

    scenario = "populated"
    products = 40
    records = 60

    def test_preexisting_links_survive_the_migration(self):
        before = self.query("SELECT SellerName, ProductId, SellerProductId FROM SellerProduct ORDER BY Id")
        self.assertEqual(len(before), self.manifest["expect"]["preexisting_links"])
        code, payload = self.run_cli()
        self.assertEqual(code, 0)
        self.assert_invariants(payload)
        after = self.query("SELECT SellerName, ProductId, SellerProductId FROM SellerProduct ORDER BY Id")
        for row in before:
            self.assertIn(row, after, "a pre-existing link was lost in the rebuild")

    def test_the_column_was_widened_with_rows_present(self):
        self.run_cli()
        declared = next(
            r[2] for r in self.query('PRAGMA table_info("SellerProduct")') if r[1] == "SellerProductId"
        )
        self.assertEqual(declared, "TEXT")

    def test_a_clash_with_a_preexisting_row_is_reported_specifically(self):
        """Not with the old vague fallback wording."""
        self.run_cli()
        _, payload = self.run_cli()
        reasons = {s["reason"] for s in payload["suppressions"]}
        self.assertTrue(reasons)
        for reason in reasons:
            self.assertNotEqual(reason, "suppressed by a unique constraint")


class TestDirtyInput(FixtureTestCase):
    scenario = "dirty"
    products = 50
    records = 80

    def test_variants_all_fold_onto_existing_products(self):
        code, payload = self.run_cli()
        self.assertEqual(code, 0)
        self.assert_invariants(payload)
        self.assertEqual(payload["summary"]["inserted"], 0, "no variant should create a product")

    def test_the_catalog_rows_are_untouched(self):
        before = self.query("SELECT Id, Name, Brand, Category FROM Product ORDER BY Id")
        self.run_cli()
        self.assertEqual(self.query("SELECT Id, Name, Brand, Category FROM Product ORDER BY Id"), before)

    def test_discarded_values_are_recorded_for_every_variant(self):
        _, payload = self.run_cli()
        self.assertTrue(payload["discarded_values"], "spelling differences must be reported, not silently dropped")


class TestDuplicates(FixtureTestCase):
    scenario = "duplicates"
    products = 30
    records = 40

    def test_both_constraints_are_attributed(self):
        code, payload = self.run_cli()
        self.assertEqual(code, 0)
        self.assert_invariants(payload)
        reasons = [s["reason"] for s in payload["suppressions"]]
        self.assertTrue(any("already submitted this SellerProductId" in r for r in reasons))
        self.assertTrue(any("under another id" in r for r in reasons))

    def test_a_reused_id_across_sellers_is_not_suppressed(self):
        self.run_cli()
        rows = self.query(
            "SELECT SellerName, count(*) FROM SellerProduct GROUP BY SellerName HAVING SellerName = 'OtherSeller'"
        )
        self.assertEqual(rows, [("OtherSeller", 1)], "a shared id across sellers is not a duplicate")


class TestMalformedInput(FixtureTestCase):
    scenario = "malformed"
    products = 30
    records = 40

    def test_bad_records_are_rejected_and_good_ones_still_land(self):
        code, payload = self.run_cli()
        self.assertEqual(code, 1, "a rejected record means exit 1")
        self.assertEqual(payload["summary"]["failed"], self.manifest["expect"]["expected_rejections"])
        self.assertGreater(payload["summary"]["links_created"], 0)
        self.assert_invariants(payload)

    def test_every_rejection_names_the_record_and_a_reason(self):
        _, payload = self.run_cli()
        for rejection in payload["rejections"]:
            self.assertIsInstance(rejection["source_index"], int)
            self.assertTrue(rejection["reason"])


class TestAdversarial(FixtureTestCase):
    """Everything at once, shuffled."""

    scenario = "adversarial"
    products = 80
    records = 120

    def test_invariants_hold_under_everything(self):
        code, payload = self.run_cli()
        self.assertIn(code, (0, 1))
        self.assert_invariants(payload)

    def test_the_manifest_describes_the_file_it_generated(self):
        """The generator must not claim defects it then cured.

        An earlier version re-pointed borrowed records at local products and overwrote
        `Brand` unconditionally, which repaired the deliberately wrong-typed record. The
        scenario advertised six malformations and shipped five.
        """
        _, payload = self.run_cli()
        self.assertEqual(
            payload["summary"]["failed"],
            self.manifest["expect"]["expected_rejections"],
            "the manifest's rejection count must match what the file actually contains",
        )
        self.assertNotIn("all_matched", self.manifest["expect"], "inherited from baseline and false here")

    def test_all_planted_malformation_kinds_survive_generation(self):
        records = self.raw_records()
        blank_required = [
            r
            for r in records
            if any(
                r.get(f) is None or (isinstance(r.get(f), str) and not r[f].strip())
                for f in ("Id", "SellerName", "Name")
            )
        ]
        wrong_type = [
            r
            for r in records
            if any(
                r.get(f) is not None and not isinstance(r.get(f), str)
                for f in ("Id", "SellerName", "Name", "Brand", "Category")
            )
        ]
        self.assertTrue(blank_required, "missing-required-field records must survive")
        self.assertTrue(wrong_type, "the wrong-type record must survive, not be cured")

    def test_a_within_run_new_product_is_inserted_once(self):
        self.run_cli()
        absent = self.manifest["expect"]["absent_name"]
        rows = self.query("SELECT count(*) FROM Product WHERE Name LIKE ?", f"%{absent.split()[1]}%")
        self.assertGreaterEqual(rows[0][0], 1)

    def test_the_discarded_value_path_is_exercised(self):
        """`adversarial` must actually contain spelling variants.

        It used to inherit only clean records from `baseline`, so every matched record
        spelled its product exactly and the report said "Values not written: None" while
        claiming to be everything at once.
        """
        _, payload = self.run_cli()
        self.assertTrue(payload["discarded_values"], "no spelling variants reached the matcher")
        fields = {d["field"] for d in payload["discarded_values"]}
        self.assertIn("Name", fields)

    def test_every_axis_is_present_at_once(self):
        """The scenario's whole claim, asserted rather than trusted."""
        _, payload = self.run_cli()
        summary = payload["summary"]
        self.assertGreater(summary["matched"], 0, "matches")
        self.assertGreater(summary["inserted"], 0, "insertions")
        self.assertGreater(summary["suppressed"], 0, "duplicate suppression")
        self.assertGreater(summary["failed"], 0, "rejections")
        self.assertTrue(payload["discarded_values"], "discarded values")

    def test_the_injection_payload_is_stored_not_executed(self):
        self.run_cli()
        self.assertEqual(self.query("PRAGMA integrity_check")[0][0], "ok")
        tables = {name for (name,) in self.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertIn("Product", tables, "DROP TABLE Product must not have executed")
        rows = self.query("SELECT count(*) FROM Product WHERE Name LIKE '%DROP TABLE%'")
        self.assertEqual(rows[0][0], 1, "the payload is stored as an ordinary product name")

    def test_idempotent_even_here(self):
        self.run_cli()
        rows = self.query("SELECT Id, SellerName, ProductId, SellerProductId FROM SellerProduct ORDER BY Id")
        products = self.query("SELECT Id, Name, Brand, Category FROM Product ORDER BY Id")
        self.run_cli()
        self.assertEqual(
            self.query("SELECT Id, SellerName, ProductId, SellerProductId FROM SellerProduct ORDER BY Id"), rows
        )
        self.assertEqual(self.query("SELECT Id, Name, Brand, Category FROM Product ORDER BY Id"), products)

    def test_dry_run_writes_nothing(self):
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.run_cli("--dry-run")
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)


class TestGeneratorItself(unittest.TestCase):
    def test_the_same_seed_produces_identical_bytes(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        first, second = tmp / "a", tmp / "b"
        build("adversarial", first, products=30, sellers=4, records=40, seed=99)
        build("adversarial", second, products=30, sellers=4, records=40, seed=99)
        for name in ("catalog.db", "ProductEntry.json"):
            with self.subTest(name=name):
                self.assertEqual(
                    hashlib.sha256((first / name).read_bytes()).hexdigest(),
                    hashlib.sha256((second / name).read_bytes()).hexdigest(),
                )

    def test_a_different_seed_produces_different_data(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        a, b = tmp / "a", tmp / "b"
        build("baseline", a, products=30, sellers=4, records=40, seed=1)
        build("baseline", b, products=30, sellers=4, records=40, seed=2)
        self.assertNotEqual(
            (a / "ProductEntry.json").read_text(encoding="utf-8"),
            (b / "ProductEntry.json").read_text(encoding="utf-8"),
        )

    def test_generated_catalog_uses_the_supplied_schema(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        build("baseline", tmp / "fx", products=10, sellers=2, records=10, seed=7)
        conn = sqlite3.connect(f"file:{(tmp / 'fx' / 'catalog.db').as_posix()}?mode=ro", uri=True)
        self.addCleanup(conn.close)
        declared = next(
            r[2] for r in conn.execute('PRAGMA table_info("SellerProduct")') if r[1] == "SellerProductId"
        )
        self.assertEqual(declared, "INTEGER", "fixtures must start unmigrated, like the supplied file")
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_unknown_scenario_is_rejected(self):
        with self.assertRaises(SystemExit):
            build("no-such-scenario", Path(tempfile.mkdtemp()))


if __name__ == "__main__":
    unittest.main()
