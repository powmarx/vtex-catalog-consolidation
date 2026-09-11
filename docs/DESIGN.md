# Design

How the consolidation is built. *What* it does and *why* those semantics were chosen lives in [`DECISIONS.md`](DECISIONS.md); decision IDs `D1`–`D10` are referenced rather than restated. This document covers only design questions that `DECISIONS.md` deliberately leaves open.

## Scope

One command reads a JSON file of seller product submissions and consolidates it into an existing SQLite catalog: matching products that already exist, inserting those that do not, and recording which sellers offer each product.

Non-goals: no API, no concurrency beyond what a single transaction gives, no incremental or streaming ingest, no product data enrichment (`D2` forbids it). The assessment weights understanding over production readiness, so the design targets clarity and testability rather than throughput.

## Module layout

```
src/catalog_consolidation/
  __init__.py
  __main__.py        python -m catalog_consolidation
  cli.py             argument parsing, exit codes, report rendering
  models.py          SellerEntry, Product, MatchKey, Report - plain dataclasses
  normalize.py       the match key. Pure functions, no imports beyond stdlib
  source.py          JSON -> list[SellerEntry], per-record validation
  migration.py       schema migration, idempotent
  repository.py      every SQL statement in the project
  consolidator.py    orchestration; depends on abstractions, not sqlite3
tests/
```

The boundary that matters: **`repository.py` is the only module that imports `sqlite3`**, and `normalize.py` imports nothing. That makes the matching rule unit-testable without a database and the orchestration testable against a fake repository. `consolidator.py` holds the algorithm and knows nothing about SQL.

## Data flow

```mermaid
graph LR
    A[ProductEntry.json] -->|source.py| B[list of SellerEntry]
    C[(catalog.db)] -->|repository.load_index| D[match key -> ProductId]
    B --> E[consolidator]
    D --> E
    E -->|insert / link| F[repository]
    F --> C
    E --> G[Report]
```

## Design decisions

### DS1 — The catalog index is preloaded into memory, and mutated during the run

One `SELECT Id, Name, Brand FROM Product` builds a `dict[MatchKey, int]`. Lookups are then O(1) with no per-record round trip.

At 975 products and 269 records the performance argument is negligible; the reason is correctness and testability. A single read means the matching logic is a pure function of a dictionary, so it can be tested without SQL, and the whole comparison happens against one consistent snapshot.

**The index is updated as products are inserted.** Without this, two incoming records for the same genuinely-new product would each miss the index and insert twice, producing a duplicate — the exact failure the assessment asks to prevent. The supplied file happens to contain each new product only once, so a test must construct this case deliberately rather than rely on the fixture.

Rejected: querying per record. It scales to a catalog that does not fit in memory, which this one does by three orders of magnitude, and it would put the matching rule behind SQL where it is harder to test.

Cost, stated plainly: memory grows with catalog size. A real system would either query per record against an indexed normalized column, or store the normalized key as a generated column so the database does the matching.

### DS2 — Migration is versioned via `user_version` and idempotent

`PRAGMA user_version` is 0 on the supplied file. The migration sets it to 1 and refuses to re-run when it already reads 1, so applying it twice is a no-op.

Two distinct operations, needing different mechanisms:

- **`SellerProductId` to `TEXT` (`D3`).** SQLite cannot alter a column's declared type, so this rebuilds the table: create `SellerProduct_new` with the corrected schema, copy all rows, drop the original, rename. The table is empty in the supplied file, but the copy step is written and tested so the migration is correct against a populated database.
- **The unique constraints (`D5`).** `CREATE UNIQUE INDEX` adds these in place with no rebuild.

Both run inside one transaction, so a failure leaves the original schema intact.

### DS3 — Duplicate suppression uses `INSERT OR IGNORE` and its rowcount

Links are written with `INSERT OR IGNORE INTO SellerProduct (...)`. Verified: `rowcount` is 1 when the row is written and 0 when a unique index rejects it, which gives the skipped-listing count for free and with no read-before-write.

This is why `D5` puts idempotency in the schema rather than in application logic. A `SELECT` then `INSERT` would be two statements racing each other and would need the count tracked separately.

### DS4 — No default database path

`--database` is required. There is no default, so no invocation can silently mutate `data/catalog.db`, the pristine baseline committed per the README.

The intended workflow copies the baseline to an ignored working file:

```
copy data\catalog.db data\catalog.local.db
python -m catalog_consolidation --database data/catalog.local.db --input data/ProductEntry.json
```

`data/*.local.db` is already in `.gitignore`.

### DS5 — CLI surface

```
python -m catalog_consolidation --database PATH --input PATH [--dry-run] [--report {text,json}]
```

- `--dry-run` runs the full consolidation inside a transaction and rolls back, printing the report it would have produced. This makes the acceptance numbers observable without mutating anything.
- `--report json` emits the report as JSON for assertion in tests and by machines.
- Exit code 0 on success, 1 when any record failed, 2 on a usage or I/O error. A run that skips duplicates is a success, not a failure — skipping is the specified behaviour.

### DS6 — The report is a value object, not print statements

`consolidator.consolidate(...)` returns a `Report` dataclass; `cli.py` renders it. The orchestration prints nothing, which is what lets the integration test assert on the acceptance numbers directly rather than parsing stdout.

```
records read              269
matched existing product   266
products inserted            3
links created              257
duplicate listings skipped  12
records failed               0
```

### DS7 — Per-record errors are collected, not raised

Per `D7`, a malformed record is recorded in `Report.errors` with its index and reason, and the run continues. This sits inside `D8`'s single transaction: the batch still commits atomically, and errored records simply contribute nothing.

Deliberate consequence: a file where every record fails commits successfully with zero rows written and exit code 1. That is correct — nothing was wrong with the transaction, only with the data.

## Test strategy

The acceptance numbers in `DECISIONS.md` are the primary test, and they are known before the code exists, so they can be written first.

| Test | Asserts |
| --- | --- |
| `test_normalize` | table-driven over the real variations: doubled spaces, `Câmera`/`Camera`, `Levi's`/`Levis`, `34"`/`34`, `12.9"`/`12.9''`, `None` brand (`D1`) |
| `test_normalize_is_lossless` | the key yields 975 distinct values over the supplied catalog, so no two products merge (`D1`) |
| `test_migration_idempotent` | applying twice is a no-op; `user_version` reaches 1; rows survive a rebuild on a populated table (`DS2`) |
| `test_new_product_deduped_within_run` | two records for the same absent product insert one row, not two (`DS1`) |
| `test_id_not_unique_across_sellers` | one `Id` shared by two sellers on different products yields two products and two links (`D4`) |
| `test_consolidate_acceptance` | full run on a copy of the supplied catalog reproduces 269/266/3/978/257/12 exactly |
| `test_idempotent_second_run` | rerunning the same file adds zero rows to either table (`D5`) |
| `test_injection_record_stored_literally` | the `TestBrand'; SELECT 1; --` brand round-trips as an exact string and the schema is unchanged (`D7`) |
| `test_dry_run_mutates_nothing` | file hash is identical after `--dry-run` (`DS5`) |

Integration tests copy `data/catalog.db` to a temporary path per test. The committed baseline is never opened for writing.

Standard library `unittest` is sufficient and keeps the core path dependency-free, consistent with the conventions in the README.
