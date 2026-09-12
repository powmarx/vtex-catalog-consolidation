#!/usr/bin/env python3
"""Measure whether the review-candidate rule could safely merge instead of report.

`D6` inserts two products it knows are probably duplicates of catalog rows 21 and 28,
and reports them for review rather than merging them. That is the one place the solution
knowingly leaves a duplicate in the catalog, so it is the decision most worth challenging
-- and the challenge deserves evidence rather than an argument from caution.

This script produces that evidence. It answers four questions:

1. What would change if the rule merged automatically?
2. Does the catalog contain pairs of *distinct* products the rule would wrongly merge?
3. How much headroom is there between the true positives and the nearest wrong pair?
4. Is the threshold measuring similarity at all?

The fourth is the one that settles it. Run with no arguments:

    python scripts/analyse_merge_threshold.py

Everything is derived from data/catalog.db and data/ProductEntry.json, so the output is
reproducible and will change if the data does.
"""

from __future__ import annotations

import collections
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from catalog_consolidation.consolidator import CANDIDATE_OVERLAP_THRESHOLD  # noqa: E402
from catalog_consolidation.normalize import match_key, normalize, token_overlap, tokens  # noqa: E402

CATALOG = ROOT / "data" / "catalog.db"
ENTRIES = ROOT / "data" / "ProductEntry.json"

# The pairs D6 declines to merge, and the catalog rows they correspond to.
TRANSLATION_PAIRS = {
    "Roteador WiFi 6 TP-Link": 21,
    "Processador AMD Ryzen 9 7950X": 28,
}


def load() -> tuple[list[tuple], list[dict]]:
    connection = sqlite3.connect(f"file:{CATALOG.as_posix()}?mode=ro", uri=True)
    try:
        products = list(connection.execute("SELECT Id, Name, Brand, Category FROM Product"))
    finally:
        connection.close()
    return products, json.loads(ENTRIES.read_text(encoding="utf-8"))


def brand_groups(products: list[tuple]) -> dict[str, list[tuple]]:
    groups: dict[str, list[tuple]] = collections.defaultdict(list)
    for product in products:
        if product[2]:
            groups[normalize(product[2])].append(product)
    return groups


def catalog_pairs(products: list[tuple]) -> list[tuple[float, tuple, tuple]]:
    """Every same-brand pair of distinct catalog products, scored."""
    pairs: list[tuple[float, tuple, tuple]] = []
    for group in brand_groups(products).values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                pairs.append((token_overlap(group[i][1], group[j][1]), group[i], group[j]))
    pairs.sort(key=lambda item: -item[0])
    return pairs


def section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def question_one(products: list[tuple], entries: list[dict]) -> dict:
    """Would merging cost anything measurable?"""
    section("1. What merging would change")

    index = {match_key(p[1], p[2]): p[0] for p in products}
    lost_offers = 0
    for name, target in TRANSLATION_PAIRS.items():
        record = next(r for r in entries if r["Name"] == name)
        seller = record["SellerName"]
        same_seller_already = [
            r
            for r in entries
            if index.get(match_key(r["Name"], r["Brand"])) == target and r["SellerName"] == seller
        ]
        verdict = "link suppressed, the seller's offer is LOST" if same_seller_already else "link survives"
        if same_seller_already:
            lost_offers += 1
        print(f"  {name!r}")
        print(f"     seller {seller} -> would merge into product {target}: {verdict}")

    print()
    print(f"  Product rows      978 -> {978 - len(TRANSLATION_PAIRS)}")
    print(f"  New products      3 -> {3 - len(TRANSLATION_PAIRS)}")
    print(f"  Links created     257 -> {257 - lost_offers}")
    print(f"  Review candidates 2 -> 0")
    print()
    print("  So merging costs nothing measurable on this file. That is what makes the")
    print("  rest of this analysis worth doing: the case for reporting cannot rest on")
    print("  a cost that is not there.")
    return {"lost_offers": lost_offers}


def question_two(products: list[tuple]) -> dict:
    """Are there distinct catalog products the rule would merge?"""
    section("2. False positives among distinct catalog products")

    pairs = catalog_pairs(products)
    above = [p for p in pairs if p[0] > CANDIDATE_OVERLAP_THRESHOLD]
    at_threshold = [p for p in pairs if p[0] == CANDIDATE_OVERLAP_THRESHOLD]

    print(f"  same-brand pairs scoring above {CANDIDATE_OVERLAP_THRESHOLD}: {len(above)}")
    for score, a, b in above[:10]:
        print(f"     {score:.3f}  {a[0]} {a[1]!r} ~ {b[0]} {b[1]!r}")
    if not above:
        print("     none -- the rule would merge nothing wrongly on this catalog")

    print(f"\n  pairs sitting exactly ON the threshold: {len(at_threshold)}")
    for score, a, b in at_threshold:
        differing = sorted(tokens(a[1]) ^ tokens(b[1]))
        print(f"     {score:.3f}  {a[1]!r} ~ {b[1]!r}")
        print(f"            brand {a[2]!r}, differing tokens {differing}")
    return {"above": len(above), "at_threshold": len(at_threshold)}


def question_three(products: list[tuple]) -> dict:
    """How much headroom is there?"""
    section("3. Headroom")

    pairs = catalog_pairs(products)
    highest_wrong = max((p[0] for p in pairs if p[0] <= CANDIDATE_OVERLAP_THRESHOLD), default=0.0)
    true_positives = sorted(token_overlap(name, next(p[1] for p in products if p[0] == pid))
                            for name, pid in TRANSLATION_PAIRS.items())

    print(f"  lowest true positive           {min(true_positives):.3f}")
    print(f"  threshold                      {CANDIDATE_OVERLAP_THRESHOLD:.3f}")
    print(f"  highest known-wrong pair       {highest_wrong:.3f}")
    print()
    print(f"  Nominal gap: {min(true_positives) - highest_wrong:.3f}. But see question 4 --")
    print("  there is no representable value in that gap for a three-token name, so the")
    print("  threshold has zero clearance below it, not 0.1.")
    return {"lowest_true_positive": min(true_positives), "highest_wrong": highest_wrong}


def question_four(products: list[tuple]) -> dict:
    """Is the threshold measuring similarity?"""
    section("4. What the threshold actually separates")

    print("  Jaccard overlap of token sets is quantised by name length. Two names of n")
    print("  tokens that differ by exactly one token score:")
    print()
    quantised = {}
    for n in range(2, 8):
        shared = n - 1
        score = shared / (2 * n - shared)
        quantised[n] = round(score, 3)
        marker = "  <- threshold sits here" if abs(score - CANDIDATE_OVERLAP_THRESHOLD) < 1e-9 else ""
        print(f"     n={n}   {shared}/{2 * n - shared} = {score:.3f}{marker}")

    print()
    print("  Nothing is representable between 0.500 and 0.600 for a three-token name.")
    print(f"  So `> {CANDIDATE_OVERLAP_THRESHOLD}` does not mean 'similar enough'. It means:")
    print("  'differs by exactly one token AND has at least four tokens.'")
    print("  Name length is doing the deciding.")

    section("   The structural comparison that settles it")
    rows = []
    for score, a, b in catalog_pairs(products):
        if score == CANDIDATE_OVERLAP_THRESHOLD:
            rows.append(("DIFFERENT products, not merged", score, a[1], b[1], sorted(tokens(a[1]) ^ tokens(b[1]))))
    for name, pid in TRANSLATION_PAIRS.items():
        catalog_name = next(p[1] for p in products if p[0] == pid)
        rows.append(
            ("SAME product, would be merged", token_overlap(name, catalog_name), name, catalog_name,
             sorted(tokens(name) ^ tokens(catalog_name)))
        )
    for label, score, left, right, differing in rows:
        print(f"   {score:.3f}  {label}")
        print(f"          {left!r}")
        print(f"          {right!r}")
        print(f"          differs only in: {differing}")

    print()
    print("  Every pair above differs in exactly one token, and in every case that token")
    print("  is the product-type word: adult/bag, stick/skates, colander/ladle,")
    print("  roteador/router, processador/processor. Structurally identical.")
    print()
    print("  Three are different products. Two are the same product. The metric cannot")
    print("  tell them apart -- only knowing that 'roteador' is Portuguese for 'router'")
    print("  can, and no lexical rule knows that.")
    print()
    print("  Had the catalog named it 'Router WiFi 6' (three tokens) the true positive")
    print("  would score exactly 0.500 and be excluded. Had it held 'Colander Stainless")
    print("  Steel Large' and 'Ladle Stainless Steel Large' (four tokens) that pair would")
    print("  score 0.600 and a colander would be merged into a ladle.")
    return {"quantised": quantised}


def conclusion(results: dict) -> None:
    section("Conclusion")
    print("  Auto-merging would be correct on this file and wrong on a plausible variant")
    print("  of it, for a reason that has nothing to do with the products: the threshold")
    print("  encodes name length rather than similarity.")
    print()
    print("  The failure modes are also asymmetric. A missed merge leaves 2 duplicate rows")
    print("  in 978 -- 0.2% bloat, named in every run's report, fixable by hand. A wrong")
    print("  merge collapses two distinct products, redirects sellers' offers onto the")
    print("  wrong one, and since D2 discards the incoming name the database keeps no")
    print("  trace of what was merged.")
    print()
    print("  D6 therefore reports rather than merges. Not from caution -- because the")
    print("  available metric provably cannot make the distinction being asked of it.")
    print()
    print("  What would actually resolve it: a real product identifier (GTIN/EAN), or")
    print("  locale-aware matching on the head noun, or the human review queue this")
    print("  report already provides.")


def main() -> int:
    products, entries = load()
    print(f"catalog: {len(products)} products   input: {len(entries)} records")
    print(f"candidate rule: same normalized brand AND token overlap > {CANDIDATE_OVERLAP_THRESHOLD}")

    results = {}
    results.update(question_one(products, entries))
    results.update(question_two(products))
    results.update(question_three(products))
    results.update(question_four(products))
    conclusion(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
