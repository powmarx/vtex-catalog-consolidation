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

231 tests, in two kinds.

`tests/test_acceptance.py` asserts the expected-outcome table from `docs/DECISIONS.md`, measured before any code existed — a contract, not a description of what the code happens to do.

`tests/test_generated.py` runs against synthetic catalogs and asserts *invariants* that must hold for any input: that every record is accounted for exactly once, that `inserted` equals the change in product count, that neither unique constraint is ever violated, and that every seller identifier round-trips byte for byte. Golden numbers catch regressions; invariants catch overfitting to one fixture.

## Generating test data

Every test above except the generated ones runs on the two supplied artifacts, which is a single fixture that does not contain several situations the implementation claims to handle. `scripts/generate_fixture.py` produces those situations so the claims can be tested rather than asserted.

```
python scripts/generate_fixture.py --list
python scripts/generate_fixture.py --scenario numeric-ids --out build/fx
python scripts/generate_fixture.py --scenario scale --products 50000 --records 20000
```

| Scenario | What it exercises |
| --- | --- |
| `baseline` | clean data, every record matching exactly |
| `numeric-ids` | seller ids like `007`, `1e3`, `' 42'` — the reason `D3` exists |
| `brand-ambiguity` | a name existing both with and without a brand, and one name under two brands |
| `within-run-new` | six sellers submitting one product the catalog lacks |
| `populated` | a non-empty `SellerProduct`, so the migration's row-copy path runs |
| `dirty` | every record a whitespace, case, accent or punctuation variant |
| `duplicates` | both duplicate shapes, plus a cross-seller id reuse that must *not* be suppressed |
| `malformed` | unprocessable records interleaved among good ones |
| `scale` | a large catalog, for timing |
| `adversarial` | all of the above at once, shuffled, plus an injection payload |

Output is a directory holding `catalog.db`, `ProductEntry.json` and a `manifest.json` describing what was planted. Deterministic: the same `--seed` gives the same bytes. Fixtures start unmigrated, like the supplied file.

This earned its place immediately by finding a real bug — the loader was stripping whitespace from seller identifiers, so `' 42'` and `'42 '` collapsed into one listing and the second was silently dropped. No test built on the supplied file could have caught it, because the supplied file has no such ids. See `D7`.

At 50,000 products and 20,000 records the ingest takes about two seconds.

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
docs/MERGE-ANALYSIS.md   why the near-miss rule reports instead of merging (the D6 evidence)
docs/DECISIONS.md        what was chosen and why, with the tradeoff each decision accepts
docs/DESIGN.md           how it is built: module boundaries, migration, CLI, test strategy
docs/TASKS.md            ordered implementation plan with per-step verification
scripts/verify_docs.py   re-measures the artifacts and checks the docs still tell the truth
scripts/analyse_merge_threshold.py  reproduces the D6 evidence
src/catalog_consolidation/
tests/
```

## Verifying the documentation

Every figure in `docs/` was produced by measuring the two artifacts. That measurement is
reproducible rather than trusted:

```
python scripts/verify_docs.py            # non-zero exit on failure
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
python scripts/verify_docs_selftest.py   # applies 24 mutations, each must be caught
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
