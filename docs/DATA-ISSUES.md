# Data issues

Every defect, inconsistency, and trap found in `data/catalog.db` and `data/ProductEntry.json`, from a systematic scan across three passes: file and encoding level, semantic, and cross-artifact.

Each issue carries an ID, a severity, and how the implementation responds. Severity is about impact on consolidation, not about how wrong the data looks.

- **Trap** — will produce a wrong result if handled naively. These are the assessment.
- **Handled** — real, and the design absorbs it.
- **Noted** — real, no action taken, recorded so the omission is deliberate.

Figures cross-reference [`SCHEMA.md`](SCHEMA.md); decisions referenced as `D1`–`D10` live in [`DECISIONS.md`](DECISIONS.md).

## Summary

| | Traps | Handled | Noted |
| --- | --- | --- | --- |
| `ProductEntry.json` | 5 | 6 | 4 |
| `catalog.db` | 0 | 2 | 6 |

The input is where the difficulty is. The catalog is nearly clean, with two exceptions.

---

## Traps

### T1 — `Id` is reused across sellers on unrelated products

**Severity: trap.** 14 of 269 ids repeat. Thirteen of those repeats are across *different* sellers on *unrelated* products. Deduplicating on `Id` alone merges products that have nothing to do with each other.

All 13, in full:

| Id | Record A | Record B |
| --- | --- | --- |
| `00112233-4455-4667-7889-900112233445` | GardenStore · Curtain Rod Adjustable | SportsHub · Bookshelf 5-Shelf |
| `87654321-0987-4654-9876-543210987654` | KitchenPlus · Sectional Couch L-Shaped | GadgetZone · Spin Mop Self-Wringing |
| `76543210-9876-4543-8765-432109876543` | OfficeSupply · Recliner Chair Leather | SuperMart · Steam Mop Bissell |
| `65432109-8765-4432-7654-321098765432` | GardenStore · Accent Chair Armchair | SportsHub · Vacuum Cleaner Upright |
| `54321098-7654-4321-6543-210987654321` | CleaningWorld · Dining Table Set 5-Piece | HomeGoods · Vacuum Cleaner Cordless |
| `43210987-6543-4210-5432-109876543210` | FootwearHub · Dining Table Rectangular | FurnitureWorld · Handheld Vacuum Cordless |
| `32109876-5432-4109-4321-098765432109` | AccessoryWorld · Dining Chairs Set of 4 | AudioPro · Shop Vac Wet Dry |
| `21098765-4321-4098-3210-987654321098` | SmartHomeStore · Bar Stools Set of 2 | GamingStore · Carpet Cleaner Machine |
| `10987654-3210-4987-2109-876543210987` | MegaStore · Kitchen Island Cart | FitnessCenter · Spot Cleaner Portable |
| `00112277-3344-4556-6778-899001122334` | FootwearHub · Rain Boots Rubber | FurnitureWorld · Sports Sunglasses Oakley |
| `11223388-4455-4667-7889-900112233445` | AccessoryWorld · Backpack Laptop North Face | AudioPro · Watch Automatic Seiko |
| `22334499-5566-4778-8990-011223344556` | SmartHomeStore · Messenger Bag Canvas | GamingStore · Smartwatch Garmin Fenix |
| `33445500-6677-4889-9001-122334455667` | MegaStore · Duffle Bag Gym | FitnessCenter · Digital Watch Casio G-Shock |

The ids are visibly constructed — a descending sequence pairing furniture against cleaning products — so this is planted, not accidental.

**Response:** `D4`. A listing is identified by `(SellerName, Id)`, never `Id` alone.

### T2 — The same seller submits the same product more than once, under different ids

**Severity: trap.** 11 groups, 12 surplus records. Ten sellers submit one product twice with ids that are rotations of each other, differing only by a doubled space in the name. GardenStore submits Canon EOS R6 three times.

| Seller | Product | Copies | Differences |
| --- | --- | --- | --- |
| GardenStore | Camera Canon EOS R6 | 3 | accent (`Câmera`), then a fresh id with category `Photo` |
| OfficeSupply | Basketball Spalding Official | 2 | doubled space; ids `1111aaaa…` / `aaaa1111…` |
| GardenStore | Basketball Hoop Portable | 2 | doubled space; `2222bbbb…` / `bbbb2222…` |
| CleaningWorld | Soccer Ball Size 5 | 2 | doubled space; `3333cccc…` / `cccc3333…` |
| FootwearHub | Soccer Goal Portable | 2 | doubled space; `4444dddd…` / `dddd4444…` |
| AccessoryWorld | Soccer Shin Guards | 2 | doubled space; `5555eeee…` / `eeee5555…` |
| SmartHomeStore | Goalkeeper Gloves | 2 | doubled space; `6666ffff…` / `ffff6666…` |
| MegaStore | Football Official Size | 2 | doubled space; `7777aaaa…` / `aaaa7777…` |
| TechWorld | Football Gloves Receiver | 2 | doubled space; `8888bbbb…` / `bbbb8888…` |
| ElectroHub | Football Helmet Adult | 2 | doubled space; `9999cccc…` / `cccc9999…` |
| GadgetZone | Baseball Glove Adult | 2 | doubled space; `0000dddd…` / `dddd0000…` |

The trap: `UNIQUE(SellerName, SellerProductId)` catches only **1** of these 12, because the ids genuinely differ. `UNIQUE(SellerName, ProductId)` catches all 12.

**Response:** `D5` adds both constraints. This is why the second one is not redundant.

### T3 — Cross-language product names

**Severity: trap.** Two records name a product in Portuguese where the catalog holds it in English. No lexical normalization or string-similarity metric bridges them.

| Input | Catalog | Same product? |
| --- | --- | --- |
| `Roteador WiFi 6 TP-Link` (TP-Link) | id 21 `Router WiFi 6 TP-Link` (TP-Link) | yes |
| `Processador AMD Ryzen 9 7950X` (AMD) | id 28 `Processor AMD Ryzen 9 7950X` (AMD) | yes |

Both will be inserted as new products — 2 of the 3 insertions this run produces are duplicates in substance.

**Response:** `D6` accepts them as a documented false negative and explains why a synonym map fitted to this file was rejected.

### T4 — SQL injection payload in a data field

**Severity: trap.** Record at index 180: brand `TestBrand'; SELECT 1; --`, name `Security Test Product`, seller MegaStore. Naive string-concatenated SQL executes the fragment.

This is also the only genuinely new product in the file.

**Response:** invariant 3 in `DECISIONS.md` — every statement parameterized. `D7` treats the payload as an ordinary string.

### T5 — `ON CONFLICT DO NOTHING` vs `INSERT OR IGNORE`

**Severity: trap**, in the implementation rather than the data, but caused by the data. `INSERT OR IGNORE` suppresses every constraint violation, not just uniqueness. A record failing `NOT NULL` is silently discarded with `rowcount = 0`, indistinguishable from a duplicate skip — so a malformed record gets counted as a duplicate listing and never reported.

**Response:** `DS3` in [`DESIGN.md`](DESIGN.md). `ON CONFLICT DO NOTHING` raises on `NOT NULL` while still absorbing uniqueness conflicts.

---

## Handled

### H1 — Doubled internal spaces in `Name`

60 of 269 records. `Smartphone  Galaxy S23`, `iPhone 15  Pro`, `Football Official  Size`. Never leading or trailing — always internal. Response: `D1` collapses whitespace runs.

### H2 — Inconsistent punctuation for units

Three spellings of the same measurement coexist:

| Product | Variants present |
| --- | --- |
| Tablet iPad Pro | `12.9"`, `12.9''`, `12.9` |
| Smart TV Samsung | `55"`, `55` (plus a doubled-space variant) |
| Monitor LG UltraWide | `34"` |

Response: `D1` drops non-alphanumeric characters, so all forms converge.

### H3 — Accented vs unaccented spelling

`Câmera Canon EOS R6` against `Camera Canon EOS R6`. The only non-ASCII character in the entire 48938-byte file is the `â` at index 55, encoded NFC (U+00E2 precomposed, not a combining sequence). Response: `D1` strips accents via NFKD decomposition, which handles both forms.

### H4 — Apostrophe dropped from a brand

Input `Levi's`, catalog `Levis`, same product `Belt Leather Reversible`. This is why `D1` removes punctuation **without substituting a space** — a space-substituting rule yields `levi s` and fails to match `levis`.

### H5 — Brand casing variants

`BLACK+DECKER` and `Black+Decker` both appear in the input, and both appear in the catalog. Response: `D1` lowercases, so both normalize to `blackdecker`.

### H6 — Null brands

3 input records have `Brand: null` — `Cable Organizer Kit`, `Bed Frame Wood King`, `Round Rug 6 Feet`. All three match catalog products that also have a null brand (ids 113, 398, 492). Response: `D1` maps null to the empty string and treats it as a value, not a wildcard. Verified that no catalog name exists both with and without a brand, so the empty key cannot drift onto the wrong product.

---

## Noted, no action

### N1 — Three ids are not valid UUIDs

| Index | Value | Defect |
| --- | --- | --- |
| 92 | `ddddeee-ffff-4000-1111-222233334444` | first group has 7 hex digits, not 8 |
| 180 | `09835342345-4678-9abc-def012345678` | 4 groups instead of 5; first has 11 digits |
| 268 | `uddd0000-eeee-4111-ffff-aaaa22223333` | contains `u`, not a hex digit |

Index 268 is one character from `dddd0000-eeee-4111-ffff-aaaa22223333`, which is also in the file on a different record — so it reads as a corrupted copy rather than a fresh id. Index 180 is the injection record.

No action: `Id` is an opaque token, only ever stored and compared for equality. Validating its shape would reject data the system has no business rejecting. Per `D7`.

### N2 — Category disagreement between sellers

One case: `Photo` versus `Photography` for Camera Canon EOS R6. `Photo` is the only one of the input's 28 category values absent from the catalog's 43. Excluded from the match key deliberately — including `Category` would split a genuine duplicate. Per `D1`.

### N3 — Category data discarded on match

1 record's category disagrees with the catalog, and it is not written. Also `Photo` never enters the catalog as a value. This is `D2` working as specified, recorded so the loss is visible.

### N4 — File has no trailing newline

`ProductEntry.json` does not end with a newline. Valid JSON, no impact, noted for completeness. Otherwise the file is clean: no BOM, LF line endings throughout, no tabs, no control characters, no zero-width or non-breaking spaces, no duplicate keys in any object, all 269 records carrying the identical five-key tuple in identical order, no nested containers.

### N5 — Catalog brand casing is internally inconsistent

The catalog is clean on whitespace, accents, and ASCII, but **not** on brand casing. Two brands are stored two ways:

| Normalized | Stored as | Products |
| --- | --- | --- |
| `simplehuman` | `simplehuman` | 451 Soap Dispenser Automatic, 454 Trash Can Step Pedal |
| | `Simplehuman` | 458 Dish Drying Rack Large, 460 Sponge Holder Sink Caddy |
| `blackdecker` | `Black+Decker` | 607 Handheld Vacuum Cordless |
| | `BLACK+DECKER` | 661 Grass Trimmer String, 663 Hedge Trimmer Electric |

No action: `D2` forbids modifying existing rows, and `D1` matches case-insensitively so nothing breaks. Recorded because it undercuts treating the catalog as canonical spelling — a brand-normalization exercise would need to address it.

### N6 — Schema gaps in the supplied database

Present as supplied, all confirmed:

- `Product.Name` has no unique constraint, so nothing prevents duplicate products at the schema level
- `SellerProduct` has no unique constraint, so nothing prevents duplicate links
- no index on `Product.Name`, so any lookup is a full scan
- the foreign key is declared but SQLite leaves enforcement **off** by default
- `SellerProductId` is declared `INTEGER` and receives UUID text
- no `Seller` table; `SellerName` is free text on every row
- no audit columns

`D3` and `D5` address the third through fifth. `D9` declines the `Seller` table. The rest are out of scope for this exercise.

---

## Things that are fine

Checked and clean, recorded so the scan's coverage is legible:

- `integrity_check` and `quick_check` both `ok`; `foreign_key_check` empty; freelist 0 pages
- No exact duplicate records in the input — no two records share all five fields
- No two records are identical except for `Id`
- Seller names are clean: 20 distinct before and after normalization, no whitespace or casing variants, so seller identity needs no deduplication
- No brand disagreements for the same normalized product
- Null discipline is consistent: both artifacts use `NULL`, never an empty string, in every nullable column
- No record would violate a `NOT NULL` column — `Id`, `SellerName`, and `Name` are populated in all 269
- Catalog text has no leading or trailing whitespace, no doubled spaces, no accents, no control characters, all ASCII
- No two catalog products share a normalized name; no catalog category has a spelling variant
- No catalog product name maps to two different categories
- The three new products' categories (`Networking`, `Components`, `Electronics`) already exist in the catalog, so no new category value is introduced

## Scale of the consolidation

Context for the numbers, since the shape is unintuitive.

The file touches **198** distinct catalog products with **266** matching records, because most products are offered by more than one seller:

| Sellers offering the product | Products |
| --- | --- |
| 1 | 134 |
| 2 | 61 |
| 3 | 2 |
| 4 | 1 |

**777** of the 975 catalog products are never mentioned. The consolidation is narrow and dense rather than broad — almost every record is a duplicate of something, which is why matching quality dominates this problem and insert throughput is irrelevant.
