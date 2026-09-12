"""Tests for the schema migration (D3, D5, DS2)."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from catalog_consolidation import migration

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "data" / "catalog.db"

SUPPLIED_SCHEMA = [
    "CREATE TABLE Product (Id INTEGER PRIMARY KEY AUTOINCREMENT, Name TEXT NOT NULL, "
    "Brand TEXT, Category TEXT)",
    "CREATE TABLE SellerProduct (Id INTEGER PRIMARY KEY AUTOINCREMENT, SellerName TEXT NOT NULL, "
    "ProductId INTEGER CONSTRAINT FK_Product_Id REFERENCES Product (Id) NOT NULL, "
    "SellerProductId INTEGER NOT NULL)",
]


class SuppliedSchemaTestCase(unittest.TestCase):
    """Base class providing a database with the schema exactly as supplied.

    Connections are registered for cleanup so the suite does not leak them; an
    unclosed sqlite3 connection raises a ResourceWarning and warnings in a passing
    test run are noise that hides the real ones.
    """

    def fresh_db(self, populate: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA foreign_keys = ON")
        for statement in SUPPLIED_SCHEMA:
            conn.execute(statement)
        if populate:
            conn.execute("INSERT INTO Product (Name, Brand, Category) VALUES ('Widget', 'Acme', 'Tools')")
            conn.execute(
                "INSERT INTO SellerProduct (SellerName, ProductId, SellerProductId) VALUES (?, ?, ?)",
                ("SellerOne", 1, "sp-001"),
            )
        return conn


def declared_type(conn: sqlite3.Connection, column: str) -> str:
    return next(r[2] for r in conn.execute('PRAGMA table_info("SellerProduct")') if r[1] == column)


def index_names(conn: sqlite3.Connection) -> set[str]:
    return {
        r[1]
        for r in conn.execute('PRAGMA index_list("SellerProduct")')
        if not str(r[1]).startswith("sqlite_")
    }


class TestApply(SuppliedSchemaTestCase):
    def test_supplied_schema_starts_unmigrated(self):
        conn = self.fresh_db()
        self.assertEqual(migration.current_version(conn), 0)
        self.assertFalse(migration.is_applied(conn))
        self.assertEqual(declared_type(conn, "SellerProductId"), "INTEGER")
        self.assertEqual(index_names(conn), set())

    def test_apply_widens_the_column_and_adds_both_indexes(self):
        conn = self.fresh_db()
        self.assertTrue(migration.apply(conn))
        self.assertEqual(declared_type(conn, "SellerProductId"), "TEXT")
        self.assertEqual(index_names(conn), {migration.LISTING_INDEX, migration.OFFER_INDEX})
        self.assertEqual(migration.current_version(conn), migration.SCHEMA_VERSION)
        self.assertTrue(migration.is_applied(conn))

    def test_apply_is_idempotent(self):
        conn = self.fresh_db()
        self.assertTrue(migration.apply(conn), "first call changes something")
        self.assertFalse(migration.apply(conn), "second call is a no-op")
        self.assertFalse(migration.apply(conn))
        self.assertEqual(index_names(conn), {migration.LISTING_INDEX, migration.OFFER_INDEX})
        self.assertEqual(migration.current_version(conn), 1)

    def test_rows_survive_the_rebuild(self):
        conn = self.fresh_db(populate=True)
        before = conn.execute("SELECT Id, SellerName, ProductId, SellerProductId FROM SellerProduct").fetchall()
        migration.apply(conn)
        after = conn.execute("SELECT Id, SellerName, ProductId, SellerProductId FROM SellerProduct").fetchall()
        self.assertEqual(before, after)
        self.assertEqual(len(after), 1)

    def test_foreign_key_survives_the_rebuild(self):
        conn = self.fresh_db(populate=True)
        migration.apply(conn)
        fks = list(conn.execute('PRAGMA foreign_key_list("SellerProduct")'))
        self.assertEqual(len(fks), 1)
        self.assertEqual(fks[0][2], "Product")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO SellerProduct (SellerName, ProductId, SellerProductId) VALUES (?, ?, ?)",
                ("SellerTwo", 9999, "sp-002"),
            )

    def test_autoincrement_survives_the_rebuild(self):
        conn = self.fresh_db(populate=True)
        migration.apply(conn)
        conn.execute(
            "INSERT INTO SellerProduct (SellerName, ProductId, SellerProductId) VALUES (?, ?, ?)",
            ("SellerTwo", 1, "sp-002"),
        )
        ids = [r[0] for r in conn.execute("SELECT Id FROM SellerProduct ORDER BY Id")]
        self.assertEqual(ids, [1, 2])

    def test_missing_tables_raise(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(conn.close)
        with self.assertRaises(migration.MigrationError):
            migration.apply(conn)

    def test_describe_reports_state(self):
        conn = self.fresh_db()
        self.assertEqual(
            migration.describe(conn),
            {
                "user_version": 0,
                "applied": False,
                "seller_product_id_type": "INTEGER",
                "unique_indexes": [],
            },
        )
        migration.apply(conn)
        self.assertEqual(
            migration.describe(conn),
            {
                "user_version": 1,
                "applied": True,
                "seller_product_id_type": "TEXT",
                "unique_indexes": [migration.LISTING_INDEX, migration.OFFER_INDEX],
            },
        )


class TestVersionIsVerifiedNotTrusted(SuppliedSchemaTestCase):
    """`user_version` is a bare integer anyone can set. It is a hint, not evidence.

    This is the failure that made the check necessary: a database claiming version 1
    without the D5 indexes ingested all 269 records, suppressed nothing, and exited 0.
    Duplicate suppression is the core requirement, so its absence must be loud.
    """

    def test_a_version_claim_without_the_indexes_is_refused(self):
        conn = self.fresh_db()
        conn.execute("PRAGMA user_version = 1")
        self.assertTrue(migration.is_applied(conn), "the claim is believed by is_applied")
        with self.assertRaises(migration.MigrationError) as ctx:
            migration.apply(conn)
        message = str(ctx.exception)
        self.assertIn(migration.LISTING_INDEX, message)
        self.assertIn(migration.OFFER_INDEX, message)

    def test_a_version_claim_with_only_one_index_is_refused(self):
        conn = self.fresh_db()
        conn.execute(
            f"CREATE UNIQUE INDEX {migration.LISTING_INDEX} ON SellerProduct (SellerName, SellerProductId)"
        )
        conn.execute("PRAGMA user_version = 1")
        with self.assertRaises(migration.MigrationError) as ctx:
            migration.apply(conn)
        message = str(ctx.exception)
        self.assertIn(migration.OFFER_INDEX, message)
        self.assertNotIn(migration.LISTING_INDEX, message, "only the missing one is named")

    def test_a_genuinely_migrated_database_passes_the_check(self):
        conn = self.fresh_db()
        migration.apply(conn)
        self.assertFalse(migration.apply(conn), "no false alarm on the real thing")

    def test_existing_rows_that_violate_the_new_index_raise_migration_error(self):
        # Not sqlite3.IntegrityError: cli.py does not import sqlite3, so an untranslated
        # failure here surfaces as a traceback instead of the documented exit code 2.
        conn = self.fresh_db()
        conn.execute("INSERT INTO Product (Name) VALUES ('P')")
        for listing_id in ("first", "second"):
            conn.execute(
                "INSERT INTO SellerProduct (SellerName, ProductId, SellerProductId) VALUES (?,?,?)",
                ("S", 1, listing_id),
            )
        with self.assertRaises(migration.MigrationError) as ctx:
            migration.apply(conn)
        self.assertIsInstance(ctx.exception.__cause__, sqlite3.IntegrityError)


class TestTextColumnBehaviour(SuppliedSchemaTestCase):
    """Why D3 widens the column, asserted rather than argued."""

    def test_integer_affinity_rewrites_numeric_looking_ids(self):
        conn = self.fresh_db()
        conn.execute("INSERT INTO Product (Name) VALUES ('P')")
        for supplied, mangled in [("007", 7), ("0012", 12), ("1e3", 1000)]:
            with self.subTest(supplied=supplied):
                conn.execute("DELETE FROM SellerProduct")
                conn.execute(
                    "INSERT INTO SellerProduct (SellerName, ProductId, SellerProductId) VALUES (?,?,?)",
                    ("S", 1, supplied),
                )
                stored = conn.execute("SELECT SellerProductId FROM SellerProduct").fetchone()[0]
                self.assertEqual(stored, mangled, "unmigrated column mangles the identifier")

    def test_after_migration_numeric_looking_ids_are_preserved(self):
        conn = self.fresh_db()
        migration.apply(conn)
        conn.execute("INSERT INTO Product (Name) VALUES ('P')")
        for supplied in ["007", "0012", "1e3", " 42"]:
            with self.subTest(supplied=supplied):
                conn.execute("DELETE FROM SellerProduct")
                conn.execute(
                    "INSERT INTO SellerProduct (SellerName, ProductId, SellerProductId) VALUES (?,?,?)",
                    ("S", 1, supplied),
                )
                row = conn.execute("SELECT SellerProductId, typeof(SellerProductId) FROM SellerProduct").fetchone()
                self.assertEqual(row, (supplied, "text"))

    def test_distinct_ids_no_longer_collide_after_migration(self):
        # '007' and '7' both coerce to 7 on an INTEGER column, so the unique index
        # would treat two different listings as one. This is the D5 failure D3 prevents.
        conn = self.fresh_db()
        migration.apply(conn)
        conn.execute("INSERT INTO Product (Name) VALUES ('P')")
        conn.execute("INSERT INTO Product (Name) VALUES ('Q')")
        conn.execute(
            "INSERT INTO SellerProduct (SellerName, ProductId, SellerProductId) VALUES (?,?,?)", ("S", 1, "007")
        )
        conn.execute(
            "INSERT INTO SellerProduct (SellerName, ProductId, SellerProductId) VALUES (?,?,?)", ("S", 2, "7")
        )
        self.assertEqual(conn.execute("SELECT count(*) FROM SellerProduct").fetchone()[0], 2)


class TestUniqueConstraints(SuppliedSchemaTestCase):
    """D5: idempotency enforced by the schema."""

    def setUp(self):
        self.conn = self.fresh_db()
        migration.apply(self.conn)
        self.conn.execute("INSERT INTO Product (Name) VALUES ('P')")
        self.conn.execute("INSERT INTO Product (Name) VALUES ('Q')")

    def _insert(self, seller: str, product_id: int, listing_id: str) -> int:
        return self.conn.execute(
            "INSERT INTO SellerProduct (SellerName, ProductId, SellerProductId) VALUES (?,?,?) "
            "ON CONFLICT DO NOTHING",
            (seller, product_id, listing_id),
        ).rowcount

    def test_same_listing_twice_is_suppressed(self):
        self.assertEqual(self._insert("S", 1, "x"), 1)
        self.assertEqual(self._insert("S", 1, "x"), 0)

    def test_same_seller_and_product_under_a_different_listing_id_is_suppressed(self):
        # The case that catches 11 of the 12 duplicates in the supplied file.
        self.assertEqual(self._insert("S", 1, "first"), 1)
        self.assertEqual(self._insert("S", 1, "second"), 0)

    def test_different_sellers_may_offer_the_same_product(self):
        self.assertEqual(self._insert("S", 1, "x"), 1)
        self.assertEqual(self._insert("T", 1, "x"), 1)

    def test_one_seller_may_offer_different_products(self):
        self.assertEqual(self._insert("S", 1, "x"), 1)
        self.assertEqual(self._insert("S", 2, "y"), 1)

    def test_on_conflict_do_nothing_still_raises_on_not_null(self):
        # DS3: this is why INSERT OR IGNORE is not used. A malformed record must be
        # distinguishable from a duplicate, not silently counted as one.
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO SellerProduct (SellerName, ProductId, SellerProductId) VALUES (?,?,?) "
                "ON CONFLICT DO NOTHING",
                (None, 1, "x"),
            )

    def test_insert_or_ignore_would_swallow_it(self):
        # The behaviour DS3 rejects, asserted so the reasoning stays checkable.
        rowcount = self.conn.execute(
            "INSERT OR IGNORE INTO SellerProduct (SellerName, ProductId, SellerProductId) VALUES (?,?,?)",
            (None, 1, "x"),
        ).rowcount
        self.assertEqual(rowcount, 0, "indistinguishable from a duplicate skip")


class TestAgainstTheSuppliedDatabase(unittest.TestCase):
    """The migration must work on the real file, and be rollback-safe (DS5)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "catalog.db"
        shutil.copy(CATALOG, self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_applies_cleanly_to_the_supplied_catalog(self):
        conn = sqlite3.connect(str(self.db), isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")
        self.assertTrue(migration.apply(conn))
        conn.execute("COMMIT")
        self.assertEqual(conn.execute("SELECT count(*) FROM Product").fetchone()[0], 975)
        self.assertEqual(declared_type(conn, "SellerProductId"), "TEXT")
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(list(conn.execute("PRAGMA foreign_key_check")), [])
        conn.close()

    def test_rollback_leaves_the_file_byte_identical(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        conn = sqlite3.connect(str(self.db), isolation_level=None)
        conn.execute("BEGIN")
        migration.apply(conn)
        conn.execute("INSERT INTO Product (Name) VALUES ('probe')")
        self.assertEqual(migration.current_version(conn), 1)
        conn.execute("ROLLBACK")
        self.assertEqual(migration.current_version(conn), 0, "user_version must roll back")
        self.assertEqual(declared_type(conn, "SellerProductId"), "INTEGER")
        self.assertEqual(index_names(conn), set())
        self.assertEqual(conn.execute("SELECT count(*) FROM Product").fetchone()[0], 975)
        conn.close()
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_the_committed_baseline_is_never_written(self):
        self.assertEqual(
            hashlib.sha256(CATALOG.read_bytes()).hexdigest(),
            "733ff1d9cc20253da48a9f8b33d7241503e4a06e7c68f65f7fa00ef14466c404",
        )


if __name__ == "__main__":
    unittest.main()
