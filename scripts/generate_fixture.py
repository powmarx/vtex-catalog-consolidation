#!/usr/bin/env python3
"""Generate synthetic catalogs and seller submissions.

Every test in `tests/` other than the property tests runs against the two supplied
artifacts. That is a single fixture, and it does not contain several of the situations
the implementation claims to handle. This generator produces those situations on demand
so the claims can be tested rather than asserted.

The gaps it exists to fill, in rough order of how much they matter:

`numeric-ids`
    D3 widens `SellerProductId` to TEXT because integer affinity rewrites `007` to `7`
    and would then collide two distinct listings on the D5 unique index. Every id in the
    supplied file is a non-numeric UUID, so the entire justification for that migration
    is untested end to end.

`brand-ambiguity`
    The empty-brand match key is safe on the supplied catalog because no product name
    exists both with and without a brand -- measured, not assumed. This scenario builds
    a catalog where one does, plus two products sharing a name under different brands.

`within-run-new`
    Several sellers submitting the same product that the catalog does not have. DS1's
    index update exists for this, and it is currently only covered against a fake
    repository, never against SQLite.

`populated`
    The migration copies rows when rebuilding `SellerProduct`. The supplied table is
    empty, so the copy path only ever runs on nothing.

Usage:
    python scripts/generate_fixture.py --scenario adversarial --out build/fixtures
    python scripts/generate_fixture.py --list
    python scripts/generate_fixture.py --scenario scale --products 50000 --records 20000

Output is a directory holding `catalog.db`, `ProductEntry.json`, and `manifest.json`
describing what was planted. Deterministic: the same `--seed` gives the same bytes.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

SUPPLIED_SCHEMA = (
    "CREATE TABLE Product (Id INTEGER PRIMARY KEY AUTOINCREMENT, Name TEXT NOT NULL, "
    "Brand TEXT, Category TEXT)",
    "CREATE TABLE SellerProduct (Id INTEGER PRIMARY KEY AUTOINCREMENT, SellerName TEXT NOT NULL, "
    "ProductId INTEGER CONSTRAINT FK_Product_Id REFERENCES Product (Id) NOT NULL, "
    "SellerProductId INTEGER NOT NULL)",
)

NOUNS = (
    "Router", "Monitor", "Keyboard", "Mouse", "Headset", "Speaker", "Camera", "Tripod",
    "Blender", "Kettle", "Toaster", "Skillet", "Mattress", "Pillow", "Lamp", "Rug",
    "Backpack", "Wallet", "Sneakers", "Boots", "Dumbbell", "Treadmill", "Helmet", "Glove",
)
QUALIFIERS = ("Pro", "Max", "Lite", "Plus", "Ultra", "Mini", "Compact", "Deluxe", "Classic")
BRANDS = (
    "Acme", "Northwind", "Contoso", "Fabrikam", "Globex", "Initech", "Umbrella",
    "Soylent", "Vandelay", "Wonka", "Tyrell", "Cyberdyne",
)
CATEGORIES = ("Networking", "Displays", "Peripherals", "Audio", "Photography", "Kitchen",
              "Bedding", "Lighting", "Bags", "Footwear", "Fitness", "Outdoor")


@dataclass
class Fixture:
    products: list[tuple[str, str | None, str | None]] = field(default_factory=list)
    """(name, brand, category) rows for the catalog, in insertion order."""

    links: list[tuple[str, int, str]] = field(default_factory=list)
    """Pre-existing (seller, product_id, listing_id) rows. Exercises the migration copy."""

    records: list[dict] = field(default_factory=list)
    """The seller submissions."""

    notes: list[str] = field(default_factory=list)
    """What was planted, for the manifest and for a human reading the output."""

    expect: dict[str, object] = field(default_factory=dict)
    """Facts a test can assert without re-deriving them."""


def normalize(value: str | None) -> str:
    """A local copy of the D1 rule, so the generator does not depend on the package."""
    if value is None:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(value))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c)).lower()
    kept = "".join(c for c in stripped if c.isalnum() or c.isspace())
    return " ".join(kept.split())


class Generator:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self._used_names: set[str] = set()
        self._serial = 0

    def unique_name(self) -> str:
        """A product name whose normalized form has not been used yet.

        Uniqueness is enforced on the *normalized* form: a catalog holding two products
        that fold to one key is a different scenario, and it is planted deliberately in
        `brand-ambiguity` rather than by accident everywhere.

        The combinatorial vocabulary yields only about 21,000 distinct names, so a naive
        retry loop hangs forever once a caller asks for more than that. It happened: a
        `--products 50000` run spun until it was killed. After a bounded number of
        attempts this falls back to appending a serial number, which is guaranteed to
        terminate for any requested size.
        """
        for _ in range(24):
            parts = [self.rng.choice(NOUNS), self.rng.choice(QUALIFIERS)]
            if self.rng.random() < 0.5:
                parts.append(str(self.rng.randint(2, 99)))
            name = " ".join(parts)
            key = normalize(name)
            if key not in self._used_names:
                self._used_names.add(key)
                return name

        # Vocabulary exhausted, or unlucky. Fall back to something certain.
        while True:
            self._serial += 1
            name = f"{self.rng.choice(NOUNS)} {self.rng.choice(QUALIFIERS)} Mk{self._serial}"
            key = normalize(name)
            if key not in self._used_names:
                self._used_names.add(key)
                return name

    def sellers(self, count: int) -> list[str]:
        return [f"Seller{i:03d}" for i in range(1, count + 1)]

    def uuid_like(self) -> str:
        hexes = "0123456789abcdef"
        pick = lambda n: "".join(self.rng.choice(hexes) for _ in range(n))
        return f"{pick(8)}-{pick(4)}-4{pick(3)}-{pick(4)}-{pick(12)}"

    def dirty(self, name: str) -> str:
        """Apply one of the variations the supplied data exhibits."""
        choice = self.rng.choice(("double_space", "case", "accent", "punctuation", "clean"))
        if choice == "double_space" and " " in name:
            head, _, tail = name.partition(" ")
            return f"{head}  {tail}"
        if choice == "case":
            return name.upper()
        if choice == "accent":
            return name.replace("a", "â", 1)
        if choice == "punctuation":
            return name + '"'
        return name


# --------------------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------------------


def scenario_baseline(gen: Generator, products: int, sellers: int, records: int) -> Fixture:
    """Clean data, no traps. Every record matches an existing product exactly."""
    fx = Fixture()
    names = [gen.unique_name() for _ in range(products)]
    for name in names:
        fx.products.append((name, gen.rng.choice(BRANDS), gen.rng.choice(CATEGORIES)))

    seller_names = gen.sellers(sellers)
    seen: set[tuple[str, int]] = set()
    for _ in range(records):
        index = gen.rng.randrange(products)
        seller = gen.rng.choice(seller_names)
        if (seller, index) in seen:
            continue  # keep the baseline free of duplicate offers
        seen.add((seller, index))
        name, brand, category = fx.products[index]
        fx.records.append(
            {"Id": gen.uuid_like(), "SellerName": seller, "Name": name, "Brand": brand, "Category": category}
        )
    fx.notes.append("clean: every record matches an existing product exactly")
    fx.expect["all_matched"] = True
    return fx


def scenario_numeric_ids(gen: Generator, products: int, sellers: int, records: int) -> Fixture:
    """Seller ids that integer affinity would rewrite. The reason D3 exists."""
    fx = scenario_baseline(gen, products, sellers, max(records, 8))
    planted = ["007", "0012", "1e3", " 42", "42 ", "7", "00000000000000000009", "0x10"]
    seller = "NumericSeller"
    for offset, listing_id in enumerate(planted):
        name, brand, category = fx.products[offset % len(fx.products)]
        fx.records.append(
            {"Id": listing_id, "SellerName": seller, "Name": name, "Brand": brand, "Category": category}
        )
    fx.notes.append(
        "planted numeric-looking seller ids: "
        + ", ".join(repr(p) for p in planted)
        + ". On an unmigrated INTEGER column '007' and '7' both become 7 and collide."
    )
    fx.expect["numeric_listing_ids"] = planted
    fx.expect["numeric_seller"] = seller
    fx.expect["all_matched"] = False
    return fx


def scenario_brand_ambiguity(gen: Generator, products: int, sellers: int, records: int) -> Fixture:
    """A catalog where the empty-brand key is genuinely risky.

    The supplied catalog has no product name existing both with and without a brand, so
    `match_key(name, None)` cannot drift onto a branded product. This builds the case it
    was verified not to contain.
    """
    fx = Fixture()
    shared = gen.unique_name()
    other = gen.unique_name()

    # Same name, one with a brand and one without.
    fx.products.append((shared, None, "Peripherals"))
    fx.products.append((shared, "Acme", "Peripherals"))
    # Same name under two different brands.
    fx.products.append((other, "Globex", "Audio"))
    fx.products.append((other, "Initech", "Audio"))
    for _ in range(max(products - 4, 0)):
        fx.products.append((gen.unique_name(), gen.rng.choice(BRANDS), gen.rng.choice(CATEGORIES)))

    fx.records = [
        # Must land on the unbranded row, not the Acme one.
        {"Id": gen.uuid_like(), "SellerName": "SellerA", "Name": shared, "Brand": None, "Category": "Peripherals"},
        # Must land on the Acme row.
        {"Id": gen.uuid_like(), "SellerName": "SellerB", "Name": shared, "Brand": "Acme", "Category": "Peripherals"},
        # Must land on each brand's own row, not merge them.
        {"Id": gen.uuid_like(), "SellerName": "SellerC", "Name": other, "Brand": "Globex", "Category": "Audio"},
        {"Id": gen.uuid_like(), "SellerName": "SellerD", "Name": other, "Brand": "Initech", "Category": "Audio"},
        # A brand the catalog does not have for this name: a new product.
        {"Id": gen.uuid_like(), "SellerName": "SellerE", "Name": other, "Brand": "Wonka", "Category": "Audio"},
    ]
    fx.notes.append(
        f"{shared!r} exists both with brand Acme and with no brand; {other!r} exists under "
        "two brands. Brand is load-bearing in this catalog, unlike the supplied one."
    )
    fx.expect["shared_name"] = shared
    fx.expect["two_brand_name"] = other
    fx.expect["expected_inserted"] = 1
    return fx


def scenario_within_run_new(gen: Generator, products: int, sellers: int, records: int) -> Fixture:
    """Several sellers submitting the same product the catalog does not have.

    DS1 keeps the in-memory index updated so the first record inserts and the rest match.
    Without it this inserts duplicates -- the exact failure the assessment is about.
    """
    fx = Fixture()
    for _ in range(products):
        fx.products.append((gen.unique_name(), gen.rng.choice(BRANDS), gen.rng.choice(CATEGORIES)))

    absent = "Wholly Unseen Contraption"
    brand = "Vandelay"
    for i, seller in enumerate(gen.sellers(6)):
        fx.records.append(
            {
                "Id": gen.uuid_like(),
                "SellerName": seller,
                # Each seller spells it slightly differently.
                "Name": gen.dirty(absent) if i else absent,
                "Brand": brand.upper() if i % 2 else brand,
                "Category": "Outdoor",
            }
        )
    fx.notes.append(
        f"6 sellers submit {absent!r}, absent from the catalog, each with a different "
        "spelling. Exactly one product row must be created."
    )
    fx.expect["absent_name"] = absent
    fx.expect["expected_inserted"] = 1
    fx.expect["expected_links"] = 6
    return fx


def scenario_populated(gen: Generator, products: int, sellers: int, records: int) -> Fixture:
    """A catalog whose SellerProduct table already has rows, so the migration copies them."""
    fx = scenario_baseline(gen, products, sellers, records)
    seller_names = gen.sellers(max(sellers, 3))
    for i in range(min(25, products)):
        fx.links.append((seller_names[i % len(seller_names)], i + 1, f"pre-existing-{i:03d}"))
    fx.notes.append(f"{len(fx.links)} SellerProduct rows exist before the migration runs")
    fx.expect["preexisting_links"] = len(fx.links)
    return fx


def scenario_dirty(gen: Generator, products: int, sellers: int, records: int) -> Fixture:
    """Every record is a spelling variant of a catalog product. Nothing should be inserted."""
    fx = Fixture()
    for _ in range(products):
        fx.products.append((gen.unique_name(), gen.rng.choice(BRANDS), gen.rng.choice(CATEGORIES)))

    seller_names = gen.sellers(sellers)
    seen: set[tuple[str, int]] = set()
    for _ in range(records):
        index = gen.rng.randrange(products)
        seller = gen.rng.choice(seller_names)
        if (seller, index) in seen:
            continue
        seen.add((seller, index))
        name, brand, category = fx.products[index]
        fx.records.append(
            {
                "Id": gen.uuid_like(),
                "SellerName": seller,
                "Name": gen.dirty(name),
                "Brand": brand.upper() if gen.rng.random() < 0.5 else brand,
                "Category": category,
            }
        )
    fx.notes.append("every record is a whitespace, case, accent or punctuation variant")
    fx.expect["expected_inserted"] = 0
    return fx


def scenario_duplicates(gen: Generator, products: int, sellers: int, records: int) -> Fixture:
    """Both duplicate shapes, so each unique index is exercised separately."""
    fx = scenario_baseline(gen, products, sellers, records)
    name, brand, category = fx.products[0]
    shared_id = gen.uuid_like()

    # Same listing id twice from one seller: caught by (SellerName, SellerProductId).
    fx.records.append({"Id": shared_id, "SellerName": "DupSeller", "Name": name, "Brand": brand, "Category": category})
    fx.records.append({"Id": shared_id, "SellerName": "DupSeller", "Name": name, "Brand": brand, "Category": category})

    # Same product under two ids from one seller: caught by (SellerName, ProductId).
    second_name, second_brand, second_category = fx.products[1]
    fx.records.append(
        {"Id": gen.uuid_like(), "SellerName": "DupSeller", "Name": second_name, "Brand": second_brand, "Category": second_category}
    )
    fx.records.append(
        {"Id": gen.uuid_like(), "SellerName": "DupSeller", "Name": gen.dirty(second_name), "Brand": second_brand, "Category": second_category}
    )

    # The same id reused by a different seller on a different product: must not merge.
    third_name, third_brand, third_category = fx.products[2]
    fx.records.append(
        {"Id": shared_id, "SellerName": "OtherSeller", "Name": third_name, "Brand": third_brand, "Category": third_category}
    )
    fx.notes.append("planted 2 suppressible duplicates and one cross-seller id reuse that must NOT be suppressed")
    fx.expect["planted_suppressions"] = 2
    return fx


def scenario_malformed(gen: Generator, products: int, sellers: int, records: int) -> Fixture:
    """Records that cannot be processed, mixed among good ones."""
    fx = scenario_baseline(gen, products, sellers, records)
    name, brand, category = fx.products[0]
    bad = [
        {"Id": gen.uuid_like(), "SellerName": "X", "Name": None, "Brand": brand, "Category": category},
        {"Id": gen.uuid_like(), "SellerName": None, "Name": name, "Brand": brand, "Category": category},
        {"Id": None, "SellerName": "X", "Name": name, "Brand": brand, "Category": category},
        {"Id": gen.uuid_like(), "SellerName": "X", "Name": "   ", "Brand": brand, "Category": category},
        {"Id": gen.uuid_like(), "SellerName": "X", "Name": name, "Brand": 12345, "Category": category},
        {"Brand": brand},
    ]
    # Interleave so a failure cannot be mistaken for a truncated run.
    for offset, record in enumerate(bad):
        fx.records.insert(min(offset * 3, len(fx.records)), record)
    fx.notes.append(f"{len(bad)} malformed records interleaved among good ones")
    fx.expect["expected_rejections"] = len(bad)
    return fx


def scenario_scale(gen: Generator, products: int, sellers: int, records: int) -> Fixture:
    """A large catalog, for timing rather than correctness."""
    fx = scenario_baseline(gen, products, sellers, records)
    fx.notes.append(f"scale: {products} products, {len(fx.records)} records, {sellers} sellers")
    return fx


def scenario_adversarial(gen: Generator, products: int, sellers: int, records: int) -> Fixture:
    """Everything at once, which is how real ingest arrives."""
    fx = scenario_baseline(gen, products, sellers, records)
    fx.expect.pop("all_matched", None)  # inherited from baseline and false here

    # Dirty roughly half the clean records. Without this the scenario claimed to be
    # everything at once while every matched record spelled its product exactly, so the
    # discarded-value path never ran and the report said "Values not written: None".
    for record in fx.records:
        if gen.rng.random() < 0.5:
            record["Name"] = gen.dirty(record["Name"])
            if record["Brand"] and gen.rng.random() < 0.4:
                record["Brand"] = record["Brand"].upper()
    fx.notes.append("about half the matching records carry a spelling variant")

    for builder in (scenario_numeric_ids, scenario_duplicates, scenario_malformed):
        piece = builder(Generator(random.Random(gen.rng.random())), 6, 3, 6)
        piece_names = {p[0] for p in piece.products}
        for record in piece.records:
            # Borrowed records refer to the piece's own catalog, which this fixture does
            # not have, so re-point them at a product that exists here.
            #
            # Only records that are *not* deliberately malformed. Rewriting a planted
            # defect cures it: an earlier version overwrote Brand unconditionally and
            # silently repaired the wrong-type record, so the scenario claimed six
            # malformations and delivered five.
            if not _is_deliberately_malformed(record) and record.get("Name") in piece_names:
                name, brand, category = fx.products[gen.rng.randrange(len(fx.products))]
                record["Name"], record["Brand"], record["Category"] = name, brand, category
            fx.records.append(record)
        fx.notes.extend(piece.notes)
        for key, value in piece.expect.items():
            if key != "all_matched":
                fx.expect[key] = value

    absent = "Adversarial Unseen Widget"
    for seller in gen.sellers(4):
        fx.records.append(
            {"Id": gen.uuid_like(), "SellerName": seller, "Name": gen.dirty(absent), "Brand": "Wonka", "Category": "Outdoor"}
        )
    fx.records.append(
        {"Id": "1e3", "SellerName": "Injector", "Name": "Robert'); DROP TABLE Product; --",
         "Brand": "'; SELECT 1; --", "Category": "Outdoor"}
    )
    fx.notes.append("plus a within-run new product from 4 sellers and an injection payload")
    gen.rng.shuffle(fx.records)

    # Derived from the shuffled result rather than assumed, so the manifest cannot drift
    # from the file it describes.
    fx.expect["expected_rejections"] = sum(1 for r in fx.records if _is_deliberately_malformed(r))
    fx.expect["absent_name"] = absent
    return fx


def _is_deliberately_malformed(record: dict) -> bool:
    """Whether a record cannot be loaded, so the generator leaves it alone."""
    for field_name in ("Id", "SellerName", "Name"):
        value = record.get(field_name)
        if value is None or (isinstance(value, str) and not value.strip()):
            return True
    return any(
        record.get(f) is not None and not isinstance(record.get(f), str)
        for f in ("Id", "SellerName", "Name", "Brand", "Category")
    )


SCENARIOS = {
    "baseline": scenario_baseline,
    "numeric-ids": scenario_numeric_ids,
    "brand-ambiguity": scenario_brand_ambiguity,
    "within-run-new": scenario_within_run_new,
    "populated": scenario_populated,
    "dirty": scenario_dirty,
    "duplicates": scenario_duplicates,
    "malformed": scenario_malformed,
    "scale": scenario_scale,
    "adversarial": scenario_adversarial,
}


# --------------------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------------------


def build(
    scenario: str,
    out: Path,
    *,
    products: int = 200,
    sellers: int = 12,
    records: int = 300,
    seed: int = 20260912,
) -> dict:
    if scenario not in SCENARIOS:
        raise SystemExit(f"unknown scenario {scenario!r}; choose from {', '.join(sorted(SCENARIOS))}")

    gen = Generator(random.Random(seed))
    fixture = SCENARIOS[scenario](gen, products, sellers, records)

    out.mkdir(parents=True, exist_ok=True)
    database = out / "catalog.db"
    database.unlink(missing_ok=True)

    connection = sqlite3.connect(str(database), isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in SUPPLIED_SCHEMA:
            connection.execute(statement)
        connection.execute("BEGIN")
        connection.executemany(
            "INSERT INTO Product (Name, Brand, Category) VALUES (?, ?, ?)", fixture.products
        )
        if fixture.links:
            connection.executemany(
                "INSERT INTO SellerProduct (SellerName, ProductId, SellerProductId) VALUES (?, ?, ?)",
                fixture.links,
            )
        connection.execute("COMMIT")
    finally:
        connection.close()

    (out / "ProductEntry.json").write_text(
        json.dumps(fixture.records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    manifest = {
        "scenario": scenario,
        "seed": seed,
        "catalog_products": len(fixture.products),
        "preexisting_links": len(fixture.links),
        "records": len(fixture.records),
        "notes": fixture.notes,
        "expect": fixture.expect,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a synthetic catalog and seller submissions.")
    parser.add_argument("--scenario", default="adversarial", help="which situation to build")
    parser.add_argument("--out", default="build/fixtures", help="output directory")
    parser.add_argument("--products", type=int, default=200)
    parser.add_argument("--sellers", type=int, default=12)
    parser.add_argument("--records", type=int, default=300)
    parser.add_argument("--seed", type=int, default=20260912, help="same seed gives the same bytes")
    parser.add_argument("--list", action="store_true", help="describe the scenarios and exit")
    args = parser.parse_args(argv)

    if args.list:
        for name, builder in sorted(SCENARIOS.items()):
            summary = (builder.__doc__ or "").strip().split("\n")[0]
            print(f"  {name:<17} {summary}")
        return 0

    manifest = build(
        args.scenario,
        Path(args.out),
        products=args.products,
        sellers=args.sellers,
        records=args.records,
        seed=args.seed,
    )
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
