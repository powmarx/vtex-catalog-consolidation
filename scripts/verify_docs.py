#!/usr/bin/env python3
"""Verify every factual claim in docs/ against the supplied artifacts.

Every number in the documentation was produced by measuring `data/catalog.db` and
`data/ProductEntry.json`. This script re-measures them and asserts the documents
still say the right thing, so a claim cannot silently rot as the code lands.

It checks four kinds of thing:

1. Artifact integrity  - the two supplied files are byte-for-byte unmodified.
2. Measured facts      - what the data actually contains, against the documented figures.
3. SQLite behaviour    - the engine-level claims the design rests on, executed rather than asserted.
4. Doc self-consistency - tables that must sum, ids that must be referenced, numbers that
                          must agree across documents, and stale wording that must not reappear.

Usage:
    python scripts/verify_docs.py            # summary, non-zero exit on failure
    python scripts/verify_docs.py -v         # list every check
    python scripts/verify_docs.py --section sqlite

No dependencies beyond the standard library.
"""

from __future__ import annotations

import argparse
import ast
import collections
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"

CATALOG = DATA / "catalog.db"
ENTRIES = DATA / "ProductEntry.json"

# sha256 of the artifacts as published by VTEX. These must never change.
CATALOG_SHA = "733ff1d9cc20253da48a9f8b33d7241503e4a06e7c68f65f7fa00ef14466c404"
ENTRIES_SHA = "1b0c861fe568c19e8b1cebcf774ee3d1d95baf8c42e35129e4ae806ece04b8f6"


# ---------------------------------------------------------------------------
# match key
#
# Prefer the real implementation so this script doubles as a regression test on
# it. Until the package exists, fall back to a local copy of the D1 rule.
# ---------------------------------------------------------------------------

try:  # pragma: no cover - depends on implementation progress
    sys.path.insert(0, str(ROOT / "src"))
    from catalog_consolidation.normalize import normalize as _normalize  # type: ignore

    NORMALIZE_SOURCE = "catalog_consolidation.normalize"
except Exception:  # ImportError, or the module not written yet
    def _normalize(value: str | None) -> str:
        if value is None:
            return ""
        decomposed = unicodedata.normalize("NFKD", str(value))
        stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
        lowered = stripped.lower()
        kept = "".join(c for c in lowered if c.isalnum() or c.isspace())
        return " ".join(kept.split())

    NORMALIZE_SOURCE = "fallback copy in verify_docs.py"


def match_key(name: str | None, brand: str | None) -> tuple[str, str]:
    return _normalize(name), _normalize(brand)


def imported_names(path: Path) -> set[str]:
    """Every name a module imports, read from its AST.

    Relative imports keep their leading dots, so `.models` is distinguishable from a
    third-party `models`. Used to check the module-boundary claims in DESIGN.md against
    the source rather than trusting them.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
    return names


# ---------------------------------------------------------------------------
# tiny check harness
# ---------------------------------------------------------------------------


class Checker:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, bool, str]] = []
        self.section = "general"

    def begin(self, section: str) -> None:
        self.section = section

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append((self.section, name, bool(ok), detail))
        return bool(ok)

    def equals(self, name: str, actual, expected) -> bool:
        ok = actual == expected
        detail = "" if ok else f"expected {expected!r}, measured {actual!r}"
        return self.check(name, ok, detail)

    def states(self, name: str, text: str, needle: str) -> bool:
        return self.check(name, needle in text, "" if needle in text else f"missing: {needle!r}")

    def absent(self, name: str, text: str, needle: str) -> bool:
        ok = needle not in text
        return self.check(name, ok, "" if ok else f"stale text present: {needle!r}")

    @property
    def failures(self):
        return [r for r in self.results if not r[2]]


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------


def read_docs() -> dict[str, str]:
    return {p.stem: p.read_text(encoding="utf-8") for p in DOCS.glob("*.md")} | {
        "README": (ROOT / "README.md").read_text(encoding="utf-8")
    }


def open_catalog() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{CATALOG.as_posix()}?mode=ro", uri=True)


# ---------------------------------------------------------------------------
# 1. artifact integrity
# ---------------------------------------------------------------------------


def check_artifacts(c: Checker) -> None:
    c.begin("artifacts")
    raw_db = CATALOG.read_bytes()
    raw_js = ENTRIES.read_bytes()
    c.equals("catalog.db size", len(raw_db), 61440)
    c.equals("ProductEntry.json size", len(raw_js), 48938)
    c.equals("catalog.db sha256 unmodified", hashlib.sha256(raw_db).hexdigest(), CATALOG_SHA)
    c.equals("ProductEntry.json sha256 unmodified", hashlib.sha256(raw_js).hexdigest(), ENTRIES_SHA)


# ---------------------------------------------------------------------------
# 2. database
# ---------------------------------------------------------------------------

PRODUCT_DDL = (
    "CREATE TABLE Product (Id INTEGER PRIMARY KEY AUTOINCREMENT, Name TEXT NOT NULL, "
    "Brand TEXT, Category TEXT)"
)
SELLERPRODUCT_DDL = (
    "CREATE TABLE SellerProduct (Id INTEGER PRIMARY KEY AUTOINCREMENT, SellerName TEXT NOT NULL, "
    "ProductId INTEGER CONSTRAINT FK_Product_Id REFERENCES Product (Id) NOT NULL, "
    "SellerProductId INTEGER NOT NULL)"
)


def check_database(c: Checker, products: list[tuple]) -> None:
    conn = open_catalog()
    q = lambda sql: list(conn.execute(sql))

    c.begin("db/integrity")
    c.equals("integrity_check ok", q("pragma integrity_check")[0][0], "ok")
    c.equals("quick_check ok", q("pragma quick_check")[0][0], "ok")
    c.equals("foreign_key_check empty", q("pragma foreign_key_check"), [])
    c.equals("freelist_count 0", q("pragma freelist_count")[0][0], 0)
    c.equals("page_size 4096", q("pragma page_size")[0][0], 4096)
    c.equals("page_count 15", q("pragma page_count")[0][0], 15)
    c.equals("encoding UTF-8", q("pragma encoding")[0][0], "UTF-8")
    c.equals("journal_mode delete", q("pragma journal_mode")[0][0], "delete")
    c.equals("user_version 0 (unmigrated)", q("pragma user_version")[0][0], 0)

    c.begin("db/schema")
    ddl = {r[0]: r[1] for r in q("select name, sql from sqlite_master where type='table'")}
    c.equals("Product DDL verbatim", ddl.get("Product"), PRODUCT_DDL)
    c.equals("SellerProduct DDL verbatim", ddl.get("SellerProduct"), SELLERPRODUCT_DDL)
    c.equals("no index on Product", q('pragma index_list("Product")'), [])
    c.equals("no index on SellerProduct", q('pragma index_list("SellerProduct")'), [])
    c.equals(
        "FK is NO ACTION/NO ACTION",
        q('pragma foreign_key_list("SellerProduct")')[0][5:7],
        ("NO ACTION", "NO ACTION"),
    )
    c.equals(
        "SellerProductId declared INTEGER",
        [r[2] for r in q('pragma table_info("SellerProduct")') if r[1] == "SellerProductId"][0],
        "INTEGER",
    )
    c.equals("sqlite_sequence Product = 975", q("select seq from sqlite_sequence where name='Product'")[0][0], 975)
    c.equals("no sqlite_sequence row for SellerProduct", q("select count(*) from sqlite_sequence where name='SellerProduct'")[0][0], 0)

    c.begin("db/content")
    c.equals("Product rows", len(products), 975)
    c.equals("SellerProduct rows", q("select count(*) from SellerProduct")[0][0], 0)
    c.equals("Product ids contiguous 1..975", q("select min(Id), max(Id), count(*) from Product")[0], (1, 975, 975))
    c.equals("Name distinct", len({p[1] for p in products}), 975)
    c.equals("Brand distinct", len({p[2] for p in products if p[2]}), 639)
    c.equals("Brand nulls", sum(1 for p in products if p[2] is None), 119)
    c.equals("Category distinct non-null", len({p[3] for p in products if p[3]}), 43)
    c.equals("Category nulls", sum(1 for p in products if p[3] is None), 34)
    c.equals("Name length range", (min(len(p[1]) for p in products), max(len(p[1]) for p in products)), (9, 36))
    c.equals("Brand length range", (min(len(p[2]) for p in products if p[2]), max(len(p[2]) for p in products if p[2])), (2, 21))
    c.equals("Category length range", (min(len(p[3]) for p in products if p[3]), max(len(p[3]) for p in products if p[3])), (4, 19))
    c.check("no empty strings, only NULL", not any(p[2] == "" or p[3] == "" for p in products))

    c.begin("db/text quality")
    values = [v for p in products for v in (p[1], p[2], p[3]) if v is not None]
    c.check("no leading/trailing whitespace", all(v == v.strip() for v in values))
    c.check("no doubled internal spaces", all("  " not in v for v in values))
    c.check("all ASCII", all(v.isascii() for v in values))
    c.check("no control characters", all(all(ord(ch) >= 32 for ch in v) for v in values))
    c.check("no combining accents", all(not any(unicodedata.combining(ch) for ch in unicodedata.normalize("NFKD", v)) for v in values))

    c.begin("db/internal consistency")
    c.equals("no two products share a normalized name", len({_normalize(p[1]) for p in products}), 975)
    c.equals(
        "no name shared by two brands",
        q("select count(*) from (select Name from Product group by Name having count(distinct coalesce(Brand,'')) > 1)")[0][0],
        0,
    )
    by_name_cat = collections.defaultdict(set)
    for p in products:
        by_name_cat[_normalize(p[1])].add(p[3])
    c.check("no product name maps to two categories", not any(len(v) > 1 for v in by_name_cat.values()))

    brands = collections.defaultdict(set)
    for p in products:
        if p[2]:
            brands[_normalize(p[2])].add(p[2])
    variants = {k: sorted(v) for k, v in brands.items() if len(v) > 1}
    c.equals("N5: exactly two brand casing variants", sorted(variants), ["blackdecker", "simplehuman"])
    c.equals("N5: simplehuman variants", variants.get("simplehuman"), ["Simplehuman", "simplehuman"])
    c.equals("N5: blackdecker variants", variants.get("blackdecker"), ["BLACK+DECKER", "Black+Decker"])
    c.equals(
        "N5: simplehuman product ids",
        sorted(p[0] for p in products if p[2] and _normalize(p[2]) == "simplehuman"),
        [451, 454, 458, 460],
    )
    c.equals(
        "N5: blackdecker product ids",
        sorted(p[0] for p in products if p[2] and _normalize(p[2]) == "blackdecker"),
        [607, 661, 663],
    )
    cats = collections.defaultdict(set)
    for p in products:
        if p[3]:
            cats[_normalize(p[3])].add(p[3])
    c.check("no category casing variants", not any(len(v) > 1 for v in cats.values()))

    c.begin("db/known rows")
    lookup = {p[0]: p for p in products}
    c.equals("product 21 is Router WiFi 6 TP-Link", lookup[21][1], "Router WiFi 6 TP-Link")
    c.equals("product 28 is Processor AMD Ryzen 9 7950X", lookup[28][1], "Processor AMD Ryzen 9 7950X")
    c.equals("product 322 brand is Levis (no apostrophe)", lookup[322][2], "Levis")
    c.equals("product 18 is Camera Canon EOS R6", lookup[18][1], "Camera Canon EOS R6")
    c.equals("next product id will be 976", max(p[0] for p in products) + 1, 976)
    conn.close()


# ---------------------------------------------------------------------------
# 3. json
# ---------------------------------------------------------------------------

UUID_SHAPE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
MALFORMED_IDS = {
    92: "ddddeee-ffff-4000-1111-222233334444",
    180: "09835342345-4678-9abc-def012345678",
    268: "uddd0000-eeee-4111-ffff-aaaa22223333",
}
FIELDS = ("Id", "SellerName", "Name", "Brand", "Category")


def check_json(c: Checker, entries: list[dict]) -> None:
    raw = ENTRIES.read_bytes()
    text = raw.decode("utf-8")

    c.begin("json/file")
    c.check("no BOM", raw[:3] != b"\xef\xbb\xbf")
    c.check("decodes as strict UTF-8", True)
    c.equals("no CR anywhere (LF endings)", text.count("\r"), 0)
    c.check("N4: no trailing newline", not text.endswith("\n"))
    c.check("no tab characters", "\t" not in text)
    c.check("no control characters besides LF", not any(ord(ch) < 32 and ch != "\n" for ch in text))
    c.check("no zero-width or non-breaking spaces", not any(ch in "\u200b\u200c\u200d\ufeff\u00a0\u2060" for ch in text))
    non_ascii = [ch for ch in text if ord(ch) > 126]
    c.equals("H3: exactly one non-ASCII character", non_ascii, ["\u00e2"])
    c.check("H3: it is NFC precomposed, not a combining sequence", "a\u0302" not in text)

    c.begin("json/structure")
    duplicate_keys: list[str] = []

    def hook(pairs):
        seen = set()
        for k, _ in pairs:
            if k in seen:
                duplicate_keys.append(k)
            seen.add(k)
        return dict(pairs)

    json.loads(text, object_pairs_hook=hook)
    c.equals("no duplicate keys in any object", duplicate_keys, [])
    c.check("top level is a list", isinstance(entries, list))
    c.equals("record count", len(entries), 269)
    c.check("every element is an object", all(isinstance(r, dict) for r in entries))
    c.check("every record has the same five keys in the same order", all(tuple(r.keys()) == FIELDS for r in entries))
    c.check("no nested containers", not any(isinstance(v, (dict, list)) for r in entries for v in r.values()))
    types = collections.Counter(type(v).__name__ for r in entries for v in r.values())
    c.equals("value types are str and None only", dict(types), {"str": 1342, "NoneType": 3})

    c.begin("json/values")
    c.equals("H6: null brands", sum(1 for r in entries if r["Brand"] is None), 3)
    c.check("nulls occur only in Brand", all(v is not None for r in entries for k, v in r.items() if k != "Brand"))
    c.check("no empty strings anywhere", not any(v == "" for r in entries for v in r.values()))
    c.check(
        "no leading or trailing whitespace in any value",
        all(v == v.strip() for r in entries for v in r.values() if v is not None),
    )
    c.equals("H1: names with doubled internal spaces", sum(1 for r in entries if "  " in r["Name"]), 60)
    c.equals("Name distinct", len({r["Name"] for r in entries}), 265)
    c.equals("Brand distinct incl null", len({r["Brand"] for r in entries}), 157)
    c.equals("Category distinct", len({r["Category"] for r in entries}), 28)
    c.equals("Name length range", (min(len(r["Name"]) for r in entries), max(len(r["Name"]) for r in entries)), (11, 36))
    c.equals(
        "Brand length range",
        (min(len(r["Brand"]) for r in entries if r["Brand"]), max(len(r["Brand"]) for r in entries if r["Brand"])),
        (2, 24),
    )
    c.check("no record would violate NOT NULL", all(r["Id"] and r["SellerName"] and r["Name"] for r in entries))

    c.begin("json/ids")
    ids = [r["Id"] for r in entries]
    c.equals("distinct ids", len(set(ids)), 255)
    c.equals("Id length range", (min(map(len, ids)), max(map(len, ids))), (34, 36))
    repeated = {k: v for k, v in collections.Counter(ids).items() if v > 1}
    c.equals("T1: repeated ids", len(repeated), 14)
    by_id = collections.defaultdict(list)
    for r in entries:
        by_id[r["Id"]].append(r)
    cross = {k: v for k, v in by_id.items() if len(v) > 1 and len({x["SellerName"] for x in v}) > 1}
    c.equals("T1: repeats spanning different sellers", len(cross), 13)
    c.equals("T2: repeats within one seller", len(repeated) - len(cross), 1)
    c.check(
        "T1: every cross-seller repeat is on a different product",
        all(len({x["Name"] for x in v}) > 1 for v in cross.values()),
    )
    malformed = {i: r["Id"] for i, r in enumerate(entries) if not UUID_SHAPE.match(r["Id"])}
    c.equals("N1: three malformed ids, at the documented indexes", malformed, MALFORMED_IDS)
    c.check(
        "N1: index 268 is one character from an id that also appears",
        "dddd0000-eeee-4111-ffff-aaaa22223333" in set(ids),
    )
    c.equals("no id is a bare integer", sum(1 for i in ids if i.isdigit()), 0)

    c.begin("json/sellers")
    sellers = {r["SellerName"] for r in entries}
    c.equals("distinct sellers", len(sellers), 20)
    c.equals("distinct after normalizing", len({_normalize(s) for s in sellers}), 20)
    c.check("seller names are clean", all(s == s.strip() and "  " not in s and s.isascii() for s in sellers))
    counts = collections.Counter(r["SellerName"] for r in entries)
    c.equals("records per seller range", (min(counts.values()), max(counts.values())), (13, 15))

    c.begin("json/duplicates")
    c.equals("no exact duplicate records", len({tuple(r[k] for k in FIELDS) for r in entries}), 269)
    without_id = collections.Counter(tuple(r[k] for k in FIELDS[1:]) for r in entries)
    c.check("no two records identical except Id", not any(v > 1 for v in without_id.values()))
    same_offer = collections.defaultdict(list)
    for r in entries:
        same_offer[(r["SellerName"],) + match_key(r["Name"], r["Brand"])].append(r)
    dupes = {k: v for k, v in same_offer.items() if len(v) > 1}
    c.equals("T2: seller/product duplicate groups", len(dupes), 11)
    c.equals("T2: surplus records", sum(len(v) - 1 for v in dupes.values()), 12)
    c.equals("T2: largest group is three copies", max(len(v) for v in dupes.values()), 3)

    c.begin("json/conflicts")
    by_product = collections.defaultdict(list)
    for r in entries:
        by_product[match_key(r["Name"], r["Brand"])].append(r)
    cat_conflicts = {k: sorted({r["Category"] for r in v}) for k, v in by_product.items() if len({r["Category"] for r in v}) > 1}
    c.equals("N2: one category disagreement", len(cat_conflicts), 1)
    c.equals("N2: it is Photo vs Photography", list(cat_conflicts.values())[0], ["Photo", "Photography"])
    c.check("no brand disagreements for the same product", not any(len({r["Brand"] for r in v}) > 1 for v in by_product.values()))
    brand_variants = collections.defaultdict(set)
    for r in entries:
        if r["Brand"]:
            brand_variants[_normalize(r["Brand"])].add(r["Brand"])
    c.equals(
        "H5: one brand casing variant in the input",
        {k: sorted(v) for k, v in brand_variants.items() if len(v) > 1},
        {"blackdecker": ["BLACK+DECKER", "Black+Decker"]},
    )

    c.begin("json/unit punctuation")
    buckets = collections.defaultdict(set)
    for r in entries:
        buckets[_normalize(r["Name"])].add(r["Name"])
    ipad = buckets["tablet ipad pro 129"]
    tv = buckets["smart tv samsung 55"]
    monitor = buckets["monitor lg ultrawide 34"]
    c.equals("H2: iPad has three spellings", len(ipad), 3)
    c.equals("H2: Smart TV has three spellings", len(tv), 3)
    c.check("H2: iPad variation is not whitespace alone", len({re.sub(r"\s+", " ", x) for x in ipad}) > 1)
    c.check("H2: Smart TV variation is not whitespace alone", len({re.sub(r"\s+", " ", x) for x in tv}) > 1)
    c.check(
        "H2: Monitor LG variation IS whitespace alone, so it belongs to H1",
        len({re.sub(r"\s+", " ", x) for x in monitor}) == 1,
    )

    c.begin("json/security")
    c.equals("T4: injection payload at index 180", entries[180]["Brand"], "TestBrand'; SELECT 1; --")
    c.equals("T4: its name", entries[180]["Name"], "Security Test Product")
    c.equals("T4: its seller", entries[180]["SellerName"], "MegaStore")
    c.equals(
        "only one record carries an injection payload",
        sum(1 for r in entries if any(tok in (r["Brand"] or "") for tok in (";", "--"))),
        1,
    )


# ---------------------------------------------------------------------------
# 4. cross-artifact and the expected outcome
# ---------------------------------------------------------------------------


def simulate(entries: list[dict], products: list[tuple]) -> dict:
    """Reproduce the documented expected outcome under D1-D6."""
    index = {match_key(p[1], p[2]): p[0] for p in products}
    next_id = max(p[0] for p in products)
    inserted: dict[tuple[str, str], int] = {}
    resolved: list[tuple[dict, int]] = []
    for r in entries:
        k = match_key(r["Name"], r["Brand"])
        if k in index:
            pid = index[k]
        elif k in inserted:
            pid = inserted[k]
        else:
            next_id += 1
            inserted[k] = next_id
            pid = next_id
        resolved.append((r, pid))

    seen: set = set()
    links = 0
    for r, pid in resolved:
        listing = ("listing", r["SellerName"], r["Id"])
        offer = ("offer", r["SellerName"], pid)
        if listing in seen or offer in seen:
            continue
        seen.add(listing)
        seen.add(offer)
        links += 1

    matched = sum(1 for r in entries if match_key(r["Name"], r["Brand"]) in index)
    return {
        "read": len(entries),
        "matched": matched,
        "inserted": len(inserted),
        "products_after": len(products) + len(inserted),
        "links": links,
        "skipped": len(entries) - links,
        "sellers": len({r["SellerName"] for r in entries}),
        "inserted_names": sorted(r["Name"] for r in entries if match_key(r["Name"], r["Brand"]) not in index),
        "resolved": resolved,
        "index": index,
    }


def check_cross(c: Checker, entries: list[dict], products: list[tuple]) -> None:
    index = {match_key(p[1], p[2]): p for p in products}

    c.begin("cross/matching")
    c.equals("D1: normalization is lossless over the catalog", len({match_key(p[1], p[2]) for p in products}), 975)
    matched = [r for r in entries if match_key(r["Name"], r["Brand"]) in index]
    unmatched = [r for r in entries if match_key(r["Name"], r["Brand"]) not in index]
    c.equals("records matched", len(matched), 266)
    c.equals("records unmatched", len(unmatched), 3)
    c.equals(
        "T3/T4: the three unmatched records",
        sorted(r["Name"] for r in unmatched),
        ["Processador AMD Ryzen 9 7950X", "Roteador WiFi 6 TP-Link", "Security Test Product"],
    )
    exact = sum(1 for r in entries if (r["Name"], r["Brand"], r["Category"]) in {(p[1], p[2], p[3]) for p in products})
    c.equals("exact triple match finds fewer", exact, 200)
    name_only = sum(1 for r in entries if _normalize(r["Name"]) in {_normalize(p[1]) for p in products})
    c.equals("D1: name alone would match the same 266", name_only, 266)
    c.equals(
        "H6: null-brand records match null-brand products 113/398/492",
        sorted(index[match_key(r["Name"], None)][0] for r in entries if r["Brand"] is None),
        [113, 398, 492],
    )
    with_brand = {_normalize(p[1]) for p in products if p[2] is not None}
    without_brand = {_normalize(p[1]) for p in products if p[2] is None}
    c.check("H6: no catalog name exists both with and without a brand", not (with_brand & without_brand))

    c.begin("cross/discarded data (N3)")
    name_diff = brand_diff = cat_diff = 0
    for r in entries:
        p = index.get(match_key(r["Name"], r["Brand"]))
        if not p:
            continue
        name_diff += r["Name"] != p[1]
        brand_diff += (r["Brand"] or None) != (p[2] or None)
        cat_diff += (r["Category"] or None) != (p[3] or None)
    c.equals("N3: matched records with a differing Name", name_diff, 64)
    c.equals("N3: matched records with a differing Brand", brand_diff, 1)
    c.equals("N3: matched records with a differing Category", cat_diff, 1)

    c.begin("cross/coverage")
    touched = {index[match_key(r["Name"], r["Brand"])][0] for r in matched}
    c.equals("distinct catalog products touched", len(touched), 198)
    c.equals("catalog products never mentioned", len(products) - len(touched), 777)
    per_product = collections.Counter(index[match_key(r["Name"], r["Brand"])][0] for r in matched)
    c.equals(
        "sellers-per-product distribution",
        dict(collections.Counter(per_product.values())),
        {1: 134, 2: 61, 3: 2, 4: 1},
    )
    c.equals("T3: Roteador corresponds to catalog product 21", index[match_key("Router WiFi 6 TP-Link", "TP-Link")][0], 21)
    c.equals("T3: Processador corresponds to catalog product 28", index[match_key("Processor AMD Ryzen 9 7950X", "AMD")][0], 28)
    c.equals("H4: Levi's matches Levis on product 322", index[match_key("Belt Leather Reversible", "Levi's")][0], 322)
    catalog_cats = {p[3] for p in products if p[3]}
    c.equals("N2: Photo is the only category absent from the catalog", sorted({r["Category"] for r in entries} - catalog_cats), ["Photo"])
    c.check("the three new products introduce no new category", all(r["Category"] in catalog_cats for r in unmatched))

    c.begin("cross/expected outcome")
    sim = simulate(entries, products)
    for field, expected in (
        ("read", 269),
        ("matched", 266),
        ("inserted", 3),
        ("products_after", 978),
        ("links", 257),
        ("skipped", 12),
        ("sellers", 20),
    ):
        c.equals(f"expected outcome: {field}", sim[field], expected)
    listings = {(r["SellerName"], r["Id"]) for r in entries}
    offers = {(r["SellerName"], pid) for r, pid in sim["resolved"]}
    c.equals("D5: UNIQUE(SellerName, SellerProductId) alone suppresses 1", len(entries) - len(listings), 1)
    c.equals("D5: UNIQUE(SellerName, ProductId) alone suppresses 12", len(entries) - len(offers), 12)
    c.check("D5: the second constraint is the one doing the work", (len(entries) - len(offers)) > (len(entries) - len(listings)))


# ---------------------------------------------------------------------------
# 5. sqlite behaviour the design depends on
# ---------------------------------------------------------------------------


def check_sqlite_behaviour(c: Checker) -> None:
    c.begin("sqlite/affinity (D3)")
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("CREATE TABLE t (v INTEGER NOT NULL)")
    conn.execute("INSERT INTO t VALUES (?)", ("a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d",))
    c.equals(
        "an INTEGER column accepts a UUID string and stores it as text",
        conn.execute("select typeof(v) from t").fetchone()[0],
        "text",
    )
    conn.execute("DELETE FROM t")
    conn.execute("CREATE TABLE txt (v TEXT)")
    for value, coerced in (("007", 7), ("0012", 12), ("1e3", 1000), (" 42", 42)):
        conn.execute("DELETE FROM t")
        conn.execute("DELETE FROM txt")
        conn.execute("INSERT INTO t VALUES (?)", (value,))
        conn.execute("INSERT INTO txt VALUES (?)", (value,))
        c.equals(f"INTEGER column rewrites {value!r}", conn.execute("select v from t").fetchone()[0], coerced)
        c.equals(f"TEXT column preserves {value!r}", conn.execute("select v from txt").fetchone()[0], value)

    conn.execute("CREATE TABLE ui (v INTEGER)")
    conn.execute("CREATE UNIQUE INDEX uxi ON ui(v)")
    conn.execute("INSERT INTO ui VALUES (?)", ("007",))
    collided = False
    try:
        conn.execute("INSERT INTO ui VALUES (?)", ("7",))
    except sqlite3.IntegrityError:
        collided = True
    c.check("D3: '007' and '7' collide on a unique INTEGER index", collided)
    conn.execute("CREATE TABLE ut (v TEXT)")
    conn.execute("CREATE UNIQUE INDEX uxt ON ut(v)")
    conn.execute("INSERT INTO ut VALUES (?)", ("007",))
    conn.execute("INSERT INTO ut VALUES (?)", ("7",))
    c.equals("D3: a TEXT column keeps them distinct", conn.execute("select count(*) from ut").fetchone()[0], 2)
    conn.close()

    c.begin("sqlite/conflict handling (DS3)")

    def fixture():
        d = sqlite3.connect(":memory:", isolation_level=None)
        d.execute("PRAGMA foreign_keys = ON")
        d.execute("CREATE TABLE P (Id INTEGER PRIMARY KEY)")
        d.execute("INSERT INTO P VALUES (1)")
        d.execute(
            "CREATE TABLE SP (SellerName TEXT NOT NULL, ProductId INTEGER NOT NULL REFERENCES P(Id), "
            "SellerProductId TEXT NOT NULL)"
        )
        d.execute("CREATE UNIQUE INDEX ux1 ON SP (SellerName, SellerProductId)")
        d.execute("CREATE UNIQUE INDEX ux2 ON SP (SellerName, ProductId)")
        return d

    d = fixture()
    sql = "INSERT INTO SP VALUES (?,?,?) ON CONFLICT DO NOTHING"
    d.execute(sql, ("s", 1, "x"))
    c.equals("DS3: rowcount is 0 when a unique index rejects the row", d.execute(sql, ("s", 1, "x")).rowcount, 0)
    c.equals("DS3: and 1 when the row is written", d.execute(sql, ("s2", 1, "y")).rowcount, 1)
    raised = False
    try:
        d.execute(sql, (None, 1, "z"))
    except sqlite3.IntegrityError:
        raised = True
    c.check("DS3: ON CONFLICT DO NOTHING raises on NOT NULL", raised)
    d.close()

    d = fixture()
    swallowed = False
    try:
        d.execute("INSERT OR IGNORE INTO SP VALUES (?,?,?)", (None, 1, "z"))
        swallowed = True
    except sqlite3.IntegrityError:
        pass
    c.check("DS3: INSERT OR IGNORE silently swallows NOT NULL, which is why it is not used", swallowed)
    d.close()

    c.begin("sqlite/pragmas and transactions (DS8)")
    d = sqlite3.connect(":memory:")
    c.equals("foreign_keys defaults to off", d.execute("pragma foreign_keys").fetchone()[0], 0)
    d.execute("CREATE TABLE P (Id INTEGER PRIMARY KEY)")
    d.execute("INSERT INTO P VALUES (1)")
    c.check("DS8: an implicit transaction is open after a write", d.in_transaction)
    d.execute("PRAGMA foreign_keys = ON")
    c.equals(
        "DS8: the pragma is silently ignored inside a transaction",
        d.execute("pragma foreign_keys").fetchone()[0],
        0,
    )
    d.close()

    d = sqlite3.connect(":memory:", isolation_level=None)
    d.execute("PRAGMA foreign_keys = ON")
    c.equals("DS8: set first on a clean connection, it takes effect", d.execute("pragma foreign_keys").fetchone()[0], 1)
    d.close()

    c.begin("sqlite/DDL rollback (DS5)")
    probe = Path(tempfile.gettempdir()) / "verify_docs_rollback_probe.db"
    probe.write_bytes(CATALOG.read_bytes())
    before = hashlib.sha256(probe.read_bytes()).hexdigest()
    d = sqlite3.connect(str(probe), isolation_level=None)
    d.execute("BEGIN")
    d.execute(
        "CREATE TABLE SellerProduct_new (Id INTEGER PRIMARY KEY AUTOINCREMENT, SellerName TEXT NOT NULL, "
        "ProductId INTEGER NOT NULL REFERENCES Product(Id), SellerProductId TEXT NOT NULL)"
    )
    d.execute("INSERT INTO SellerProduct_new SELECT * FROM SellerProduct")
    d.execute("DROP TABLE SellerProduct")
    d.execute("ALTER TABLE SellerProduct_new RENAME TO SellerProduct")
    d.execute("CREATE UNIQUE INDEX ux_a ON SellerProduct (SellerName, SellerProductId)")
    d.execute("PRAGMA user_version = 1")
    d.execute("INSERT INTO Product (Name) VALUES ('probe')")
    c.equals("inside the transaction the migration is visible", d.execute("pragma user_version").fetchone()[0], 1)
    d.execute("ROLLBACK")
    c.equals("DS5: user_version rolls back", d.execute("pragma user_version").fetchone()[0], 0)
    c.equals(
        "DS5: the column type change rolls back",
        [r[2] for r in d.execute('pragma table_info("SellerProduct")') if r[1] == "SellerProductId"][0],
        "INTEGER",
    )
    c.equals("DS5: the index rolls back", list(d.execute('pragma index_list("SellerProduct")')), [])
    c.equals("DS5: the inserted row rolls back", d.execute("select count(*) from Product").fetchone()[0], 975)
    c.equals("DS5: integrity survives", d.execute("pragma integrity_check").fetchone()[0], "ok")
    d.close()
    c.equals("DS5: the file is byte-identical after rollback", hashlib.sha256(probe.read_bytes()).hexdigest(), before)
    probe.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 6. documentation self-consistency
# ---------------------------------------------------------------------------


def check_docs(c: Checker, docs: dict[str, str]) -> None:
    c.begin("docs/presence")
    for name in ("DECISIONS", "DESIGN", "TASKS", "SCHEMA", "DATA-ISSUES", "README"):
        c.check(f"{name} exists", name in docs)

    decisions = docs["DECISIONS"]
    design = docs["DESIGN"]
    tasks = docs["TASKS"]
    schema = docs["SCHEMA"]
    issues = docs["DATA-ISSUES"]

    # Discover the ids rather than hardcoding a range, so a new decision is
    # covered the moment it is written instead of silently escaping these checks.
    c.begin("docs/ids")
    decision_ids = sorted(int(n) for n in re.findall(r"^### D(\d+) ", decisions, re.M))
    design_ids = sorted(int(n) for n in re.findall(r"^### DS(\d+) ", design, re.M))
    c.check("DECISIONS defines decisions", len(decision_ids) > 0)
    c.equals("decision ids are contiguous from D1", decision_ids, list(range(1, len(decision_ids) + 1)))
    c.equals("design ids are contiguous from DS1", design_ids, list(range(1, len(design_ids) + 1)))
    c.check("at least D1-D11 exist", len(decision_ids) >= 11, f"found D1-D{len(decision_ids)}")
    c.check("at least DS1-DS9 exist", len(design_ids) >= 9, f"found DS1-DS{len(design_ids)}")
    for i in decision_ids:
        c.check(
            f"D{i} is referenced outside DECISIONS",
            f"`D{i}`" in design or f"`D{i}`" in tasks or f"`D{i}`" in issues,
            f"D{i} is defined but never referenced",
        )
    for i in design_ids:
        c.states(f"TASKS references DS{i}", tasks, f"DS{i}")

    # The reverse direction: nothing may cite a decision or design id that does not
    # exist. A dangling `D99` is a silent documentation bug the forward check misses,
    # because the real id stays referenced from somewhere else.
    for doc_name, text in (("DECISIONS", decisions), ("DESIGN", design), ("TASKS", tasks), ("SCHEMA", schema), ("DATA-ISSUES", issues)):
        cited_d = {int(n) for n in re.findall(r"`D(\d+)`", text)}
        cited_ds = {int(n) for n in re.findall(r"`DS(\d+)`", text)}
        dangling_d = sorted(cited_d - set(decision_ids))
        dangling_ds = sorted(cited_ds - set(design_ids))
        c.check(
            f"{doc_name} cites no undefined decision",
            not dangling_d,
            f"cites D{dangling_d} which DECISIONS does not define" if dangling_d else "",
        )
        c.check(
            f"{doc_name} cites no undefined design section",
            not dangling_ds,
            f"cites DS{dangling_ds} which DESIGN does not define" if dangling_ds else "",
        )

    c.begin("docs/acceptance numbers agree")
    for value in ("269", "266", "978", "257", "12"):
        c.states(f"DECISIONS states {value}", decisions, value)
        c.states(f"TASKS states {value}", tasks, value)
    for value in ("975", "978", "257"):
        c.states(f"SCHEMA states {value}", schema, value)

    c.begin("docs/DATA-ISSUES arithmetic")
    ids = re.findall(r"^### ([THN])(\d+) ", issues, re.M)
    counts = collections.Counter(p for p, _ in ids)
    c.equals("issue headings total", len(ids), 17)
    c.equals("trap/handled/noted counts", (counts["T"], counts["H"], counts["N"]), (5, 6, 6))
    c.equals("no duplicate issue ids", len({f"{p}{n}" for p, n in ids}), len(ids))
    for prefix, total in (("T", counts["T"]), ("H", counts["H"]), ("N", counts["N"])):
        numbers = sorted(int(n) for p, n in ids if p == prefix)
        c.equals(f"{prefix} ids are 1..{total} with no gaps", numbers, list(range(1, total + 1)))
    rows = re.findall(r"^\| (?!Source|---|\*\*Total)([^|]+) \| (\d+) \| (\d+) \| (\d+) \| (\d+) \|", issues, re.M)
    c.check("summary table has rows", len(rows) >= 4)
    c.check("each row's total column is the sum of its cells", all(int(r[1]) + int(r[2]) + int(r[3]) == int(r[4]) for r in rows))
    sums = [sum(int(r[i]) for r in rows) for i in range(1, 5)]
    c.equals("summary table columns reconcile with the issue headings", tuple(sums[:3]), (counts["T"], counts["H"], counts["N"]))
    declared = re.search(r"\| \*\*Total\*\* \| \*\*(\d+)\*\* \| \*\*(\d+)\*\* \| \*\*(\d+)\*\* \| \*\*(\d+)\*\* \|", issues)
    c.check("a Total row is declared", declared is not None)
    if declared:
        c.equals("the declared Total row matches the columns", [int(x) for x in declared.groups()], sums)
        c.equals("the declared grand total is 17", int(declared.group(4)), 17)

    c.begin("docs/no stale claims")
    c.absent("D3's false premise is gone", decisions, "data cannot be loaded")
    c.absent("no unjustified Python 3.11 floor", tasks, "Python 3.11+")
    c.absent("no vague 'two exceptions'", issues, "two exceptions")
    c.absent("category count corrected", decisions, "44 categories")
    c.absent("DESIGN does not recommend INSERT OR IGNORE", design, "via `INSERT OR IGNORE`")
    c.states("DECISIONS points at DS3 for the conflict clause", decisions, "not `INSERT OR IGNORE`")
    c.states("dry-run covers the migration", design, "both the migration and the consolidation")
    c.states("the catalog's brand casing exception is stated", decisions, "brand **casing** is internally inconsistent")

    # DS3 prescribes one clause and rejects the other. Asserting only that the
    # preferred clause appears somewhere is too weak: it survives the heading
    # being inverted. Pin the prescription itself.
    c.states(
        "DS3 heading prescribes ON CONFLICT DO NOTHING over OR IGNORE",
        design,
        "### DS3 — Duplicate suppression uses `ON CONFLICT DO NOTHING`, not `OR IGNORE`",
    )
    c.states("DS3 states plainly that OR IGNORE would be a bug", design, "`INSERT OR IGNORE` would be a bug here")
    c.states("DS3 gives the reason: NOT NULL is swallowed", design, "suppresses *every* constraint violation")
    c.states("TASKS forbids OR IGNORE at the point of use", tasks, "**Not `INSERT OR IGNORE`**")
    c.states("TASKS specifies the ON CONFLICT clause", tasks, "`INSERT ... ON CONFLICT DO NOTHING`")
    c.states("DATA-ISSUES records it as T5", issues, "### T5 — `INSERT OR IGNORE` would miscount malformed records")

    # D3's premise was wrong once. Pin both the correction and its replacement reasoning.
    c.states("D3 admits the earlier premise was wrong", decisions, "That was wrong")
    c.states("D3 gives the affinity reason instead", decisions, "type affinity rather than strict typing")
    c.states("D3 names the collision consequence", decisions, "undermine `D5`")

    # D3 argues from input the supplied file does not contain, so it must say so up front
    # rather than in a trailing caveat. The count is measured, not asserted.
    conn3 = open_catalog()
    conn3.close()
    supplied_ids = [e["Id"] for e in json.loads(ENTRIES.read_text(encoding="utf-8"))]
    numeric = [i for i in supplied_ids if re.fullmatch(r"\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*", i)]
    c.equals("no supplied id is numeric in any form", len(numeric), 0)
    c.states(
        "D3 states the measured count of affected records",
        decisions,
        f"would alter **0 of {len(supplied_ids)}**",
    )
    c.states("D3 calls the migration defensive before arguing for it", decisions, "migration is therefore defensive")
    c.states("D3 frames the coercion table as prospective", decisions, "would store")
    c.states("D3 concedes the change is questionable on this data", decisions, "is a fair question")
    c.states("D3 points at the fixture that makes it bite", decisions, "--scenario numeric-ids")

    # DS8 exists because a pragma silently no-ops. Pin the specifics.
    c.states("DS8 states the pragma is ignored in a transaction", design, "silently ignored inside a transaction")
    c.states("DS8 prescribes isolation_level=None", design, "isolation_level=None")

    # D11/DS9: the candidate rule's threshold is a measured judgement call, and the
    # strict inequality is what keeps a known confusable family out. Pin both.
    c.begin("docs/findings report (D11, DS9)")
    c.states("D11 requires a report per run", decisions, "### D11 — Every run writes a findings report")
    c.states("D11 states the threshold is strict", decisions, "strictly greater than 0.5")
    c.states("D11 records the two candidate scores", decisions, "0.667")
    c.states("D11 records the second candidate score", decisions, "0.600")
    c.states("D11 records the nearest in-scope rejection", decisions, "0.143")
    c.states("D11 names the confusable family at exactly 0.500", decisions, "score exactly 0.500")
    c.states("D11 admits the threshold is under-determined", decisions, "does not finely determine the value")
    c.states("D11 excludes profiling noise", decisions, "regenerating it per run produces noise")
    c.states("DS9 explains why it is not threaded", design, "### DS9")
    c.states("DS9 cites the measured total", design, "32.9 ms")
    c.states("DS9 gives the GIL reason", design, "the GIL prevents real parallelism")
    c.states("DS9 gives the connection-affinity reason", design, "have thread affinity")
    c.states("DS9 requires writing after commit", design, "written only after `COMMIT` returns")
    c.states("DS9 names the correct scale boundary", design, "a batch boundary")
    c.states("TASKS sequences the report as task 9", tasks, "## 9. Findings report")
    c.check("reports/ is gitignored", "reports/" in (ROOT / ".gitignore").read_text(encoding="utf-8"))

    # SCHEMA.md reproduces the DDL in two states. Both must be right, and the
    # "as supplied" one must match the live database rather than a memory of it.
    c.begin("docs/SCHEMA DDL fidelity")
    marker = "## 4. Database after migration"
    c.check("SCHEMA has an 'after migration' section", marker in schema)
    supplied, _, migrated = schema.partition(marker)

    # The DDL is column-aligned for readability, so compare with all whitespace
    # removed. That makes the check independent of formatting while still pinning
    # every identifier, type and constraint.
    squash = lambda s: re.sub(r"\s+", "", s)
    conn = open_catalog()
    live_ddl = {r[0]: r[1] for r in conn.execute("select name, sql from sqlite_master where type='table'")}
    conn.close()
    for table in ("Product", "SellerProduct"):
        c.check(
            f"SCHEMA reproduces the live {table} DDL exactly",
            squash(live_ddl[table]) in squash(supplied),
            f"documented DDL does not match sqlite_master for {table}",
        )
    c.check("as-supplied DDL shows SellerProductId as INTEGER", "SellerProductIdINTEGERNOTNULL" in squash(supplied))
    c.check("as-supplied DDL does not claim TEXT", "SellerProductIdTEXT" not in squash(supplied))
    c.check("post-migration DDL shows SellerProductId as TEXT", "SellerProductIdTEXTNOTNULL" in squash(migrated))
    c.check("post-migration DDL does not still say INTEGER", "SellerProductIdINTEGER" not in squash(migrated))
    c.states("post-migration DDL creates the listing index", migrated, "UX_SellerProduct_Listing")
    c.states("post-migration DDL creates the offer index", migrated, "UX_SellerProduct_Offer")
    c.states("post-migration DDL bumps user_version", migrated, "PRAGMA user_version = 1")
    c.check("post-migration DDL leaves Product unchanged", "Product" in migrated and "-- unchanged" in migrated)

    # The after-state table used to give only row counts, so a reader could carry the
    # section-1 column figures across the ingest unchanged. It now states which move and
    # why, and those deltas are measured by running the consolidation.
    c.begin("docs/post-ingest column figures")
    import shutil as _shutil
    import tempfile as _tempfile

    sys.path.insert(0, str(ROOT / "src"))
    from catalog_consolidation import cli as _cli  # noqa: PLC0415

    workspace = Path(_tempfile.mkdtemp())
    try:
        working = workspace / "catalog.db"
        _shutil.copy(CATALOG, working)
        import contextlib as _contextlib
        import io as _io

        with _contextlib.redirect_stdout(_io.StringIO()):
            _cli.main(["--database", str(working), "--input", str(ENTRIES), "--no-report-file"])
        after = sqlite3.connect(f"file:{working.as_posix()}?mode=ro", uri=True)
        try:
            one = lambda sql: after.execute(sql).fetchone()[0]
            c.equals("Product rows after ingest", one("SELECT count(*) FROM Product"), 978)
            c.equals("distinct Brand after ingest", one("SELECT count(DISTINCT Brand) FROM Product"), 640)
            c.equals("Brand nulls after ingest", one("SELECT count(*) FROM Product WHERE Brand IS NULL"), 119)
            c.equals("distinct Category after ingest", one("SELECT count(DISTINCT Category) FROM Product"), 43)
            c.equals("Category nulls after ingest", one("SELECT count(*) FROM Product WHERE Category IS NULL"), 34)
            original = after.execute(
                "SELECT Id, Name, Brand, Category FROM Product WHERE Id <= 975 ORDER BY Id"
            ).fetchall()
        finally:
            after.close()
        pristine = open_catalog()
        try:
            baseline = pristine.execute(
                "SELECT Id, Name, Brand, Category FROM Product ORDER BY Id"
            ).fetchall()
        finally:
            pristine.close()
        c.check("the 975 original rows are byte-identical after ingest", original == baseline)
    finally:
        _shutil.rmtree(workspace, ignore_errors=True)

    c.states("SCHEMA tabulates the Brand delta", schema, "| Distinct `Brand` values | 639 | 640 |")
    c.states("SCHEMA states nothing existing changes", schema, "Nothing in the existing 975 rows changes")
    c.states("SCHEMA attributes the Brand increment", schema, "comes entirely from the injection payload")

    # MERGE-ANALYSIS.md is the evidence behind D6, the decision most open to challenge.
    # Its counter-examples are real catalog rows, so they are checked against the data
    # rather than trusted.
    c.begin("docs/merge analysis (D6)")
    merge = docs.get("MERGE-ANALYSIS", "")
    c.check("MERGE-ANALYSIS exists", bool(merge))
    c.states("D6 cites the analysis", decisions, "MERGE-ANALYSIS.md")
    c.states("D6 states the threshold encodes name length", decisions, "encodes *name length*, not similarity")
    c.states("the analysis names the quantisation", merge, "quantised by name length")
    c.states("the analysis states merging costs nothing here", merge, "cost nothing measurable")
    c.states("the analysis records the asymmetry", merge, "Asymmetric failure")

    conn = open_catalog()
    products = list(conn.execute("SELECT Id, Name, Brand FROM Product"))
    conn.close()
    by_brand: dict[str, list[tuple]] = collections.defaultdict(list)
    for product in products:
        if product[2]:
            by_brand[_normalize(product[2])].append(product)

    def overlap(left: str, right: str) -> float:
        a, b = set(_normalize(left).split()), set(_normalize(right).split())
        return len(a & b) / len(a | b) if a and b else 0.0

    scored = [
        (overlap(g[i][1], g[j][1]), g[i][1], g[j][1])
        for g in by_brand.values()
        for i in range(len(g))
        for j in range(i + 1, len(g))
    ]
    above = [s for s in scored if s[0] > 0.5]
    at_half = [s for s in scored if s[0] == 0.5]
    c.equals("no distinct catalog pair scores above the threshold", len(above), 0)
    c.equals("exactly four catalog pairs sit on the threshold", len(at_half), 4)
    # Each counter-example must appear as a table row AND be one of the measured pairs.
    # Checking only that the name appears somewhere is too weak: the names occur twice in
    # the document, so a single edit leaves one copy behind and the check still passes.
    measured_names = {s[1] for s in at_half} | {s[2] for s in at_half}
    for name in ("Tennis Racket Adult", "Hockey Stick Ice", "Colander Stainless Steel", "Nightstand Set of 2"):
        c.check(
            f"{name!r} is a real measured pair",
            name in measured_names,
            f"{name!r} is quoted as a counter-example but does not score 0.5 against a same-brand peer",
        )
        c.check(
            f"{name!r} appears as a table row in the analysis",
            f"| `{name}` |" in merge,
            "quoted in prose but not tabulated, or the wording drifted",
        )
    # The quantisation table must be arithmetically right, asserted as the whole row so a
    # single altered cell cannot hide behind the same number appearing elsewhere.
    scores = [f"{(n - 1) / (2 * n - (n - 1)):.3f}" for n in range(2, 8)]
    c.equals("quantisation arithmetic", scores, ["0.333", "0.500", "0.600", "0.667", "0.714", "0.750"])
    expected_row = "| score | 0.333 | **0.500** | 0.600 | 0.667 | 0.714 | 0.750 |"
    c.states("the analysis tabulates the quantisation exactly", merge, expected_row)
    c.states("D6 links the analysis as markdown", decisions, "[`MERGE-ANALYSIS.md`](MERGE-ANALYSIS.md)")

    # D6 claims the data is inverted relative to production: an all-English catalog with
    # three planted Portuguese outliers. Both halves are measurable.
    c.begin("docs/language distribution (D6)")
    conn2 = open_catalog()
    catalog_names = [r[0] for r in conn2.execute("SELECT Name FROM Product")]
    conn2.close()
    input_names = [e["Name"] for e in json.loads(ENTRIES.read_text(encoding="utf-8"))]

    # Only vocabulary that differs between the two languages. Words spelled identically --
    # camera, monitor, smartphone, mouse, notebook, tablet -- prove nothing, and including
    # them in a first pass produced a wrong answer.
    portuguese = {
        "roteador", "processador", "teclado", "geladeira", "liquidificador", "cafeteira",
        "ventilador", "aspirador", "cadeira", "armario", "colchao", "travesseiro", "panela",
        "garrafa", "mochila", "carteira", "relogio", "oculos", "bicicleta", "furadeira",
        "lampada", "carregador", "bateria", "impressora", "celular", "televisao", "forno",
    }
    has_pt = lambda name: bool(set(_normalize(name).split()) & portuguese)

    c.equals("all 975 catalog names are English", sum(1 for n in catalog_names if has_pt(n)), 0)
    c.equals("no catalog name carries an accent", sum(1 for n in catalog_names if not n.isascii()), 0)
    c.equals("two input names are distinctively Portuguese", sum(1 for n in input_names if has_pt(n)), 2)
    c.equals("one input name carries an accent", sum(1 for n in input_names if not n.isascii()), 1)
    english = len(input_names) - sum(1 for n in input_names if has_pt(n) or not n.isascii())
    c.equals("so 266 of 269 records are English-named", english, 266)
    # And the prose must state the measured figure, not merely a plausible one. Checking
    # the data alone left the sentence free to drift, which a mutation exposed.
    c.states(
        "D6 states the measured split",
        decisions,
        f"Of {len(input_names)} input records, {english} are English-named",
    )
    c.states("D6 states the catalog is wholly English", decisions, f"all {len(catalog_names)} catalog names are English")
    for foreign, native, pid in (
        ("Câmera Canon EOS R6", "Camera Canon EOS R6", 18),
        ("Roteador WiFi 6 TP-Link", "Router WiFi 6 TP-Link", 21),
        ("Processador AMD Ryzen 9 7950X", "Processor AMD Ryzen 9 7950X", 28),
    ):
        c.check(f"{foreign!r} is in the input", foreign in input_names)
        c.check(f"its twin {native!r} is catalog #{pid}", native in catalog_names)
        c.states(f"D6 cites the {native.split()[0]} pair", decisions, f"(#{pid})")
    c.states("D6 states the inversion", decisions, "inverted relative to production")
    c.states("D6 notes the graded difficulty", decisions, "graded in difficulty on purpose")

    # D6 once argued from "VTEX operates in Brazil", which is wrong -- the platform serves
    # retailers across dozens of countries -- and was a claim about the reader's own
    # business asserted as fact in a document addressed to them. The argument is stronger
    # made from the problem domain, and needs no such premise. Guard against reintroducing
    # one.
    c.states("D6 argues from the domain, not from the reader's company", decisions, "artifact of the exercise, not of the domain")
    for name, text in docs.items():
        c.check(
            f"{name} asserts nothing about VTEX's operations",
            not re.search(r"VTEX (operates|is based|is a Brazilian|only operates)", text),
            "a claim about the reader's business does not belong in a submission to them",
        )

    c.begin("docs/formatting")
    for name, text in docs.items():
        c.check(f"{name}: no trailing whitespace", all(l == l.rstrip() for l in text.split("\n")))
        c.check(f"{name}: fenced blocks balanced", text.count("```") % 2 == 0)

    c.begin("docs/README layout matches reality")
    readme = docs["README"]
    for path in ("data/catalog.db", "data/ProductEntry.json", "docs/DECISIONS.md", "docs/DESIGN.md", "docs/TASKS.md", "docs/SCHEMA.md", "docs/DATA-ISSUES.md"):
        c.check(f"README lists {path}", path in readme)
        c.check(f"{path} exists on disk", (ROOT / path).exists())

    # The reading order is the answer to "there are six documents and I have twenty
    # minutes". It only works if it is complete, so a new document cannot be added
    # without being placed in it, and it cannot point at one that has been removed.
    c.begin("docs/reading order is complete")
    c.states("README has a reading order", readme, "## Where to start")
    start, _, rest = readme.partition("## Where to start")
    reading_order, _, _ = rest.partition("\n## Running it")
    linked = set(re.findall(r"\]\(docs/([A-Z-]+\.md)\)", reading_order))
    on_disk = {p.name for p in DOCS.glob("*.md")}
    c.equals("every document appears in the reading order", sorted(on_disk - linked), [])
    c.equals("the reading order points at nothing missing", sorted(linked - on_disk), [])
    c.states("it names the two most challengeable decisions", reading_order, "most open to challenge")
    c.states("it says the docs are not required to review the code", reading_order, "Nothing in `docs/` is required")

    # The module boundaries are the architectural claim DESIGN.md leads with, and they
    # are checkable against the source rather than takeable on trust. They were not:
    # three places said `normalize.py` imports nothing, and it imports the MatchKey
    # alias. A boundary that is stated but unverified is a boundary that drifts.
    c.begin("docs/module boundaries match the source")
    package = ROOT / "src" / "catalog_consolidation"
    if not (package / "normalize.py").exists():
        c.check("the package is present", False, f"not found: {package}")
    else:
        c.equals(
            "normalize.py imports only unicodedata and the MatchKey alias",
            imported_names(package / "normalize.py"),
            {"__future__", "unicodedata", ".models"},
        )
        c.states(
            "DESIGN names that import set instead of claiming none",
            design,
            "imports nothing but `unicodedata` and a type alias",
        )
        c.absent("DESIGN does not claim normalize.py imports nothing at all", design, "`normalize.py` imports nothing.")
        c.absent("DESIGN does not claim no imports beyond stdlib", design, "no imports beyond stdlib")
        c.states("README names the same import set", readme, "unicodedata and a type alias")

        importers = sorted(p.name for p in package.glob("*.py") if "sqlite3" in imported_names(p))
        c.equals("only the repository and the migration import sqlite3", importers, ["migration.py", "repository.py"])
        c.states("DESIGN states the sqlite3 boundary", design, "the only module that imports `sqlite3`")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the documentation against the supplied artifacts.")
    parser.add_argument("-v", "--verbose", action="store_true", help="list every check, not just failures")
    parser.add_argument("--section", help="run only sections whose name contains this substring")
    args = parser.parse_args(argv)

    for path in (CATALOG, ENTRIES):
        if not path.exists():
            print(f"missing artifact: {path}", file=sys.stderr)
            return 2

    entries = json.loads(ENTRIES.read_text(encoding="utf-8"))
    conn = open_catalog()
    products = list(conn.execute("select Id, Name, Brand, Category from Product"))
    conn.close()
    docs = read_docs()

    c = Checker()
    check_artifacts(c)
    check_database(c, products)
    check_json(c, entries)
    check_cross(c, entries, products)
    check_sqlite_behaviour(c)
    check_docs(c, docs)

    results = c.results
    if args.section:
        results = [r for r in results if args.section.lower() in r[0].lower()]

    by_section: dict[str, list] = collections.defaultdict(list)
    for section, name, ok, detail in results:
        by_section[section].append((name, ok, detail))

    print(f"match key implementation: {NORMALIZE_SOURCE}\n")
    for section, items in by_section.items():
        failed = [i for i in items if not i[1]]
        if args.verbose or failed:
            print(f"{section}  ({len(items) - len(failed)}/{len(items)})")
            for name, ok, detail in items:
                if args.verbose or not ok:
                    mark = "ok  " if ok else "FAIL"
                    print(f"  {mark} {name}" + (f"\n         {detail}" if detail else ""))
            print()

    passed = sum(1 for r in results if r[2])
    failed = len(results) - passed
    print(f"{passed}/{len(results)} checks passed across {len(by_section)} sections")
    if failed:
        print(f"\n{failed} FAILED:")
        for section, name, ok, detail in results:
            if not ok:
                print(f"  [{section}] {name}" + (f" - {detail}" if detail else ""))
        return 1
    print("all claims in docs/ verified against the artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
