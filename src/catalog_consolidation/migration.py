"""Schema migration. See D3, D5 and DS2 in the docs.

Two changes to the supplied schema, for different reasons and by different mechanisms.

`SellerProductId` INTEGER -> TEXT (D3)
--------------------------------------
Not because the data cannot be loaded otherwise -- it can. SQLite uses type affinity
rather than strict typing, so a UUID string offered to an INTEGER column is stored
unchanged and `typeof()` reports `text`. Ingestion would work with no migration at all,
quietly storing text in a column declared integer.

The reason is that integer affinity *rewrites* anything numeric-looking. A seller id of
`007` becomes `7`, `0012` becomes `12`, `1e3` becomes `1000`. Worse, `007` and `7`
then collide on the unique index below, so two genuinely different listings silently
become one. None of that bites on the supplied file, where every id is a non-numeric
UUID; the migration is a guarantee for the identifiers this schema invites.

SQLite cannot alter a column's declared type, so this rebuilds the table. Rows are
copied even though the supplied table is empty, so the migration stays correct against
a populated database.

Two unique indexes (D5)
-----------------------
`(SellerName, SellerProductId)` stops the same listing being linked twice.
`(SellerName, ProductId)` records a seller against a product once.

Both are needed and the second does most of the work: on the supplied file the first
suppresses 1 duplicate, the second suppresses 12. These are added in place --
`CREATE UNIQUE INDEX` needs no rebuild.

Idempotency
-----------
Guarded on `PRAGMA user_version`, which is 0 on the supplied file and 1 afterwards.
Everything runs in one transaction, so a failure leaves the original schema intact.
SQLite makes DDL and `user_version` transactional, which is also what lets `--dry-run`
roll the whole thing back (DS5).
"""

from __future__ import annotations

import sqlite3

__all__ = ["SCHEMA_VERSION", "current_version", "is_applied", "apply", "MigrationError"]

SCHEMA_VERSION = 1
"""`user_version` once this migration has run."""

LISTING_INDEX = "UX_SellerProduct_Listing"
OFFER_INDEX = "UX_SellerProduct_Offer"

_REBUILD_SELLERPRODUCT = """
CREATE TABLE SellerProduct_migrated (
    Id              INTEGER PRIMARY KEY AUTOINCREMENT,
    SellerName      TEXT NOT NULL,
    ProductId       INTEGER NOT NULL CONSTRAINT FK_Product_Id REFERENCES Product (Id),
    SellerProductId TEXT NOT NULL
)
"""


class MigrationError(Exception):
    """The database is not shaped the way this migration expects."""


def current_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def is_applied(connection: sqlite3.Connection) -> bool:
    return current_version(connection) >= SCHEMA_VERSION


def apply(connection: sqlite3.Connection) -> bool:
    """Bring the schema to `SCHEMA_VERSION`.

    Returns True if anything changed, False if it was already applied. Assumes the
    caller owns transaction control: it does not begin or commit, so the migration can
    share a transaction with the ingest and be rolled back with it (DS5, D8).
    """
    if is_applied(connection):
        return False

    _require_expected_shape(connection)

    if _declared_type(connection, "SellerProduct", "SellerProductId") != "TEXT":
        _widen_seller_product_id(connection)

    _create_unique_indexes(connection)

    # A literal is required: PRAGMA does not accept a bound parameter. The value is a
    # module constant, never user input, so there is nothing to interpolate unsafely.
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")
    return True


def _require_expected_shape(connection: sqlite3.Connection) -> None:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    missing = {"Product", "SellerProduct"} - tables
    if missing:
        raise MigrationError(f"expected table(s) not found: {', '.join(sorted(missing))}")


def _declared_type(connection: sqlite3.Connection, table: str, column: str) -> str | None:
    for row in connection.execute(f'PRAGMA table_info("{table}")'):
        if row[1] == column:
            return str(row[2]).upper()
    return None


def _widen_seller_product_id(connection: sqlite3.Connection) -> None:
    """Rebuild SellerProduct with SellerProductId as TEXT, preserving all rows."""
    before = connection.execute("SELECT count(*) FROM SellerProduct").fetchone()[0]

    connection.execute(_REBUILD_SELLERPRODUCT)
    connection.execute(
        "INSERT INTO SellerProduct_migrated (Id, SellerName, ProductId, SellerProductId) "
        "SELECT Id, SellerName, ProductId, SellerProductId FROM SellerProduct"
    )
    connection.execute("DROP TABLE SellerProduct")
    connection.execute("ALTER TABLE SellerProduct_migrated RENAME TO SellerProduct")

    after = connection.execute("SELECT count(*) FROM SellerProduct").fetchone()[0]
    if after != before:
        raise MigrationError(f"rebuild lost rows: {before} before, {after} after")


def _create_unique_indexes(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {LISTING_INDEX} ON SellerProduct (SellerName, SellerProductId)"
    )
    connection.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {OFFER_INDEX} ON SellerProduct (SellerName, ProductId)"
    )


def describe(connection: sqlite3.Connection) -> dict[str, object]:
    """Current migration state, for reporting and tests."""
    indexes = {
        row[1]
        for row in connection.execute('PRAGMA index_list("SellerProduct")')
        if not str(row[1]).startswith("sqlite_")
    }
    return {
        "user_version": current_version(connection),
        "applied": is_applied(connection),
        "seller_product_id_type": _declared_type(connection, "SellerProduct", "SellerProductId"),
        "unique_indexes": sorted(indexes),
    }
