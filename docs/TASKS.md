# Tasks

Ordered implementation plan. Each task names what proves it done. Semantics come from [`DECISIONS.md`](DECISIONS.md) (`D1`–`D10`), structure from [`DESIGN.md`](DESIGN.md) (`DS1`–`DS7`).

Order is chosen so that every step is verifiable on its own, and so the two hardest things — the match key and the migration — are settled before anything depends on them.

## 1. Package skeleton and models

- [ ] `src/catalog_consolidation/` with `__init__.py`, `__main__.py`
- [ ] `models.py`: `SellerEntry`, `Product`, `Report`, `RecordError` as frozen dataclasses
- [ ] `pyproject.toml` declaring the package, no runtime dependencies
- [ ] Python floor set to 3.10, the earliest version supporting the `X | Y` type syntax used in signatures. Nothing in the design needs anything newer; developed against 3.13

**Done when:** `python -c "import catalog_consolidation"` succeeds and `python -m unittest discover tests` runs with zero tests collected and no import errors.

## 2. The match key (`D1`)

- [ ] `normalize.py`: `normalize(value: str | None) -> str` and `match_key(name, brand) -> tuple[str, str]`
- [ ] NFKD accent stripping, lowercase, drop non-alphanumeric-non-space characters **without substituting a space**, collapse whitespace
- [ ] `None` maps to `""` and is a value, not a wildcard

**Tests:** `test_normalize` table-driven over the real variations — `Smartphone  Galaxy S23`/`Smartphone Galaxy S23`, `Câmera`/`Camera`, `Levi's`/`Levis`, `Monitor LG UltraWide 34"`/`...34`, `Tablet iPad Pro 12.9"`/`12.9''`, `None` brand. Plus `test_normalize_is_lossless`: 975 distinct keys over the supplied catalog.

**Done when:** both tests pass. This is the highest-risk logic in the project, and it is pure, so it needs no database.

## 3. Input loading (`D7`)

- [ ] `source.py`: `load_entries(path) -> tuple[list[SellerEntry], list[RecordError]]`
- [ ] Missing or blank `Name` or `SellerName` becomes a `RecordError` carrying the record index; the record is dropped and loading continues
- [ ] `Id` is opaque — no UUID validation, so the malformed `09835342345-4678-9abc-def012345678` loads normally

**Done when:** the supplied file yields 269 entries and 0 errors, and a fixture with a missing `Name` yields an error rather than an exception.

## 4. Migration (`D3`, `D5`, `DS2`)

- [ ] `migration.py`: `apply(connection) -> bool`, returning whether it changed anything
- [ ] Guard on `PRAGMA user_version`; set to 1 on success
- [ ] Rebuild `SellerProduct` with `SellerProductId TEXT NOT NULL`, copying existing rows
- [ ] `CREATE UNIQUE INDEX` for `(SellerName, SellerProductId)` and `(SellerName, ProductId)`
- [ ] Whole migration in one transaction

**Tests:** `test_migration_idempotent` — apply twice, second is a no-op, `user_version` is 1, declared type is `TEXT`, both indexes exist, and rows in a pre-populated table survive the rebuild.

**Done when:** those pass and `PRAGMA integrity_check` still returns `ok` afterwards.

## 5. Repository (`D8`, `DS1`, `DS3`)

- [ ] `repository.py`, the only module importing `sqlite3`
- [ ] Connect with `isolation_level=None` and issue `PRAGMA foreign_keys = ON` as the **first** statement, then assert it reads back as 1 (`DS8` — the pragma is silently ignored inside a transaction)
- [ ] Explicit `BEGIN` / `COMMIT` / `ROLLBACK`; no reliance on implicit transactions
- [ ] `load_index() -> dict[tuple[str, str], int]` from one `SELECT`
- [ ] `insert_product(name, brand, category) -> int`
- [ ] `link(...) -> bool` via `INSERT ... ON CONFLICT DO NOTHING`, returning whether a row was written. **Not `INSERT OR IGNORE`** (`DS3`)
- [ ] Every statement parameterized; no f-strings or concatenation in SQL

**Tests:** `link` returns `True` then `False` for the same pair; `insert_product` round-trips a brand containing `'; SELECT 1; --` unchanged; `test_malformed_record_is_reported_not_skipped` proves a `NOT NULL` violation raises rather than counting as a duplicate.

**Done when:** those pass and `pragma foreign_keys` reads 1 on a live connection.

## 6. Consolidator (`D1`, `D2`, `D4`, `D5`, `DS1`, `DS6`)

- [ ] `consolidate(entries, repository) -> Report`
- [ ] Resolve each entry against the in-memory index; on miss insert and **add the new key to the index** (`DS1`)
- [ ] Never update an existing `Product` row (`D2`)
- [ ] Link keyed on `(SellerName, Id)` (`D4`)
- [ ] Count records read, matched, inserted, linked, skipped, failed
- [ ] Returns a `Report`; prints nothing

**Tests:** `test_new_product_deduped_within_run` and `test_id_not_unique_across_sellers`, both against a fake repository with no database involved.

**Done when:** those pass and no module other than `repository.py` imports `sqlite3`.

## 7. CLI (`DS4`, `DS5`)

- [ ] `cli.py` with required `--database` and `--input`, optional `--dry-run` and `--report {text,json}`
- [ ] `--dry-run` rolls the transaction back
- [ ] Exit 0 clean, 1 if any record failed, 2 on usage or I/O error
- [ ] Migration applied automatically before consolidating

**Tests:** `test_dry_run_mutates_nothing` — the database file hash is unchanged after a dry run.

**Done when:** the documented invocation from `DESIGN.md` runs end to end against a working copy.

## 8. Acceptance (`DECISIONS.md` expected outcome)

- [ ] `test_consolidate_acceptance`: copy the supplied catalog to a temp path, run, assert **269** read, **266** matched, **3** inserted, **978** products, **257** links, **12** skipped, **0** failed, **20** distinct sellers
- [ ] Assert the 3 inserted products are exactly `Roteador WiFi 6 TP-Link`, `Processador AMD Ryzen 9 7950X`, `Security Test Product`
- [ ] `test_idempotent_second_run`: run twice, second adds nothing
- [ ] `test_injection_record_stored_literally`
- [ ] `test_existing_products_untouched`: all 975 pre-existing rows byte-identical afterwards (`D2`)
- [ ] `test_failure_rolls_back`: an injected mid-run error leaves both tables unchanged (`D8`)
- [ ] `test_dry_run_mutates_nothing`: file hash unchanged, migration included in the rollback (`DS5`)
- [ ] Confirm the committed `data/catalog.db` hash is unchanged after the whole suite runs

**Done when:** every number matches with no adjustment to the expected values. If a number differs, the implementation is wrong or a decision needs revisiting — the table is not to be edited to fit the code.

## 9. Documentation and submission

- [ ] README: real usage, the acceptance table, how to run tests
- [ ] Confirm README conventions match the code as written
- [ ] Note in the README that `docs/DECISIONS.md` is the reasoning trail, since `D2` and `D6` are the decisions a reviewer is most likely to question
- [ ] Push, verify the repo is public and clean, send the link

**Done when:** a clean clone runs the tests successfully with no steps beyond the README.

## Running alongside: documentation verification

`scripts/verify_docs.py` already asserts every figure in these documents against the
artifacts, and `scripts/verify_docs_selftest.py` proves it can fail. Both should stay green
as the implementation lands.

Two points where they interact with the tasks above:

- **Task 2** makes `verify_docs.py` import `catalog_consolidation.normalize` instead of its
  fallback copy of the `D1` rule. From that point the 278 checks also act as a regression
  test on the real match key — if the normalizer drifts, the documented counts stop
  reproducing. The script prints which implementation it used, so the switchover is visible.
- **Task 8** duplicates the acceptance numbers as unit tests. That is deliberate: the script
  proves the *documented* figures are still true of the data, the tests prove the *code*
  reproduces them. Either can fail without the other.

## Deliberately excluded

Logging framework, config files, ORM, fuzzy matching (`D6` rejects it), a `Seller` table (`D9`), price and stock columns (`D10`), and any performance work. `DECISIONS.md` records why for each.
