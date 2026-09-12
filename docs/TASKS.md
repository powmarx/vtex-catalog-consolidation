# Tasks

**Status: complete.** All nine tasks are done, 231 tests pass, and the acceptance table in [`DECISIONS.md`](DECISIONS.md) reproduces exactly.

Two things landed differently from this plan. `reporting.py` arrived with task 7 rather than task 9, because the CLI needed rendering either way. And task 5 grew an error-translation layer that was not planned: a boundary test caught `cli.py` importing `sqlite3`, and fixing it properly rather than widening the allowlist closed a real gap — `DS3` justified `ON CONFLICT DO NOTHING` on the grounds that a `NOT NULL` violation stays reportable, but nothing was catching it, so it would have rolled back the whole run.

Ordered implementation plan. Each task names what proves it done. Semantics come from [`DECISIONS.md`](DECISIONS.md) (`D1`–`D11`), structure from [`DESIGN.md`](DESIGN.md) (`DS1`–`DS9`).

Order is chosen so that every step is verifiable on its own, and so the two hardest things — the match key and the migration — are settled before anything depends on them.

## 1. Package skeleton and models

- [x] `src/catalog_consolidation/` with `__init__.py`, `__main__.py`
- [x] `models.py`: `SellerEntry`, `Product`, `Report`, `RecordError` as frozen dataclasses
- [x] `pyproject.toml` declaring the package, no runtime dependencies
- [x] Python floor set to 3.10, the earliest version supporting the `X | Y` type syntax used in signatures. Nothing in the design needs anything newer; developed against 3.13

**Done when:** `python -c "import catalog_consolidation"` succeeds and `python -m unittest discover tests` runs with zero tests collected and no import errors.

## 2. The match key (`D1`)

- [x] `normalize.py`: `normalize(value: str | None) -> str` and `match_key(name, brand) -> tuple[str, str]`
- [x] NFKD accent stripping, lowercase, drop non-alphanumeric-non-space characters **without substituting a space**, collapse whitespace
- [x] `None` maps to `""` and is a value, not a wildcard

**Tests:** `test_normalize` table-driven over the real variations — `Smartphone  Galaxy S23`/`Smartphone Galaxy S23`, `Câmera`/`Camera`, `Levi's`/`Levis`, `Monitor LG UltraWide 34"`/`...34`, `Tablet iPad Pro 12.9"`/`12.9''`, `None` brand. Plus `test_normalize_is_lossless`: 975 distinct keys over the supplied catalog.

**Done when:** both tests pass. This is the highest-risk logic in the project, and it is pure, so it needs no database.

## 3. Input loading (`D7`, `DS7`)

- [x] `source.py`: `load_entries(path) -> tuple[list[SellerEntry], list[RecordError]]`
- [x] Missing or blank `Name` or `SellerName` becomes a `RecordError` carrying the record index; the record is dropped and loading continues
- [x] `Id` is opaque — no UUID validation, so the malformed `09835342345-4678-9abc-def012345678` loads normally

**Done when:** the supplied file yields 269 entries and 0 errors, and a fixture with a missing `Name` yields an error rather than an exception.

## 4. Migration (`D3`, `D5`, `DS2`)

- [x] `migration.py`: `apply(connection) -> bool`, returning whether it changed anything
- [x] Guard on `PRAGMA user_version`; set to 1 on success
- [x] Rebuild `SellerProduct` with `SellerProductId TEXT NOT NULL`, copying existing rows
- [x] `CREATE UNIQUE INDEX` for `(SellerName, SellerProductId)` and `(SellerName, ProductId)`
- [x] Whole migration in one transaction

**Tests:** `test_migration_idempotent` — apply twice, second is a no-op, `user_version` is 1, declared type is `TEXT`, both indexes exist, and rows in a pre-populated table survive the rebuild.

**Done when:** those pass and `PRAGMA integrity_check` still returns `ok` afterwards.

## 5. Repository (`D8`, `DS1`, `DS3`)

- [x] `repository.py`, the only module importing `sqlite3`
- [x] Connect with `isolation_level=None` and issue `PRAGMA foreign_keys = ON` as the **first** statement, then assert it reads back as 1 (`DS8` — the pragma is silently ignored inside a transaction)
- [x] Explicit `BEGIN` / `COMMIT` / `ROLLBACK`; no reliance on implicit transactions
- [x] `load_index() -> dict[tuple[str, str], int]` from one `SELECT`
- [x] `insert_product(name, brand, category) -> int`
- [x] `link(...) -> bool` via `INSERT ... ON CONFLICT DO NOTHING`, returning whether a row was written. **Not `INSERT OR IGNORE`** (`DS3`)
- [x] Every statement parameterized; no f-strings or concatenation in SQL

**Tests:** `link` returns `True` then `False` for the same pair; `insert_product` round-trips a brand containing `'; SELECT 1; --` unchanged; `test_malformed_record_is_reported_not_skipped` proves a `NOT NULL` violation raises rather than counting as a duplicate.

**Done when:** those pass and `pragma foreign_keys` reads 1 on a live connection.

## 6. Consolidator (`D1`, `D2`, `D4`, `D5`, `DS1`, `DS6`, `DS7`)

- [x] `consolidate(entries, repository) -> Report`
- [x] Resolve each entry against the in-memory index; on miss insert and **add the new key to the index** (`DS1`)
- [x] Never update an existing `Product` row (`D2`)
- [x] Link keyed on `(SellerName, Id)` (`D4`)
- [x] Count records read, matched, inserted, linked, skipped, failed
- [x] Returns a `Report`; prints nothing

**Tests:** `test_new_product_deduped_within_run` and `test_id_not_unique_across_sellers`, both against a fake repository with no database involved.

**Done when:** those pass and no module other than `repository.py` imports `sqlite3`.

## 7. CLI (`DS4`, `DS5`)

- [x] `cli.py` with required `--database` and `--input`, optional `--dry-run` and `--report {text,json}`
- [x] `--dry-run` rolls the transaction back
- [x] Exit 0 clean, 1 if any record failed, 2 on usage or I/O error
- [x] Migration applied automatically before consolidating

**Tests:** `test_dry_run_mutates_nothing` — the database file hash is unchanged after a dry run.

**Done when:** the documented invocation from `DESIGN.md` runs end to end against a working copy.

## 8. Acceptance (`DECISIONS.md` expected outcome)

- [x] `test_consolidate_acceptance`: copy the supplied catalog to a temp path, run, assert **269** read, **266** matched, **3** inserted, **978** products, **257** links, **12** skipped, **0** failed, **20** distinct sellers
- [x] Assert the 3 inserted products are exactly `Roteador WiFi 6 TP-Link`, `Processador AMD Ryzen 9 7950X`, `Security Test Product`
- [x] `test_idempotent_second_run`: run twice, second adds nothing
- [x] `test_injection_record_stored_literally`
- [x] `test_existing_products_untouched`: all 975 pre-existing rows byte-identical afterwards (`D2`)
- [x] `test_failure_rolls_back`: an injected mid-run error leaves both tables unchanged (`D8`)
- [x] `test_dry_run_mutates_nothing`: file hash unchanged, migration included in the rollback (`DS5`)
- [x] Confirm the committed `data/catalog.db` hash is unchanged after the whole suite runs

**Done when:** every number matches with no adjustment to the expected values. If a number differs, the implementation is wrong or a decision needs revisiting — the table is not to be edited to fit the code.

## 9. Documentation and submission

- [x] README: real usage, the acceptance table, how to run tests
- [x] Confirm README conventions match the code as written
- [x] Note in the README that `docs/DECISIONS.md` is the reasoning trail, since `D2` and `D6` are the decisions a reviewer is most likely to question
- [x] Push, verify the repo is public and clean, send the link

**Done when:** a clean clone runs the tests successfully with no steps beyond the README.

## 9. Findings report (`D11`, `DS9`)

Sequenced last deliberately. It builds on the `Report` object, and the acceptance test must pass before anything is layered on top.

- [x] Extend `Report` with per-record verdicts: matched, inserted, suppressed, rejected
- [x] `reporting.py`: `to_json(report)` as the source of truth, `to_markdown(report)` derived from it
- [x] Review-candidate search in `consolidator.py`, scoped to inserted records only, brand equality plus Jaccard token overlap **strictly greater than** 0.5
- [x] Record every field difference `D2` discarded
- [x] Write to `reports/<timestamp>-<input-stem>.json` and `.md`, after `COMMIT` returns
- [x] `--dry-run` marks the report as such and writes no file unless `--report-file` is given
- [x] `reports/` added to `.gitignore`

**Tests:** `test_review_candidates_found`, `test_candidate_rule_excludes_exact_half`, `test_report_written_only_after_commit`, `test_discarded_values_recorded`.

**Done when:** a run on the supplied file proposes exactly two candidates — `Processador`→`Processor` at 0.667 and `Roteador`→`Router` at 0.600 — proposes nothing for `Security Test Product`, and the acceptance numbers from task 8 are unchanged. The report must not alter what gets written to the database.

## Running alongside: documentation verification

`scripts/verify_docs.py` already asserts every figure in these documents against the
artifacts, and `scripts/verify_docs_selftest.py` proves it can fail. Both should stay green
as the implementation lands.

Two points where they interact with the tasks above:

- **Task 2** makes `verify_docs.py` import `catalog_consolidation.normalize` instead of its
  fallback copy of the `D1` rule. From that point the documentation checks also act as a regression
  test on the real match key — if the normalizer drifts, the documented counts stop
  reproducing. The script prints which implementation it used, so the switchover is visible.
- **Task 8** duplicates the acceptance numbers as unit tests. That is deliberate: the script
  proves the *documented* figures are still true of the data, the tests prove the *code*
  reproduces them. Either can fail without the other.

## Deliberately excluded

Logging framework, config files, ORM, fuzzy matching (`D6` rejects it), a `Seller` table (`D9`), price and stock columns (`D10`), and any performance work. `DECISIONS.md` records why for each.
