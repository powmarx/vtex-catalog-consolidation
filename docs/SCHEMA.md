# Schema reference

The database and input file as supplied, and the database after migration. Every figure here was read from `data/catalog.db` and `data/ProductEntry.json` in this repository, not from the assessment brief.

Reference only — no decisions live here. See [`DECISIONS.md`](DECISIONS.md) for why the schema changes and [`DESIGN.md`](DESIGN.md) for how.

## 1. Database as supplied

`data/catalog.db` — SQLite 3, 61440 bytes, page size 4096, 15 pages, UTF-8, `journal_mode=delete`, `user_version=0`, `integrity_check=ok`.

### DDL, verbatim

```sql
CREATE TABLE Product (
    Id       INTEGER PRIMARY KEY AUTOINCREMENT,
    Name     TEXT NOT NULL,
    Brand    TEXT,
    Category TEXT
);

CREATE TABLE SellerProduct (
    Id              INTEGER PRIMARY KEY AUTOINCREMENT,
    SellerName      TEXT NOT NULL,
    ProductId       INTEGER CONSTRAINT FK_Product_Id REFERENCES Product (Id) NOT NULL,
    SellerProductId INTEGER NOT NULL
);
```

No indexes exist on either table beyond the implicit primary keys. No unique constraints. `sqlite_sequence` holds one row, `('Product', 975)`.

### `Product` — 975 rows

| Column | Type | Null | Notes |
| --- | --- | --- | --- |
| `Id` | `INTEGER` | no | PK, autoincrement. Contiguous 1–975, no gaps |
| `Name` | `TEXT` | no | 975 distinct, length 9–36 |
| `Brand` | `TEXT` | **yes** | 639 distinct, 119 null (12.2%), length 2–21 |
| `Category` | `TEXT` | **yes** | 43 distinct, 34 null (3.5%), length 4–19 |

Data quality: no leading or trailing whitespace, no doubled internal spaces, no accented characters, all ASCII. No product name repeats, and no name is shared by two products under different brands.

Nulls are meaningful volume, not edge cases — 119 products have no brand, 34 no category. Any matching rule has to treat null as a first-class value.

`Category` distribution, all 43 values plus null:

```
Kitchen 133   Sports 105   Outdoor 63   Furniture 53   Apparel 49   Fitness 49
Medical 45    Accessories 37   <null> 34   Home Decor 32   Supplements 30
Electronics 25   Garden 25   Cleaning 24   Baby Products 21   Smart Home 18
Bedding 17    Footwear 17   Photography 17   Home Appliances 15   Pet Care 15
Bathroom 14   Tools 14   Automotive 13   Storage 13   Audio 12   Office 10
Personal Care 10   Bags 8   Musical Instruments 7   Components 6   Computers 6
Cables 5   Crafts 5   Home 5   Watches 5   Monitors 4   Gaming 3   Tablets 3
Wearables 3   Beach 2   Lighting 1   Networking 1   Toys 1
```

### `SellerProduct` — 0 rows

Empty as supplied, and has no `sqlite_sequence` entry yet. It is the offer-level table: one row is intended to record that a seller offers a product.

| Column | Type | Null | Notes |
| --- | --- | --- | --- |
| `Id` | `INTEGER` | no | PK, autoincrement |
| `SellerName` | `TEXT` | no | the only seller attribute anywhere in the system |
| `ProductId` | `INTEGER` | no | `REFERENCES Product (Id)`, constraint named `FK_Product_Id` |
| `SellerProductId` | `INTEGER` | no | the seller's own identifier for the product |

The declared foreign key is `NO ACTION` on both update and delete, and **SQLite does not enforce it unless `PRAGMA foreign_keys = ON` is set** on the connection. It defaults to off.

## 2. Input file as supplied

`data/ProductEntry.json` — 48938 bytes, UTF-8, a JSON array of 269 objects. No wrapper object, no metadata, no pagination.

```json
[
  {
    "Id": "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d",
    "SellerName": "MegaStore",
    "Name": "Smartphone Galaxy S23",
    "Brand": "Samsung",
    "Category": "Electronics"
  }
]
```

Every record has exactly these five keys in this order. No record has extra or missing keys.

| Field | JSON type | Null | Distinct | Length | Notes |
| --- | --- | --- | --- | --- | --- |
| `Id` | string | no | 255 | 34–36 | the *seller's* id, not globally unique |
| `SellerName` | string | no | 20 | 8–14 | clean; no variants |
| `Name` | string | no | 265 | 11–36 | carries the variation |
| `Brand` | string / null | **3 null** | 157 | 2–24 | |
| `Category` | string | no | 28 | 4–15 | |

There is no `price`, `stock`, `sku`, `gtin`, or `ean` field. No product identifier of any kind beyond the seller's own `Id`, which is why matching is necessarily lexical.

### `Id` is not what it appears to be

Three separate problems, all deliberate:

**It is not unique.** 255 distinct values across 269 records; 14 values repeat. Thirteen of those repeats span *different sellers on unrelated products* — one id covers both `Curtain Rod Adjustable` from GardenStore and `Bookshelf 5-Shelf` from SportsHub. The id is scoped to the seller.

**It is not always a UUID.** 266 of 269 match a UUID shape. Three do not:

| Index | Value | Defect |
| --- | --- | --- |
| 92 | `ddddeee-ffff-4000-1111-222233334444` | first group has 7 hex digits, not 8 |
| 180 | `09835342345-4678-9abc-def012345678` | 4 groups instead of 5; first has 11 digits |
| 268 | `uddd0000-eeee-4111-ffff-aaaa22223333` | contains `u`, not a hex digit |

Index 268 is one character from `dddd0000-eeee-4111-ffff-aaaa22223333`, which is also present in the file, on a different record. Index 180 is the record carrying the SQL injection payload.

**Its declared column type would corrupt it.** `SellerProductId INTEGER` applies integer affinity, which rewrites numeric-looking strings. Every id here is non-numeric so nothing is harmed today, but see `D3`.

### Sellers — 20, each with 13–15 records

```
AccessoryWorld 13  AudioPro 13   CleaningWorld 13  ElectroHub 14   FitnessCenter 13
FootwearHub 13     FurnitureWorld 13  GadgetZone 15  GamingStore 13  GardenStore 15
HealthStore 13     HomeGoods 13  KitchenPlus 13    MegaStore 15    OfficeSupply 13
OutdoorGear 13     SmartHomeStore 14  SportsHub 13  SuperMart 13    TechWorld 14
```

All clean: no whitespace or casing variants, 20 distinct values before and after normalization. Seller identity needs no deduplication.

### Field variation, catalogued

The input is dirty where the catalog is clean. Counts over 269 records:

| Variation | Count | Example |
| --- | --- | --- |
| Doubled internal space in `Name` | 60 | `Smartphone  Galaxy S23` |
| Punctuation difference in `Name` | 37 | `34"` vs `34`, `12.9"` vs `12.9''` |
| Accented vs unaccented | — | `Câmera Canon EOS R6` vs `Camera Canon EOS R6` |
| Brand casing variant | — | `BLACK+DECKER` vs `Black+Decker` (both also in the catalog) |
| Null brand | 3 | `Cable Organizer Kit`, `Bed Frame Wood King`, `Round Rug 6 Feet` |
| Category disagreement | 1 | `Photo` vs `Photography` for Canon EOS R6 |
| Cross-language name | 2 | `Roteador`/`Router`, `Processador`/`Processor` |
| SQL injection payload | 1 | brand `TestBrand'; SELECT 1; --` |

Brand values containing punctuation, all legitimate except the last:

```
BLACK+DECKER   Black+Decker   Levi's   O-Cedar   Park & Sun Sports
Ray-Ban        TP-Link        Tempur-Pedic       TestBrand'; SELECT 1; --
```

`Category` has 28 distinct values, of which `Photo` is the only one absent from the catalog's 43.

## 3. Field mapping

| JSON field | Destination | Transform |
| --- | --- | --- |
| `Name` | `Product.Name` | stored verbatim on insert; never used to update an existing row |
| `Brand` | `Product.Brand` | stored verbatim, null preserved |
| `Category` | `Product.Category` | stored verbatim |
| `SellerName` | `SellerProduct.SellerName` | verbatim |
| `Id` | `SellerProduct.SellerProductId` | verbatim, opaque; requires the `TEXT` column |
| — | `SellerProduct.ProductId` | resolved by matching, or the id of a newly inserted product |
| — | `Product.Id` | assigned by the database |

Matching uses a normalized form of `Name` and `Brand` computed in memory. Nothing normalized is ever written.

## 4. Database after migration

Two changes, both from `DECISIONS.md`. Validated: the DDL below creates cleanly, `SellerProductId` reports declared type `TEXT`, both indexes exist, and `'007'` round-trips as the string `007` rather than the integer 7.

```sql
CREATE TABLE Product (                          -- unchanged
    Id       INTEGER PRIMARY KEY AUTOINCREMENT,
    Name     TEXT NOT NULL,
    Brand    TEXT,
    Category TEXT
);

CREATE TABLE SellerProduct (
    Id              INTEGER PRIMARY KEY AUTOINCREMENT,
    SellerName      TEXT NOT NULL,
    ProductId       INTEGER NOT NULL CONSTRAINT FK_Product_Id REFERENCES Product (Id),
    SellerProductId TEXT NOT NULL                -- D3: was INTEGER
);

-- D5: idempotency enforced by the schema
CREATE UNIQUE INDEX UX_SellerProduct_Listing ON SellerProduct (SellerName, SellerProductId);
CREATE UNIQUE INDEX UX_SellerProduct_Offer   ON SellerProduct (SellerName, ProductId);

PRAGMA user_version = 1;
```

`Product` is untouched. The column type change requires a table rebuild because SQLite cannot alter a declared type; the indexes do not.

### Expected state after ingesting the supplied file

| | Before | After |
| --- | --- | --- |
| `Product` rows | 975 | 978 |
| `SellerProduct` rows | 0 | 257 |
| `user_version` | 0 | 1 |
| Unique indexes | 0 | 2 |

New product ids are 976, 977, 978, following `sqlite_sequence`. Full accounting of the 269 input records is in the expected-outcome table in `DECISIONS.md`.
