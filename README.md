# vtex-catalog-consolidation

Catalog consolidation for a marketplace: ingest a file of products submitted by many sellers into an existing product catalog, without duplicating products, while recording which sellers offer each product.

The decisions governing behaviour are in [`docs/DECISIONS.md`](docs/DECISIONS.md), and each was made from a measurement against the supplied data rather than from assumption.

## Running it

Python 3.10 or newer. No dependencies.

The catalog is mutated in place, and `data/catalog.db` is the pristine artifact as supplied, so work on a copy:

```
copy data\catalog.db data\catalog.local.db
python -m catalog_consolidation --database data/catalog.local.db --input data/ProductEntry.json
```

`--database` is deliberately required with no default, so no invocation can mutate the baseline by forgetting an argument.

```
records read                  269
matched existing product      266
products inserted               3
links created                 257
duplicate listings skipped     12
records failed                  0
sellers linked                 20
catalog products              975 -> 978

2 review candidate(s): inserted as new, but a close match exists
  record 64: 'Processador AMD Ryzen 9 7950X' ~ product 28 'Processor AMD Ryzen 9 7950X' (overlap 0.667)
  record 57: 'Roteador WiFi 6 TP-Link' ~ product 21 'Router WiFi 6 TP-Link' (overlap 0.600)

findings: reports\20260912T004501Z-ProductEntry.json
          reports\20260912T004501Z-ProductEntry.md
```

Options:

| Flag | Effect |
| --- | --- |
| `--dry-run` | run everything in one transaction and roll it back; the file is left byte-identical |
| `--report {text,json}` | `json` prints the full machine-readable report to stdout |
| `--reports-dir DIR` | where findings files go (default `reports/`, gitignored) |
| `--no-report-file` | print the summary but write no files |
| `--no-candidates` | skip the review-candidate search |

Exit codes: `0` success, `1` the run finished but at least one record could not be processed, `2` the run could not start or finish. Suppressing duplicate listings is success — it is the specified behaviour.

Running the same file twice is safe: the second run adds nothing.

### Tests

```
set PYTHONPATH=src
python -m unittest discover -s tests
```

185 tests. The acceptance tests in `tests/test_acceptance.py` assert the expected-outcome table from `docs/DECISIONS.md`, which was measured before any code existed — so it is a contract, not a description of what the code happens to do.

## Review candidates

Matching is deliberately lexical and conservative, so it cannot tell that `Roteador` and `Router` are the same word in two languages. Those two products are therefore inserted as new, which is a documented false negative rather than a hidden one: every run reports the near-misses it declined to merge, with the evidence, so a human can act on them. A wrong automatic merge is unrecoverable; a reported near-miss costs only attention.

## The problem in one paragraph

A traditional e-commerce company is becoming a marketplace. It owns a catalog of 975 products. Sellers submit their own catalogs, and the same real-world product is commonly submitted by several sellers with slight differences in how it is described. Duplicates must not enter the product table, but every seller's offer of a product must be recorded.

## Layout

```
data/catalog.db          SQLite catalog as supplied (975 products) - pristine, never mutated
data/ProductEntry.json   seller submissions as supplied (269 records, 20 sellers)
docs/SCHEMA.md           database and JSON schemas as supplied, and after migration
docs/DATA-ISSUES.md      every defect and trap found in the two artifacts, and the response
docs/DECISIONS.md        what was chosen and why, with the tradeoff each decision accepts
docs/DESIGN.md           how it is built: module boundaries, migration, CLI, test strategy
docs/TASKS.md            ordered implementation plan with per-step verification
scripts/verify_docs.py   re-measures the artifacts and checks the docs still tell the truth
src/catalog_consolidation/
tests/
```

## Verifying the documentation

Every figure in `docs/` was produced by measuring the two artifacts. That measurement is
reproducible rather than trusted:

```
python scripts/verify_docs.py            # 278 checks, non-zero exit on failure
python scripts/verify_docs.py -v         # list every check
python scripts/verify_docs.py --section sqlite
```

It re-measures both files, confirms they are byte-for-byte unmodified, executes the
SQLite behaviours the design depends on rather than asserting them, and checks the
documents for self-consistency — that summary tables sum, that every decision id is
referenced, that shared numbers agree across documents, and that corrected claims have
not crept back.

A verifier that cannot fail is worse than none, so its ability to fail is itself tested:

```
python scripts/verify_docs_selftest.py   # applies 16 mutations, each must be caught
```

That self-test earned its place. It found two checks that were passing vacuously — one
that survived the DS3 heading being inverted to recommend the opposite SQL clause, and a
missing check that let `SCHEMA.md` misstate the database's own DDL.

Read `docs/DECISIONS.md` first. It is the reasoning trail, and `D2` (first write wins) and `D6` (two documented false negatives) are the decisions most worth challenging.

`data/catalog.db` is committed unmodified and serves as the baseline. Ingestion runs against a copy so that runs are repeatable and the baseline stays restorable from git.

## Design summary

Full reasoning and the tradeoff accepted for each decision is in `docs/DECISIONS.md`. In brief:

- Products match on a normalized `(Name, Brand)` key — accents stripped, lowercased, punctuation dropped, whitespace collapsed. The data carries no GTIN, EAN, or SKU, so matching is necessarily lexical.
- First write wins. Existing catalog rows are never modified by ingestion.
- Idempotency is enforced by unique constraints rather than by read-then-write checks.
- A seller listing is identified by `(SellerName, Id)`. `Id` alone is not unique across sellers in the supplied data.
- Two known false negatives are documented rather than papered over with a heuristic fitted to the test file.

The implementation is correct when it reproduces the expected-outcome table in `docs/DECISIONS.md`: 269 records in, 266 matched, 3 products inserted, 257 seller links, 12 duplicate listings skipped, and a second run that changes nothing.

## Conventions

The assessment references a Guideline Document that was never provided. In its absence this repo follows PEP 8, uses type hints on public functions, keeps the core path to the standard library, separates ingestion logic from persistence, and exposes a single documented entry point. Recording the substitution here so the missing input reads as a documented assumption rather than an oversight.

## Provenance

`data/catalog.db` and `data/ProductEntry.json` are the artifacts supplied with the assessment, published by VTEX at `engineering-hiring-process.s3.us-east-1.amazonaws.com`. The assessment brief itself is deliberately not included in this repository.
