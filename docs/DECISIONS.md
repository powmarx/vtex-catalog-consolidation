# Decision record — catalog consolidation

Decisions taken before implementation, with the tradeoff each one accepts. Language: Python. Source assessment: `VTEX-Coding-Interview-AI-Take-home-assessment-2.md`.

Every decision below is grounded in a measurement against the supplied `catalog.db` (975 products, empty `SellerProduct`) and `ProductEntry.json` (269 records, 20 sellers). The numbers in "Expected outcome" are acceptance criteria, not estimates.

## What the data actually contains

Records carry only `Id`, `SellerName`, `Name`, `Brand`, `Category`. There is no GTIN, EAN, or SKU, and no price or stock, so matching has to run on `Name` and `Brand` text alone and there are no per-seller attributes needing a home.

The variations are deliberate and countable: 60 records with doubled internal spaces, 37 with punctuation differences (`34"` vs `34`, `12.9"` vs `12.9''`, hyphens), accents (`Câmera` vs `Camera`), 3 null brands, 1 category disagreement, 2 Portuguese names for products the catalog holds in English, and 1 record carrying a SQL injection payload.

## The catalog is clean; all the mess is inbound

Profiled separately, because it changes which side of the ingest needs defending.

`catalog.db` passes `integrity_check`, has no orphan rows, and its 975 products contain no whitespace or accent anomalies: no leading or trailing spaces, no doubled spaces, no accents, and every value is ASCII. No product name repeats, and no name is shared by two products under different brands. Ids run 1 to 975 with no gaps, and `sqlite_sequence` sits at 975, so the new rows this ingest creates will be 976 to 978.

One exception, so "clean" is not overstated: brand **casing** is internally inconsistent. `simplehuman` and `Simplehuman` both appear, as do `Black+Decker` and `BLACK+DECKER`. Case-insensitive matching absorbs this, and `D2` forbids rewriting it, but the catalog is not authoritative on brand spelling. See `N5` in [`DATA-ISSUES.md`](DATA-ISSUES.md).

Nulls are common but structured: 119 products (12%) have a null `Brand` and 34 have a null `Category`. Null-tolerance is therefore a main path, not an edge case. Across 975 products there are 639 distinct brands and 43 distinct non-null categories. Full breakdown in [`SCHEMA.md`](SCHEMA.md).

Two consequences. Normalization exists purely to absorb *incoming* variation, since the reference side has none. And the catalog is a trustworthy reference, which is the strongest argument for D2.

Engine state relevant to implementation: `journal_mode` is `delete` (not WAL), encoding is UTF-8, `user_version` is 0, and `SellerProduct` is empty with no `sqlite_sequence` entry yet.

## Invariants

These hold regardless of any decision below.

1. **Existing `Product` rows are never modified.** Ingestion inserts and links; it does not update the canonical catalog. Any future change to that stance is a decision to be taken explicitly, not a side effect of ingestion.
2. **Normalization is comparison-only.** Normalized forms exist to compute a match key in memory. Nothing normalized is ever written to the database. Stored `Name` and `Brand` keep whatever spelling they already have.
3. **All SQL is parameterized.** No string interpolation into statements, anywhere.

## Decisions

### D1 — Match on normalized `(Name, Brand)`

Match key: strip accents via NFKD, lowercase, drop every character that is not alphanumeric or whitespace, collapse runs of whitespace. Applied identically to catalog rows and incoming records.

Dropping punctuation without substituting a space is deliberate: it makes `Levi's` and `Levis` agree, which a space-substituting rule would not.

`Category` is excluded from the key. It disagrees across sellers for the same product (`Photo` vs `Photography`), so including it would split a genuine duplicate.

A null `Brand` normalizes to the empty string and is treated as a value, not a wildcard. It matches only another null or blank brand, never a populated one. Verified: all 3 null-brand incoming records match null-brand catalog rows (products 113, 398, 492), and no catalog name exists both with and without a brand, so the empty key cannot match the wrong product.

Measured: exact matching on `(Name, Brand, Category)` finds 200 of 269. This key finds 266.

**Verified safe, not lucky:** the key produces 975 distinct values across the 975 seeded products. Zero collisions, so normalization cannot merge two distinct catalog entries on this data.

**`Brand` is redundant on this data and kept anyway.** Matching on the name key alone also finds 266, no catalog name key is shared by more than one product, and zero matches are rejected on a brand disagreement. Brand is retained because two products sharing a name under different brands is an ordinary catalog scenario, and dropping the field would be fitting the key to this particular file. The key is deliberately more conservative than the data requires.

Tradeoff: purely lexical. It cannot see that two differently-worded names denote the same product. See D6.

### D2 — First write wins; the canonical row is never updated

When an incoming record matches an existing product, write the `SellerProduct` link and leave `Product` untouched. Field disagreements between sellers do not propagate into the catalog.

Rationale: the assessment asks only that duplicates not be inserted and that seller offers be recorded. It never asks for the canonical record to be improved. Not writing keeps ingestion auditable and means no seller can degrade catalog data.

The measured data quality makes this concrete. The catalog has zero text anomalies while the input has 60 doubled-space names, 37 punctuation variants, and accent inconsistencies. Letting incoming records overwrite canonical fields would move dirt from the seller side into a clean catalog. First-write-wins is not just the simplest option here, it is the one the data argues for.

Tradeoff: the catalog forgoes genuinely better incoming data. A record with a brand where the catalog has null stays null. Revisiting this would mean a most-complete-wins merge, which needs a trust model to be safe.

### D3 — `SellerProductId` becomes `TEXT`

The column is `INTEGER NOT NULL` and every incoming `Id` is a UUID string.

An earlier version of this decision claimed the data could not be loaded without widening the column. That was wrong, and the correction is worth recording. SQLite uses type affinity rather than strict typing: a non-numeric string offered to an `INTEGER` column is stored unchanged. Verified — inserting `a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d` succeeds and `typeof()` reports `text`. Ingestion would work with no migration at all, quietly storing text in a column declared integer.

The migration is still made, for two measured reasons that survive the correction.

**Silent coercion mangles numeric-looking identifiers.** On an `INTEGER` column, affinity converts any string that looks numeric:

| Seller sends | `INTEGER` column stores | `TEXT` column stores |
| --- | --- | --- |
| `007` | `7` | `007` |
| `0012` | `12` | `0012` |
| `1e3` | `1000` | `1e3` |
| ` 42` | `42` | ` 42` |
| `a1b2c3d4-e5f6` | `a1b2c3d4-e5f6` | `a1b2c3d4-e5f6` |

A seller SKU carrying leading zeros or padding is irreversibly altered. An identifier is an opaque token; a column that rewrites it is the wrong column.

**Worse, coercion breaks `D5`.** Because `007` and `7` both collapse to `7`, two genuinely different seller listings collide on `UNIQUE(SellerName, SellerProductId)` and one is silently discarded as a duplicate. Verified: inserting `007` then `7` into a unique-indexed `INTEGER` column raises a constraint violation, while a `TEXT` column stores both. The idempotency guarantee is only sound if the column preserves what it is given.

None of this bites on the supplied file, where every `Id` is a non-numeric UUID. The migration is a correctness guarantee for the identifiers this schema invites rather than a fix for a present failure, which is a weaker but honest justification.

Tradeoff: rejected the surrogate-integer alternative because it preserves the original schema at the cost of losing traceability back to the seller's own identifier, which is the one thing that column exists to hold.

Implementation note: SQLite cannot alter a column's declared type, so this migration rebuilds `SellerProduct` — create, copy, drop, rename. The table is empty here, but the migration copies rows so it remains correct against a populated database. The D5 constraints need no rebuild; `CREATE UNIQUE INDEX` adds them in place.

### D4 — Identity of a seller listing is `(SellerName, Id)`, never `Id` alone

14 `Id` values repeat in the file. Thirteen of those repeats span *different* sellers on unrelated products — one `Id` covers both `Curtain Rod Adjustable` from GardenStore and `Bookshelf 5-Shelf` from SportsHub. `Id` is scoped to the seller, not global.

Deduplicating on `Id` alone would merge unrelated products. `(SellerName, Id)` is the natural key.

### D5 — Idempotency enforced by the schema, via two constraints

- `UNIQUE(SellerName, SellerProductId)` — the same seller listing cannot be linked twice.
- `UNIQUE(SellerName, ProductId)` — a seller is recorded against a product once.

Both are needed, and the second does most of the work. Measured: the first alone suppresses 1 duplicate row, the second suppresses 12.

That gap is the planted intra-file duplication. Ten sellers each submit one product twice under two *different* `Id` values differing only by a doubled space, with `Id`s that are rotations of each other. GardenStore submits Canon EOS R6 three times: twice under one `Id` differing by an accent, once more under a fresh `Id` with `Category` as `Photo` instead of `Photography`.

Rationale for the second constraint: `SellerProduct` exists to record which sellers offer each product. A second row for the same seller and product records nothing new.

Tradeoff: this discards the duplicate listing's `SellerProductId`. A real marketplace can legitimately have one seller listing the same product twice under different SKUs, and this schema deliberately cannot express that. Accepted because the assessment's stated goal is recording which sellers offer each product, not modelling listings.

Enforcement is by constraint plus `INSERT ... ON CONFLICT DO NOTHING` rather than a read-then-write check, so concurrent runs cannot interleave into a duplicate. Specifically `ON CONFLICT DO NOTHING` and not `INSERT OR IGNORE`, which would also swallow `NOT NULL` violations and miscount malformed records as duplicates — see `DS3` in `DESIGN.md`.

### D6 — Portuguese/English pairs are accepted as new products, not matched

`Roteador WiFi 6 TP-Link` and `Processador AMD Ryzen 9 7950X` are the same products as catalog rows 21 (`Router WiFi 6 TP-Link`) and 28 (`Processor AMD Ryzen 9 7950X`). No lexical normalization or string-similarity metric reaches `Roteador` from `Router`. They will be inserted as new products.

This is a known, documented false negative rather than a silent one.

Rejected alternatives, and why:

- **Hardcoded synonym map** (`roteador`→`router`). Would catch exactly these two cases and nothing else. It is fitted to the test file, which is worse than a stated limitation.
- **Brand-plus-model-token matching.** Would catch both, but `TP-Link` alone matches two different catalog products, so the rule needs a notion of which token is the distinctive one. That is a heuristic with real false-positive risk, and merging two genuinely different products is a worse failure than leaving a duplicate.

What a production system would do instead: resolve on a real product identifier (GTIN/EAN), or run locale-aware matching with a translation service, or route low-confidence candidates to human review rather than deciding automatically. All three need infrastructure the assessment does not provide.

### D7 — Malformed and hostile input is processed as data, never as code

One record has brand `TestBrand'; SELECT 1; --`. It is treated as an ordinary record: parameterized queries mean the payload is stored as a literal string, never parsed.

That same record also carries a malformed identifier, and it is not the only one — **three** of the 269 ids fail a UUID shape check: a group with 7 hex digits instead of 8, one with 4 groups instead of 5, and one containing the non-hex character `u`. All three are accepted. `Id` is an opaque token that is only ever compared for equality and stored, so validating its shape would reject data the system has no business rejecting. Enumerated in [`SCHEMA.md`](SCHEMA.md).

Null brands (3 records) normalize to an empty string and participate in matching normally.

**Identifiers are preserved byte for byte.** `Name`, `Brand` and `Category` have surrounding whitespace stripped on load, because that is presentational. `Id` does not. It is an opaque token, so anything that alters it changes which listing it denotes.

This began as a bug, and the bug is worth recording because it is the same mistake `D3` is about. The loader stripped every field alike, which turned `' 42'` and `'42 '` into one `'42'` — two distinct listings collapsing, with the second silently suppressed as a duplicate. `D3` widened `SellerProductId` to `TEXT` so the *database* could not rewrite an identifier through integer affinity; the loader was rewriting it one layer earlier, so the migration was protecting a value that had already been altered.

The supplied file contains no such ids, so no test built on it could have caught this. A synthetic fixture did, on the first run. Blankness is still judged on the stripped form: an id of `'   '` is no identifier and is reported as missing.

Per-record failures are collected and reported rather than aborting the batch, so one bad record cannot cost the other 268.

Tradeoff: no schema validation on input. A record missing `Name` would fail on the `NOT NULL` constraint and be reported rather than rejected up front. Acceptable at this size; a real ingest would validate before touching the database.

### D8 — Ingestion runs in a single transaction

The whole file commits or rolls back as one unit, so a mid-file failure cannot leave the catalog half-consolidated. `PRAGMA foreign_keys = ON` is set explicitly, since SQLite ignores the declared foreign key otherwise.

### D9 — No `Seller` table is added

The assessment invites schema changes, so the absence of a seller entity is worth answering rather than leaving implicit. `SellerName` stays a `TEXT` column on `SellerProduct`.

Measured: `SellerName` is the only seller attribute anywhere in the input. There are 20 distinct values, all clean — no leading or trailing whitespace, no doubled spaces, all ASCII, and 20 distinct values after normalization, so no two seller names are variants of each other. There is no seller metadata to normalize into a table and no seller-name deduplication to perform.

Adding a `Seller` table would introduce a join and a surrogate key while storing exactly one column of real information. That is structure without content.

Tradeoff: seller names are duplicated across `SellerProduct` rows, and a seller rename would require an update across many rows rather than one. Accepted, because nothing in the problem involves renaming sellers or attaching attributes to them. This decision would reverse the moment sellers gained any attribute of their own — a contact, a status, a trust tier.

### D10 — Per-seller attributes are out of scope, because the data has none

The natural reading of the schema-change permission is that offer-level data such as price, stock, or a seller's own SKU has nowhere to live in a two-table design, since those belong to a seller's offer rather than to the product.

Measured: the input has no such fields. Every record carries exactly `Id`, `SellerName`, `Name`, `Brand`, `Category`. `Id` is the seller's own identifier and already has a home in `SellerProductId` once D3 widens it.

So no schema change is made on this account. Recorded explicitly because the omission is otherwise indistinguishable from having missed the point.

Had those fields been present, `SellerProduct` is where they would belong — it is already the offer-level table, one row per seller per product, and adding price and stock columns there would need no new relation.

### D11 — Every run writes a findings report

Consolidation makes decisions that are invisible afterwards. `D6` inserts two products it knows are probably duplicates; `D2` discards 66 incoming field values; `D5` suppresses 12 records. None of that is recoverable from the resulting database.

Each run therefore writes a findings file to `reports/<timestamp>-<input-stem>.json`, with a Markdown rendering beside it. Four classes:

- **Rejections** — records that could not be processed at all.
- **Suppressions** — records deliberately not written, naming the constraint that caught each.
- **Review candidates** — records inserted as new products for which a plausible existing match was found on partial evidence. See below.
- **Discarded values** — every field difference `D2` dropped, so `Levi's` and `Photo` remain recoverable.

Review candidates are the point of this decision. They let matching stay conservative while making the cost of that conservatism visible. `D6` refuses to guess that `Roteador` means `Router`, which is right — but a silent refusal teaches nobody. Reported, it becomes a queue a human can act on, without putting a false-positive-prone heuristic into the matching path where a wrong merge is unrecoverable.

**Candidate rule:** the incoming record's normalized brand equals a catalog product's normalized brand, **and** Jaccard token overlap of the normalized names is strictly greater than 0.5. Searched only against records that were inserted, never against matched ones.

Measured on the supplied file. Within scope, two candidates and one correct rejection:

| Inserted record | Catalog product | Overlap | Proposed |
| --- | --- | --- | --- |
| `Processador AMD Ryzen 9 7950X` | 28 `Processor AMD Ryzen 9 7950X` | 0.667 | yes |
| `Roteador WiFi 6 TP-Link` | 21 `Router WiFi 6 TP-Link` | 0.600 | yes |
| `Roteador WiFi 6 TP-Link` | 202 `Smart Plug TP-Link 4-Pack` | 0.143 | no |

`Security Test Product` proposes nothing, because no catalog product shares its brand. Two true positives, zero false positives.

Two things the measurement settled that reasoning would not have:

**The scoping is a correctness requirement, not an optimization.** Run against matched records too, the same rule produces spurious pairs: `Hockey Stick Ice` against `Hockey Skates Ice`, and `Bar Stools Set of 2` against `Nightstand Set of 2`. Genuinely different products.

**The inequality is strict for a reason.** Those spurious pairs score exactly 0.500. `>= 0.5` would admit them the moment anyone widened the scope; `> 0.5` excludes them outright. The threshold is deliberately placed just above the nearest confusable family in the actual data.

Honest limitation: within scope the gap between the lowest accepted candidate (0.600) and the highest rejected one (0.143) is wide, so any threshold in that range behaves identically here. This data does not finely determine the value — it only establishes that 0.5 is a safe floor. A larger corpus would be needed to tune it, and the report is the mechanism that would generate the evidence.

Tradeoff: a report nobody reads is dead weight. It deliberately excludes input profiling for its own sake — counting doubled spaces, cataloguing brand casing. That analysis is in [`DATA-ISSUES.md`](DATA-ISSUES.md), it describes a known file, and regenerating it per run produces noise. The report contains only what affected this run's decisions.

## Expected outcome

Measured by simulating the decisions above against the real files. The implementation is correct when it reproduces these exactly.

| Metric | Value |
| --- | --- |
| Input records | 269 |
| Matched to an existing product | 266 |
| New `Product` rows inserted | 3 |
| `Product` rows after ingest | 978 (975 + 3) |
| `SellerProduct` rows after ingest | 257 |
| Records skipped as duplicate listings | 12 |
| Distinct sellers linked | 20 |
| Second run of the same file | 0 new rows in either table |

The 3 insertions are `Roteador WiFi 6 TP-Link`, `Processador AMD Ryzen 9 7950X` (both per D6), and `Security Test Product` (genuinely absent from the catalog).

## Conventions

The Guideline Document referenced by the assessment was never provided. In its absence: PEP 8, type hints on public functions, standard library only for the core path, ingestion logic separated from persistence, and a single documented entry point. The substitution is recorded in the README so the missing input reads as a documented assumption rather than an oversight.
