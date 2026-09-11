"""Tests for the repository (D8, DS1, DS3, DS8)."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from catalog_consolidation import migration
from catalog_consolidation.models import Product
from catalog_consolidation.normalize import match_key
from catalog_consolidation.repository import (
    RepositoryError,
    SqliteCatalogRepository,
    connect,
    open_catalog,
)

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "data" / "catalog.db"


class RepositoryTestCase(unittest.TestCase):
    """A migrated copy of the supplied catalog, on disk, cleaned up afterwards."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / "catalog.db"
        shutil.copy(CATALOG, self.db)
        self.connection = connect(self.db)
        self.addCleanup(self.connection.close)
        self.repo = SqliteCatalogRepository(self.connection)
        with self.repo.transaction():
            migration.apply(self.connection)


class TestConnectionSetup(unittest.TestCase):
    """DS8: the pragma ordering that a naive implementation gets wrong."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / "catalog.db"
        shutil.copy(CATALOG, self.db)

    def test_foreign_keys_are_actually_enabled(self):
        conn = connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_no_implicit_transaction_is_open(self):
        conn = connect(self.db)
        self.addCleanup(conn.close)
        self.assertFalse(conn.in_transaction, "isolation_level=None means we control BEGIN")

    def test_the_naive_ordering_would_have_failed_silently(self):
        # Why connect() sets the pragma first. Left as a test so the reasoning in DS8
        # stays checkable rather than becoming folklore.
        conn = sqlite3.connect(str(self.db))
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO Product (Name) VALUES ('opens a transaction')")
        self.assertTrue(conn.in_transaction)
        conn.execute("PRAGMA foreign_keys = ON")
        self.assertEqual(
            conn.execute("PRAGMA foreign_keys").fetchone()[0],
            0,
            "pragma is ignored inside a transaction, and reports no error",
        )
        conn.rollback()

    def test_foreign_keys_are_enforced_in_practice(self):
        conn = connect(self.db)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO SellerProduct (SellerName, ProductId, SellerProductId) VALUES (?,?,?)",
                ("S", 999_999, "x"),
            )

    def test_missing_database_raises(self):
        with self.assertRaises(RepositoryError):
            connect(self.tmp / "absent.db")

    def test_read_only_connection_refuses_writes(self):
        with open_catalog(self.db, read_only=True) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO Product (Name) VALUES ('nope')")

    def test_open_catalog_closes_on_exception(self):
        try:
            with open_catalog(self.db) as conn:
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        with self.assertRaises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


class TestReads(RepositoryTestCase):
    def test_load_index_covers_every_product_exactly_once(self):
        index = self.repo.load_index()
        self.assertEqual(self.repo.product_count(), 975)
        self.assertEqual(len(index), 975, "no two products may share a match key")

    def test_load_index_is_keyed_by_match_key(self):
        index = self.repo.load_index()
        self.assertEqual(index[match_key("Router WiFi 6 TP-Link", "TP-Link")], 21)
        self.assertEqual(index[match_key("Processor AMD Ryzen 9 7950X", "AMD")], 28)

    def test_index_lookup_folds_incoming_spelling_variants(self):
        # The index is built from catalog spellings; a lookup with the seller's
        # spelling must still land on the right product.
        index = self.repo.load_index()
        # Catalog holds 'Levis'; the input says "Levi's".
        self.assertEqual(index[match_key("Belt Leather Reversible", "Levi's")], 322)
        # Catalog holds 'Camera'; the input says 'Câmera'.
        self.assertEqual(index[match_key("Câmera Canon EOS R6", "Canon")], 18)
        # Doubled internal space.
        self.assertEqual(index[match_key("Smartphone  Galaxy S23", "Samsung")], index[match_key("Smartphone Galaxy S23", "Samsung")])

    def test_null_brand_products_are_keyed_on_the_empty_string(self):
        index = self.repo.load_index()
        self.assertEqual(index[("cable organizer kit", "")], 113)

    def test_fetch_product(self):
        self.assertEqual(
            self.repo.fetch_product(21),
            Product(21, "Router WiFi 6 TP-Link", "TP-Link", "Networking"),
        )
        self.assertIsNone(self.repo.fetch_product(999_999))

    def test_iter_products_yields_all(self):
        products = list(self.repo.iter_products())
        self.assertEqual(len(products), 975)
        self.assertTrue(all(isinstance(p, Product) for p in products))

    def test_link_count_starts_empty(self):
        self.assertEqual(self.repo.link_count(), 0)


class TestWrites(RepositoryTestCase):
    def test_insert_product_returns_the_new_id(self):
        with self.repo.transaction():
            new_id = self.repo.insert_product("Brand New Thing", "Acme", "Tools")
        self.assertEqual(new_id, 976, "follows sqlite_sequence, which sits at 975")
        self.assertEqual(self.repo.fetch_product(976).name, "Brand New Thing")
        self.assertEqual(self.repo.product_count(), 976)

    def test_insert_preserves_values_verbatim(self):
        # D2/D11: nothing is normalized on the way in.
        with self.repo.transaction():
            new_id = self.repo.insert_product("Câmera  Dupla", "Levi's", None)
        product = self.repo.fetch_product(new_id)
        self.assertEqual(product.name, "Câmera  Dupla")
        self.assertEqual(product.brand, "Levi's")
        self.assertIsNone(product.category)

    def test_injection_payload_is_stored_as_a_literal(self):
        payload = "TestBrand'; SELECT 1; --"
        with self.repo.transaction():
            new_id = self.repo.insert_product("Security Test Product", payload, "Electronics")
        self.assertEqual(self.repo.fetch_product(new_id).brand, payload)
        # And it executed nothing: the schema and row count are untouched apart from
        # the one insert.
        self.assertEqual(self.repo.product_count(), 976)
        self.assertEqual(self.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_link_returns_true_then_false_for_the_same_listing(self):
        with self.repo.transaction():
            self.assertTrue(self.repo.link("MegaStore", 21, "listing-1"))
            self.assertFalse(self.repo.link("MegaStore", 21, "listing-1"))
        self.assertEqual(self.repo.link_count(), 1)

    def test_link_is_suppressed_for_the_same_seller_and_product_under_a_new_id(self):
        # D5's second constraint: the one that catches 11 of the 12 duplicates.
        with self.repo.transaction():
            self.assertTrue(self.repo.link("MegaStore", 21, "listing-1"))
            self.assertFalse(self.repo.link("MegaStore", 21, "listing-2"))
        self.assertEqual(self.repo.link_count(), 1)

    def test_different_sellers_may_offer_the_same_product(self):
        with self.repo.transaction():
            self.assertTrue(self.repo.link("MegaStore", 21, "a"))
            self.assertTrue(self.repo.link("TechWorld", 21, "b"))
        self.assertEqual(self.repo.link_count(), 2)

    def test_link_stores_the_seller_id_as_text(self):
        with self.repo.transaction():
            self.repo.link("MegaStore", 21, "007")
        row = self.connection.execute(
            "SELECT SellerProductId, typeof(SellerProductId) FROM SellerProduct"
        ).fetchone()
        self.assertEqual(row, ("007", "text"), "D3: the migration prevents coercion to 7")

    def test_link_raises_rather_than_swallowing_a_not_null_violation(self):
        # DS3. If this ever returns False instead, a malformed record is being
        # miscounted as a duplicate.
        with self.assertRaises(sqlite3.IntegrityError):
            with self.repo.transaction():
                self.repo.link(None, 21, "x")  # type: ignore[arg-type]

    def test_link_raises_on_a_missing_product(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.repo.transaction():
                self.repo.link("MegaStore", 999_999, "x")


class TestTransactionControl(RepositoryTestCase):
    def test_commit_persists(self):
        with self.repo.transaction():
            self.repo.insert_product("Kept", None, None)
        self.assertEqual(self.repo.product_count(), 976)

    def test_rollback_on_exception_discards_everything(self):
        with self.assertRaises(RuntimeError):
            with self.repo.transaction():
                self.repo.insert_product("Discarded", None, None)
                self.repo.link("MegaStore", 21, "x")
                raise RuntimeError("boom")
        self.assertEqual(self.repo.product_count(), 975)
        self.assertEqual(self.repo.link_count(), 0)

    def test_commit_false_rolls_back_on_success(self):
        # How --dry-run works (DS5).
        with self.repo.transaction(commit=False):
            self.repo.insert_product("Never kept", None, None)
            self.repo.link("MegaStore", 21, "x")
        self.assertEqual(self.repo.product_count(), 975)
        self.assertEqual(self.repo.link_count(), 0)

    def test_transaction_closes_cleanly(self):
        self.assertFalse(self.repo.in_transaction)
        with self.repo.transaction():
            self.assertTrue(self.repo.in_transaction)
        self.assertFalse(self.repo.in_transaction)

    def test_foreign_keys_survive_a_rollback(self):
        with self.repo.transaction(commit=False):
            self.repo.insert_product("x", None, None)
        self.assertEqual(self.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)


class TestOnlyThisModuleTouchesSqlite(unittest.TestCase):
    """DS design boundary: the package must not import sqlite3 anywhere else."""

    def test_no_other_module_imports_sqlite3(self):
        package = ROOT / "src" / "catalog_consolidation"
        offenders = []
        for path in sorted(package.glob("*.py")):
            if path.name in {"repository.py", "migration.py"}:
                continue  # migration.py takes a connection and type-hints it
            if "sqlite3" in path.read_text(encoding="utf-8"):
                offenders.append(path.name)
        self.assertEqual(offenders, [], "only repository.py and migration.py may reference sqlite3")


if __name__ == "__main__":
    unittest.main()
