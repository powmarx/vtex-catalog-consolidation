"""Tests for the match key (D1).

Every case here is a real pair from the supplied data, not an invented one. The
losslessness test runs against the actual catalog, because "normalization is safe"
is only a meaningful claim about a specific corpus.
"""

from __future__ import annotations

import collections
import json
import sqlite3
import unittest
from pathlib import Path

from catalog_consolidation.normalize import match_key, normalize, token_overlap, tokens

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "data" / "catalog.db"
ENTRIES = ROOT / "data" / "ProductEntry.json"


class TestNormalizeRule(unittest.TestCase):
    def test_collapses_doubled_internal_space(self):
        # 60 of the 269 input records carry one of these.
        for dirty, clean in [
            ("Smartphone  Galaxy S23", "Smartphone Galaxy S23"),
            ("iPhone 15  Pro", "iPhone 15 Pro"),
            ("Football Official  Size", "Football Official Size"),
            ("Goalkeeper  Gloves", "Goalkeeper Gloves"),
            ("Omega-3  Fish Oil", "Omega-3 Fish Oil"),
        ]:
            with self.subTest(dirty=dirty):
                self.assertEqual(normalize(dirty), normalize(clean))

    def test_strips_accents(self):
        self.assertEqual(normalize("Câmera Canon EOS R6"), normalize("Camera Canon EOS R6"))
        self.assertEqual(normalize("Câmera Canon EOS R6"), "camera canon eos r6")

    def test_handles_precomposed_and_decomposed_accents_alike(self):
        # The file uses NFC (U+00E2). A different exporter might emit NFD.
        self.assertEqual(normalize("C\u00e2mera"), normalize("Ca\u0302mera"))

    def test_drops_punctuation_without_substituting_a_space(self):
        # The reason for the rule: the input has Levi's, the catalog has Levis.
        # Replacing punctuation with a space would give "levi s" and fail to match.
        self.assertEqual(normalize("Levi's"), "levis")
        self.assertEqual(normalize("Levi's"), normalize("Levis"))
        self.assertNotEqual(normalize("Levi's"), "levi s")

    def test_unit_marks_converge(self):
        for variants in [
            ('Tablet iPad Pro 12.9"', "Tablet iPad Pro 12.9''", "Tablet iPad Pro 12.9"),
            ('Smart TV Samsung 55"', "Smart TV Samsung 55", 'Smart TV Samsung  55"'),
            ('Monitor LG UltraWide 34"', 'Monitor LG UltraWide  34"'),
        ]:
            with self.subTest(variants=variants):
                self.assertEqual(len({normalize(v) for v in variants}), 1)

    def test_lowercases(self):
        self.assertEqual(normalize("BLACK+DECKER"), normalize("Black+Decker"))
        self.assertEqual(normalize("simplehuman"), normalize("Simplehuman"))
        self.assertEqual(normalize("BLACK+DECKER"), "blackdecker")

    def test_none_and_blank_collapse_to_empty(self):
        self.assertEqual(normalize(None), "")
        self.assertEqual(normalize(""), "")
        self.assertEqual(normalize("   "), "")

    def test_null_brand_is_a_value_not_a_wildcard(self):
        # A record with no brand must not match a product that has one.
        self.assertEqual(match_key("Cable Organizer Kit", None), ("cable organizer kit", ""))
        self.assertNotEqual(
            match_key("Cable Organizer Kit", None),
            match_key("Cable Organizer Kit", "Anker"),
        )

    def test_preserves_alphanumerics_that_matter(self):
        # Model numbers must survive; only separators go.
        self.assertEqual(normalize("Headphones Sony WH-1000XM5"), "headphones sony wh1000xm5")
        self.assertEqual(normalize("Processor AMD Ryzen 9 7950X"), "processor amd ryzen 9 7950x")

    def test_does_not_bridge_languages(self):
        # D6's documented limitation, asserted so it stays deliberate.
        self.assertNotEqual(
            match_key("Roteador WiFi 6 TP-Link", "TP-Link"),
            match_key("Router WiFi 6 TP-Link", "TP-Link"),
        )

    def test_is_idempotent(self):
        for value in ["Smartphone  Galaxy S23", "Câmera Canon EOS R6", "Levi's", None]:
            with self.subTest(value=value):
                once = normalize(value)
                self.assertEqual(normalize(once), once)


class TestNormalizeIsLosslessOnTheCatalog(unittest.TestCase):
    """The key must not merge two products that are genuinely different."""

    @classmethod
    def setUpClass(cls):
        conn = sqlite3.connect(f"file:{CATALOG.as_posix()}?mode=ro", uri=True)
        cls.products = list(conn.execute("select Id, Name, Brand from Product"))
        conn.close()

    def test_every_catalog_product_has_a_distinct_key(self):
        keys = {match_key(name, brand) for _, name, brand in self.products}
        self.assertEqual(len(self.products), 975, "catalog changed; other expectations need review")
        self.assertEqual(
            len(keys),
            len(self.products),
            "normalization merges two distinct catalog products",
        )

    def test_no_catalog_name_exists_both_with_and_without_a_brand(self):
        # What makes the empty-brand key safe.
        with_brand = {normalize(n) for _, n, b in self.products if b is not None}
        without_brand = {normalize(n) for _, n, b in self.products if b is None}
        self.assertEqual(with_brand & without_brand, set())


class TestTokenOverlap(unittest.TestCase):
    """The D11 candidate rule's similarity measure."""

    def test_scores_the_two_translation_pairs(self):
        self.assertAlmostEqual(
            token_overlap("Roteador WiFi 6 TP-Link", "Router WiFi 6 TP-Link"), 0.600, places=3
        )
        self.assertAlmostEqual(
            token_overlap("Processador AMD Ryzen 9 7950X", "Processor AMD Ryzen 9 7950X"),
            0.667,
            places=3,
        )

    def test_the_confusable_pairs_score_exactly_one_half(self):
        # This is why the D11 threshold is a strict inequality. These are different
        # products and must never be proposed.
        for a, b in [
            ("Hockey Stick Ice", "Hockey Skates Ice"),
            ("Bar Stools Set of 2", "Nightstand Set of 2"),
        ]:
            with self.subTest(pair=(a, b)):
                self.assertEqual(token_overlap(a, b), 0.5)
                self.assertFalse(token_overlap(a, b) > 0.5, "strict threshold must exclude this")

    def test_identical_names_score_one(self):
        self.assertEqual(token_overlap("Router WiFi 6", "Router  WiFi 6"), 1.0)

    def test_empty_scores_zero(self):
        self.assertEqual(token_overlap(None, "Router"), 0.0)
        self.assertEqual(token_overlap("", ""), 0.0)

    def test_is_symmetric(self):
        a, b = "Roteador WiFi 6 TP-Link", "Router WiFi 6 TP-Link"
        self.assertEqual(token_overlap(a, b), token_overlap(b, a))

    def test_tokens_are_normalized(self):
        self.assertEqual(tokens("Câmera  Canon"), frozenset({"camera", "canon"}))


class TestAgainstTheRealInput(unittest.TestCase):
    """Counts that the documentation states, asserted against the file itself."""

    @classmethod
    def setUpClass(cls):
        cls.entries = json.loads(ENTRIES.read_text(encoding="utf-8"))

    def test_normalization_folds_the_documented_number_of_variants(self):
        """265 raw spellings name 201 distinct products.

        The arithmetic is asserted rather than the bare total, so a change tells you
        *how* the folding shifted instead of just that a constant moved: 62 groups
        carry more than one spelling -- 60 pairs and 2 triples -- which removes
        60 + 2*2 = 64 spellings, and 265 - 64 = 201.
        """
        raw = {e["Name"] for e in self.entries}
        groups = collections.defaultdict(set)
        for name in raw:
            groups[normalize(name)].add(name)
        multi = {k: v for k, v in groups.items() if len(v) > 1}
        reductions = sum(len(v) - 1 for v in multi.values())

        self.assertEqual(len(raw), 265, "distinct raw spellings")
        self.assertEqual(len(multi), 62, "groups holding more than one spelling")
        self.assertEqual(sum(1 for v in multi.values() if len(v) == 2), 60, "pairs")
        self.assertEqual(sum(1 for v in multi.values() if len(v) == 3), 2, "triples")
        self.assertEqual(reductions, 64, "spellings removed by folding")
        self.assertEqual(len(groups), len(raw) - reductions)
        self.assertEqual(len(groups), 201, "distinct products named in the file")

    def test_input_matches_the_catalog_for_266_records(self):
        conn = sqlite3.connect(f"file:{CATALOG.as_posix()}?mode=ro", uri=True)
        index = {match_key(n, b) for _, n, b in conn.execute("select Id, Name, Brand from Product")}
        conn.close()
        matched = sum(1 for e in self.entries if match_key(e["Name"], e["Brand"]) in index)
        self.assertEqual(matched, 266)


if __name__ == "__main__":
    unittest.main()
