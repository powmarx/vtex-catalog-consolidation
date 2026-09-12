"""All database access. See D8, DS1, DS3 and DS8 in the docs.

This is the only module in the package that imports `sqlite3`. Everything above it
depends on the `CatalogRepository` interface rather than on SQL, which is what lets the
consolidation algorithm be tested against a fake with no database at all.

Three SQLite behaviours shape this module, each verified rather than assumed:

`PRAGMA foreign_keys` is silently ignored inside a transaction (DS8)
    Python's `sqlite3` opens an implicit transaction on the first data-modifying
    statement under its default `isolation_level`. A pragma issued after that point
    reports success and does nothing, and foreign key violations then insert happily.
    So the connection is opened with `isolation_level=None`, the pragma is the first
    statement executed, and the result is read back and checked.

Transaction boundaries must be explicit (D8)
    With `isolation_level=None` there is no implicit transaction, so `BEGIN`, `COMMIT`
    and `ROLLBACK` are issued deliberately. The whole run is one transaction, which is
    also what makes `--dry-run` possible: SQLite makes DDL and `user_version`
    transactional, so a rolled-back run leaves the file byte-identical.

`INSERT OR IGNORE` is the wrong tool (DS3)
    It suppresses *every* constraint violation, not just uniqueness. A row failing
    NOT NULL is discarded with `rowcount = 0`, indistinguishable from a duplicate
    skip -- so a malformed record would be counted as a duplicate and never reported.
    `ON CONFLICT DO NOTHING` absorbs uniqueness conflicts and raises on NOT NULL,
    keeping the two cases apart.

Every statement is parameterized. The only interpolated values anywhere in the package
are module-level constants in `migration.py`, where PRAGMA syntax forbids a placeholder.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from .models import MatchKey, Product
from .normalize import match_key

__all__ = [
    "CatalogRepository",
    "SqliteCatalogRepository",
    "RepositoryError",
    "RepositoryIntegrityError",
    "connect",
    "open_catalog",
]


class RepositoryError(Exception):
    """The database could not be opened, or is not in a state we can work with.

    `sqlite3` exceptions are translated into this so that no module above the
    repository has to import `sqlite3`. The original is kept as `__cause__`.
    """


class RepositoryIntegrityError(RepositoryError):
    """A constraint refused a write.

    Separate from `RepositoryError` because it is usually a problem with one record
    rather than with the database, so the consolidator can report it and carry on
    instead of losing the whole batch (D7). A SQLite constraint failure does not
    invalidate the surrounding transaction, so continuing is safe.
    """


class CatalogRepository(Protocol):
    """What the consolidator needs from storage.

    Kept deliberately small. A fake implementing these five methods is enough to test
    the whole algorithm, which is why the interface exists.
    """

    def load_index(self) -> dict[MatchKey, int]:
        """Every catalog product keyed by its match key. One query. See DS1."""

    def product_count(self) -> int: ...

    def fetch_product(self, product_id: int) -> Product | None: ...

    def insert_product(self, name: str, brand: str | None, category: str | None) -> int:
        """Insert and return the new product id."""

    def link(self, seller_name: str, product_id: int, seller_product_id: str) -> bool:
        """Link a seller to a product. False if a unique index suppressed it."""


def connect(database: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a connection with the pragmas and transaction model this project needs.

    Ordering matters: `PRAGMA foreign_keys` must be the first statement on a fresh
    connection, before anything can open an implicit transaction. See DS8.
    """
    path = Path(database)
    try:
        if read_only:
            connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, isolation_level=None)
        else:
            if not path.exists():
                raise RepositoryError(f"database not found: {path}")
            connection = sqlite3.connect(str(path), isolation_level=None)
    except sqlite3.Error as exc:
        raise RepositoryError(f"cannot open {path}: {exc}") from exc

    connection.execute("PRAGMA foreign_keys = ON")
    enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    if not enabled:
        connection.close()
        raise RepositoryError(
            "PRAGMA foreign_keys did not take effect. It is silently ignored inside a "
            "transaction, so it must be set on a fresh connection before any other statement."
        )
    return connection


@contextmanager
def open_catalog(database: str | Path, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
    """A connection that is always closed, even on failure."""
    connection = connect(database, read_only=read_only)
    try:
        yield connection
    finally:
        connection.close()


class SqliteCatalogRepository:
    """`CatalogRepository` over a SQLite connection.

    Does not own the connection: the caller opens it, controls the transaction, and
    closes it. That is what lets the migration and the ingest share one transaction.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    # -- statement execution -----------------------------------------------------

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Run a statement, translating SQLite failures at this boundary.

        Every statement goes through here so that no module above the repository has to
        know about `sqlite3`. That boundary used to hold only at import time: a test
        asserts no other module imports `sqlite3`, but `begin`, `commit`, the reads and
        the whole migration issued bare `execute()` calls, so an `OperationalError` from a
        locked database travelled straight past the CLI's handlers and surfaced as a
        traceback with exit code 1 -- where the documented contract promises exit 2.
        """
        try:
            return self._connection.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            raise RepositoryIntegrityError(str(exc)) from exc
        except sqlite3.Error as exc:
            raise RepositoryError(f"{type(exc).__name__}: {exc}") from exc

    # -- transaction control (D8) ------------------------------------------------

    def begin(self) -> None:
        self._execute("BEGIN")

    def commit(self) -> None:
        self._execute("COMMIT")

    def rollback(self) -> None:
        self._execute("ROLLBACK")

    @property
    def in_transaction(self) -> bool:
        return self._connection.in_transaction

    @contextmanager
    def transaction(self, *, commit: bool = True) -> Iterator[None]:
        """Run a block in one transaction.

        `commit=False` rolls back on success as well as on failure, which is how
        `--dry-run` works (DS5). Any exception always rolls back and propagates.
        """
        self.begin()
        try:
            yield
        except BaseException:
            self.rollback()
            raise
        if commit:
            self.commit()
        else:
            self.rollback()

    # -- reads -------------------------------------------------------------------

    def load_index(self) -> dict[MatchKey, int]:
        """Build the match-key index from a single pass over the catalog (DS1).

        At 975 products the performance argument is negligible; the reason is that one
        read gives a consistent snapshot and keeps the matching rule a pure function of
        a dictionary, testable without SQL.
        """
        index: dict[MatchKey, int] = {}
        for product_id, name, brand in self._execute("SELECT Id, Name, Brand FROM Product"):
            index[match_key(name, brand)] = product_id
        return index

    def product_count(self) -> int:
        return int(self._execute("SELECT count(*) FROM Product").fetchone()[0])

    def link_count(self) -> int:
        return int(self._execute("SELECT count(*) FROM SellerProduct").fetchone()[0])

    def fetch_product(self, product_id: int) -> Product | None:
        row = self._execute("SELECT Id, Name, Brand, Category FROM Product WHERE Id = ?", (product_id,)).fetchone()
        return Product(*row) if row else None

    def iter_products(self) -> Iterator[Product]:
        """Every catalog product. Used by the D11 candidate search."""
        for row in self._execute("SELECT Id, Name, Brand, Category FROM Product"):
            yield Product(*row)

    # -- writes ------------------------------------------------------------------

    def insert_product(self, name: str, brand: str | None, category: str | None) -> int:
        try:
            cursor = self._execute(
                "INSERT INTO Product (Name, Brand, Category) VALUES (?, ?, ?)",
                (name, brand, category),
            )
        except RepositoryIntegrityError as exc:
            # Re-raise with context, keeping the sqlite3 exception as the root cause
            # rather than nesting one RepositoryIntegrityError inside another.
            raise RepositoryIntegrityError(f"cannot insert product {name!r}: {exc}") from exc.__cause__
        new_id = cursor.lastrowid
        if new_id is None:  # pragma: no cover - sqlite always supplies this for a rowid table
            raise RepositoryError("insert did not yield a product id")
        return int(new_id)

    def link(self, seller_name: str, product_id: int, seller_product_id: str) -> bool:
        """Record that a seller offers a product.

        Returns True if a row was written, False if a unique index suppressed it as a
        duplicate. `ON CONFLICT DO NOTHING` rather than `INSERT OR IGNORE`, so a
        NOT NULL violation raises instead of being miscounted as a duplicate (DS3).

        Note on `AUTOINCREMENT`: a suppressed insert still consumes a rowid, so
        `sqlite_sequence` advances even when no row is written. Re-ingesting a file
        inflates that counter without changing the data. Harmless at any realistic
        scale, but it is why idempotency is asserted on row content rather than on the
        file's bytes.
        """
        try:
            cursor = self._execute(
                "INSERT INTO SellerProduct (SellerName, ProductId, SellerProductId) "
                "VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                (seller_name, product_id, seller_product_id),
            )
        except RepositoryIntegrityError as exc:
            # As in `insert_product`: keep the sqlite3 exception as the root cause rather
            # than nesting one RepositoryIntegrityError inside another.
            raise RepositoryIntegrityError(
                f"cannot link seller {seller_name!r} to product {product_id}: {exc}"
            ) from exc.__cause__
        return cursor.rowcount == 1
