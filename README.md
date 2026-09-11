# vtex-catalog-consolidation

Catalog consolidation for a marketplace: ingest a file of products submitted by many sellers into an existing product catalog, without duplicating products, while recording which sellers offer each product.

**Status: design settled, implementation not started.** The decisions governing the implementation are in [`docs/DECISIONS.md`](docs/DECISIONS.md), and they were made from measurements against the supplied data rather than from assumption. Usage instructions will land with the code.

## The problem in one paragraph

A traditional e-commerce company is becoming a marketplace. It owns a catalog of 975 products. Sellers submit their own catalogs, and the same real-world product is commonly submitted by several sellers with slight differences in how it is described. Duplicates must not enter the product table, but every seller's offer of a product must be recorded.

## Layout

```
data/catalog.db          SQLite catalog as supplied (975 products) - pristine, never mutated
data/ProductEntry.json   seller submissions as supplied (269 records, 20 sellers)
docs/DECISIONS.md        what was chosen and why, with the tradeoff each decision accepts
docs/DESIGN.md           how it is built: module boundaries, migration, CLI, test strategy
docs/TASKS.md            ordered implementation plan with per-step verification
src/catalog_consolidation/
tests/
```

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
