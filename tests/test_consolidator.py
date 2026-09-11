"""Tests for the consolidation algorithm (D1, D2, D4, D5, DS1, DS6).

These run against an in-memory fake repository, with no database involved. That is the
point of the boundary: the algorithm is testable without SQL, so a failure here is a
failure of the logic rather than of the storage.
"""

from __future__ import annotations

import unittest

from catalog_consolidation.consolidator import (
    CANDIDATE_OVERLAP_THRESHOLD,
    consolidate,
    find_review_candidates,
)
from catalog_consolidation.models import MatchKey, Product, RecordError, SellerEntry, Verdict
from catalog_consolidation.normalize import match_key


class FakeRepository:
    """A `CatalogRepository` backed by dictionaries.

    Enforces the two D5 unique constraints, because suppression behaviour is part of
    what these tests are about.
    """

    def __init__(self, products: list[Product] | None = None) -> None:
        self.products: dict[int, Product] = {p.product_id: p for p in (products or [])}
        self._next_id = max(self.products, default=0) + 1
        self.listings: set[tuple[str, str]] = set()
        self.offers: set[tuple[str, int]] = set()
        self.links: list[tuple[str, int, str]] = []
        self.inserts: list[Product] = []

    def load_index(self) -> dict[MatchKey, int]:
        return {match_key(p.name, p.brand): p.product_id for p in self.products.values()}

    def product_count(self) -> int:
        return len(self.products)

    def fetch_product(self, product_id: int) -> Product | None:
        return self.products.get(product_id)

    def iter_products(self):
        return iter(list(self.products.values()))

    def insert_product(self, name: str, brand: str | None, category: str | None) -> int:
        product = Product(self._next_id, name, brand, category)
        self.products[product.product_id] = product
        self.inserts.append(product)
        self._next_id += 1
        return product.product_id

    def link(self, seller_name: str, product_id: int, seller_product_id: str) -> bool:
        if seller_name is None or seller_product_id is None:
            raise ValueError("NOT NULL violation")
        listing = (seller_name, seller_product_id)
        offer = (seller_name, product_id)
        if listing in self.listings or offer in self.offers:
            return False
        self.listings.add(listing)
        self.offers.add(offer)
        self.links.append((seller_name, product_id, seller_product_id))
        return True


def entry(index: int, seller: str, name: str, brand: str | None = None, category: str | None = None, entry_id: str | None = None) -> SellerEntry:
    return SellerEntry(
        entry_id=entry_id or f"id-{index}",
        seller_name=seller,
        name=name,
        brand=brand,
        category=category,
        source_index=index,
    )


CATALOG = [
    Product(1, "Router WiFi 6 TP-Link", "TP-Link", "Networking"),
    Product(2, "Smartphone Galaxy S23", "Samsung", "Electronics"),
    Product(3, "Cable Organizer Kit", None, "Accessories"),
]


class TestMatching(unittest.TestCase):
    def test_an_existing_product_is_matched_not_inserted(self):
        repo = FakeRepository(CATALOG)
        report = consolidate([entry(0, "MegaStore", "Smartphone Galaxy S23", "Samsung")], repo)
        self.assertEqual(report.matched, 1)
        self.assertEqual(report.inserted, 0)
        self.assertEqual(repo.inserts, [])
        self.assertEqual(repo.links, [("MegaStore", 2, "id-0")])

    def test_spelling_variants_still_match(self):
        repo = FakeRepository(CATALOG)
        report = consolidate(
            [
                entry(0, "A", "Smartphone  Galaxy S23", "Samsung"),
                entry(1, "B", "smartphone galaxy s23", "SAMSUNG"),
            ],
            repo,
        )
        self.assertEqual(report.matched, 2)
        self.assertEqual(report.inserted, 0)

    def test_an_absent_product_is_inserted_and_linked(self):
        repo = FakeRepository(CATALOG)
        report = consolidate([entry(0, "MegaStore", "Brand New Widget", "Acme")], repo)
        self.assertEqual(report.inserted, 1)
        self.assertEqual(report.matched, 0)
        self.assertEqual(len(repo.inserts), 1)
        self.assertEqual(repo.inserts[0].name, "Brand New Widget")

    def test_a_null_brand_matches_only_a_null_brand_product(self):
        repo = FakeRepository(CATALOG)
        report = consolidate(
            [
                entry(0, "A", "Cable Organizer Kit", None),
                entry(1, "B", "Cable Organizer Kit", "Anker"),
            ],
            repo,
        )
        self.assertEqual(report.matched, 1, "the null-brand record matches product 3")
        self.assertEqual(report.inserted, 1, "the branded record is a different product")


class TestWithinRunDeduplication(unittest.TestCase):
    """DS1: the index must be updated as products are inserted."""

    def test_two_records_for_the_same_new_product_insert_it_once(self):
        # Not present in the supplied file, so constructed deliberately.
        repo = FakeRepository(CATALOG)
        report = consolidate(
            [
                entry(0, "SellerOne", "Entirely New Gadget", "Acme"),
                entry(1, "SellerTwo", "Entirely  New Gadget", "ACME"),
            ],
            repo,
        )
        self.assertEqual(len(repo.inserts), 1, "the second record must reuse the first insert")
        self.assertEqual(repo.product_count(), len(CATALOG) + 1)
        self.assertEqual({pid for _, pid, _ in repo.links}, {4})
        # `inserted` counts new Product rows, so the pair is one insertion and one match.
        self.assertEqual(report.inserted, 1)
        self.assertEqual(report.matched, 1)
        self.assertEqual(report.links_created, 2)

    def test_inserted_always_equals_the_change_in_product_count(self):
        """The invariant that pins the meaning of `inserted`."""
        repo = FakeRepository(CATALOG)
        report = consolidate(
            [
                entry(0, "A", "New One", "Acme"),
                entry(1, "B", "New  One", "ACME"),
                entry(2, "C", "New Two", "Acme"),
                entry(3, "D", "Smartphone Galaxy S23", "Samsung"),
            ],
            repo,
        )
        self.assertEqual(report.inserted, report.products_after - report.products_before)
        self.assertEqual(report.inserted, 2)
        self.assertEqual(report.matched + report.inserted, report.records_read)

    def test_the_second_record_is_reported_as_matching_a_row_created_this_run(self):
        repo = FakeRepository(CATALOG)
        report = consolidate(
            [
                entry(0, "SellerOne", "Entirely New Gadget", "Acme"),
                entry(1, "SellerTwo", "Entirely  New Gadget", "ACME"),
            ],
            repo,
        )
        self.assertEqual(report.outcomes[0].detail, "new product created")
        self.assertEqual(report.outcomes[1].detail, "linked to a product created earlier in this run")

    def test_nothing_is_reported_as_discarded_against_a_row_created_this_run(self):
        # The row holds the first record's own values, so a spelling difference against
        # it is not the catalog refusing an update -- it is the same run's own data.
        repo = FakeRepository(CATALOG)
        report = consolidate(
            [
                entry(0, "SellerOne", "Entirely New Gadget", "Acme"),
                entry(1, "SellerTwo", "Entirely  New Gadget", "ACME"),
            ],
            repo,
        )
        self.assertEqual(report.discarded, [])

    def test_three_records_for_the_same_new_product_still_insert_once(self):
        repo = FakeRepository(CATALOG)
        consolidate(
            [
                entry(0, "A", "Novel Item", "Acme"),
                entry(1, "B", "Novel  Item", "Acme"),
                entry(2, "C", "novel item", "ACME"),
            ],
            repo,
        )
        self.assertEqual(len(repo.inserts), 1)


class TestSellerIdentity(unittest.TestCase):
    """D4: a listing is (SellerName, Id), never Id alone."""

    def test_the_same_id_across_two_sellers_on_different_products(self):
        repo = FakeRepository(CATALOG)
        shared = "00112233-4455-4667-7889-900112233445"
        report = consolidate(
            [
                entry(0, "GardenStore", "Curtain Rod Adjustable", "AmazonBasics", entry_id=shared),
                entry(1, "SportsHub", "Bookshelf 5-Shelf", "Furinno", entry_id=shared),
            ],
            repo,
        )
        self.assertEqual(len(repo.inserts), 2, "unrelated products must not be merged on a shared id")
        self.assertEqual(report.links_created, 2)
        self.assertEqual(report.suppressed, 0)

    def test_the_same_listing_twice_is_suppressed(self):
        repo = FakeRepository(CATALOG)
        report = consolidate(
            [
                entry(0, "MegaStore", "Smartphone Galaxy S23", "Samsung", entry_id="same"),
                entry(1, "MegaStore", "Smartphone Galaxy S23", "Samsung", entry_id="same"),
            ],
            repo,
        )
        self.assertEqual(report.links_created, 1)
        self.assertEqual(report.suppressed, 1)
        self.assertIn("already submitted this SellerProductId", report.outcomes[1].detail)

    def test_the_same_seller_and_product_under_a_new_id_is_suppressed(self):
        repo = FakeRepository(CATALOG)
        report = consolidate(
            [
                entry(0, "MegaStore", "Smartphone Galaxy S23", "Samsung", entry_id="first"),
                entry(1, "MegaStore", "Smartphone  Galaxy S23", "Samsung", entry_id="second"),
            ],
            repo,
        )
        self.assertEqual(report.links_created, 1)
        self.assertEqual(report.suppressed, 1)
        self.assertIn("under another id", report.outcomes[1].detail)

    def test_two_sellers_may_offer_one_product(self):
        repo = FakeRepository(CATALOG)
        report = consolidate(
            [
                entry(0, "MegaStore", "Smartphone Galaxy S23", "Samsung"),
                entry(1, "TechWorld", "Smartphone Galaxy S23", "Samsung"),
            ],
            repo,
        )
        self.assertEqual(report.links_created, 2)
        self.assertEqual(report.suppressed, 0)
        self.assertEqual(report.sellers_linked, 2)


class TestNeverUpdatesTheCatalog(unittest.TestCase):
    """D2: first write wins."""

    def test_matched_products_are_left_untouched(self):
        repo = FakeRepository(CATALOG)
        before = dict(repo.products)
        consolidate(
            [entry(0, "MegaStore", "Smartphone  Galaxy S23", "SAMSUNG", category="Phones")],
            repo,
        )
        self.assertEqual(repo.products, before)

    def test_discarded_values_are_recorded(self):
        repo = FakeRepository(CATALOG)
        report = consolidate(
            [entry(0, "MegaStore", "Smartphone  Galaxy S23", "SAMSUNG", category="Phones")],
            repo,
        )
        fields = {d.field_name: (d.incoming, d.retained) for d in report.discarded}
        self.assertEqual(fields["Name"], ("Smartphone  Galaxy S23", "Smartphone Galaxy S23"))
        self.assertEqual(fields["Brand"], ("SAMSUNG", "Samsung"))
        self.assertEqual(fields["Category"], ("Phones", "Electronics"))

    def test_identical_values_are_not_recorded_as_discarded(self):
        repo = FakeRepository(CATALOG)
        report = consolidate(
            [entry(0, "MegaStore", "Smartphone Galaxy S23", "Samsung", category="Electronics")],
            repo,
        )
        self.assertEqual(report.discarded, [])


class TestReviewCandidates(unittest.TestCase):
    """D11."""

    def test_the_translation_pair_is_proposed(self):
        repo = FakeRepository(CATALOG)
        report = consolidate([entry(0, "FootwearHub", "Roteador WiFi 6 TP-Link", "TP-Link")], repo)
        self.assertEqual(report.inserted, 1)
        self.assertEqual(len(report.candidates), 1)
        candidate = report.candidates[0]
        self.assertEqual(candidate.candidate_product_id, 1)
        self.assertEqual(candidate.candidate_name, "Router WiFi 6 TP-Link")
        self.assertAlmostEqual(candidate.overlap, 0.6, places=3)

    def test_a_pair_scoring_exactly_one_half_is_not_proposed(self):
        # The strict inequality, asserted. These are different products.
        repo = FakeRepository([Product(1, "Hockey Skates Ice", "Bauer", "Sports")])
        report = consolidate([entry(0, "SportsHub", "Hockey Stick Ice", "Bauer")], repo)
        self.assertEqual(report.inserted, 1)
        self.assertEqual(report.candidates, [], "0.5 must not clear a strictly-greater threshold")

    def test_a_different_brand_is_never_proposed(self):
        repo = FakeRepository([Product(1, "Router WiFi 6 TP-Link", "Netgear", "Networking")])
        report = consolidate([entry(0, "A", "Roteador WiFi 6 TP-Link", "TP-Link")], repo)
        self.assertEqual(report.candidates, [])

    def test_a_null_brand_proposes_nothing(self):
        repo = FakeRepository(CATALOG)
        report = consolidate([entry(0, "A", "Cable Organizer Kit Deluxe", None)], repo)
        self.assertEqual(report.candidates, [])

    def test_matched_records_are_never_searched(self):
        # Scoping is a correctness requirement, not an optimization.
        repo = FakeRepository(
            [
                Product(1, "Hockey Skates Ice", "Bauer", "Sports"),
                Product(2, "Hockey Stick Ice", "Bauer", "Sports"),
            ]
        )
        report = consolidate([entry(0, "A", "Hockey Stick Ice", "Bauer")], repo)
        self.assertEqual(report.matched, 1)
        self.assertEqual(report.candidates, [])

    def test_candidates_can_be_switched_off(self):
        repo = FakeRepository(CATALOG)
        report = consolidate(
            [entry(0, "A", "Roteador WiFi 6 TP-Link", "TP-Link")], repo, collect_candidates=False
        )
        self.assertEqual(report.candidates, [])

    def test_threshold_is_exclusive(self):
        self.assertEqual(CANDIDATE_OVERLAP_THRESHOLD, 0.5)

    def test_candidates_are_sorted_by_descending_overlap(self):
        repo = FakeRepository(
            [
                Product(1, "Router WiFi 6 TP-Link", "TP-Link", "Networking"),
                Product(2, "Router WiFi 6 TP-Link Pro Max", "TP-Link", "Networking"),
            ]
        )
        report = consolidate([entry(0, "A", "Roteador WiFi 6 TP-Link", "TP-Link")], repo)
        overlaps = [c.overlap for c in report.candidates]
        self.assertEqual(overlaps, sorted(overlaps, reverse=True))

    def test_find_review_candidates_directly(self):
        repo = FakeRepository(CATALOG)
        got = find_review_candidates([(entry(0, "A", "Roteador WiFi 6 TP-Link", "TP-Link"), 99)], repo)
        self.assertEqual(len(got), 1)


class TestReportShape(unittest.TestCase):
    def test_load_errors_are_carried_into_the_report(self):
        repo = FakeRepository(CATALOG)
        errors = [RecordError(7, "missing Name")]
        report = consolidate([entry(0, "A", "Smartphone Galaxy S23", "Samsung")], repo, errors=errors)
        self.assertEqual(report.records_read, 2, "one parsed plus one rejected")
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.exit_code(), 1)

    def test_a_clean_run_exits_zero_even_with_suppressions(self):
        repo = FakeRepository(CATALOG)
        report = consolidate(
            [
                entry(0, "MegaStore", "Smartphone Galaxy S23", "Samsung", entry_id="x"),
                entry(1, "MegaStore", "Smartphone Galaxy S23", "Samsung", entry_id="x"),
            ],
            repo,
        )
        self.assertEqual(report.suppressed, 1)
        self.assertEqual(report.exit_code(), 0, "suppressing a duplicate is the specified behaviour")

    def test_every_record_gets_exactly_one_outcome(self):
        repo = FakeRepository(CATALOG)
        entries = [entry(i, f"S{i}", "Smartphone Galaxy S23", "Samsung") for i in range(5)]
        report = consolidate(entries, repo)
        self.assertEqual(len(report.outcomes), 5)
        self.assertEqual([o.source_index for o in report.outcomes], list(range(5)))

    def test_product_counts_bracket_the_run(self):
        repo = FakeRepository(CATALOG)
        report = consolidate([entry(0, "A", "Something New", "Acme")], repo)
        self.assertEqual(report.products_before, 3)
        self.assertEqual(report.products_after, 4)

    def test_verdicts_are_the_expected_enum(self):
        repo = FakeRepository(CATALOG)
        report = consolidate(
            [
                entry(0, "A", "Smartphone Galaxy S23", "Samsung"),
                entry(1, "B", "Totally New", "Acme"),
            ],
            repo,
        )
        self.assertEqual(
            [o.verdict for o in report.outcomes],
            [Verdict.MATCHED, Verdict.INSERTED],
        )

    def test_an_empty_run_is_valid(self):
        repo = FakeRepository(CATALOG)
        report = consolidate([], repo)
        self.assertEqual(report.records_read, 0)
        self.assertEqual(report.links_created, 0)
        self.assertEqual(report.exit_code(), 0)


if __name__ == "__main__":
    unittest.main()
