# Consolidation findings

- Generated: 2026-09-12T23:08:55+00:00
- Input: `data/ProductEntry.json`
- Database: `data/catalog.local.db`

## Summary

| Metric | Value |
| --- | --- |
| Records read | 269 |
| Matched an existing product | 266 |
| New products inserted | 3 |
| Links created | 257 |
| Duplicate listings suppressed | 12 |
| Records rejected | 0 |
| Distinct sellers linked | 20 |
| Catalog products | 975 -> 978 |

## Review candidates

Inserted as new products, but a catalog product shares the brand and most of the name. Matching is deliberately conservative, so these are surfaced rather than merged automatically -- a wrong merge is unrecoverable.

| Record | Inserted | Existing candidate | Overlap |
| --- | --- | --- | --- |
| 64 | `Processador AMD Ryzen 9 7950X` | 28 `Processor AMD Ryzen 9 7950X` | 0.667 |
| 57 | `Roteador WiFi 6 TP-Link` | 21 `Router WiFi 6 TP-Link` | 0.600 |

## Suppressed duplicate listings

| Reason | Records |
| --- | --- |
| duplicate offer: this seller is already recorded against this product under another id | 11 |
| duplicate listing: this seller already submitted this SellerProductId | 1 |

<details><summary>Every suppressed record</summary>

| Record | Seller | Listing id | Product |
| --- | --- | --- | --- |
| 76 | GardenStore | `e5e5e5e5-f6f6-4a7a-b8b8-c9c9c9c9c9c9` | 18 |
| 87 | GardenStore | `e6e6e5e5-f6f6-4a7a-b8b8-c9c9c9c9c9c9` | 18 |
| 257 | OfficeSupply | `aaaa1111-bbbb-4222-cccc-dddd33334444` | 781 |
| 258 | GardenStore | `bbbb2222-cccc-4333-dddd-eeee44445555` | 782 |
| 259 | CleaningWorld | `cccc3333-dddd-4444-eeee-ffff55556666` | 783 |
| 260 | FootwearHub | `dddd4444-eeee-4555-ffff-aaaa66667777` | 784 |
| 261 | AccessoryWorld | `eeee5555-ffff-4666-aaaa-bbbb77778888` | 785 |
| 262 | SmartHomeStore | `ffff6666-aaaa-4777-bbbb-cccc88889999` | 786 |
| 263 | MegaStore | `aaaa7777-bbbb-4888-cccc-dddd99990000` | 787 |
| 264 | TechWorld | `bbbb8888-cccc-4999-dddd-eeee00001111` | 788 |
| 265 | ElectroHub | `cccc9999-dddd-4000-eeee-ffff11112222` | 789 |
| 266 | GadgetZone | `dddd0000-eeee-4111-ffff-aaaa22223333` | 791 |

</details>

## Rejected records

None.

## Values not written

Existing catalog rows are never updated, so where an incoming record spelled a field differently the catalog's value was kept. Recorded so the loss is auditable.

| Field | Records |
| --- | --- |
| Name | 64 |
| Category | 1 |
| Brand | 1 |

Differences outside `Name`, which are the arguable ones:

| Record | Product | Field | Incoming | Retained |
| --- | --- | --- | --- | --- |
| 87 | 18 | Category | `Photo` | `Photography` |
| 188 | 322 | Brand | `Levi's` | `Levis` |
