"""The consolidation algorithm. See D1, D2, D4, D5, DS1 and DS6 in the docs.

This module holds the decisions and knows nothing about SQL. It talks to a
`CatalogRepository`, so the whole algorithm is testable against a fake.

For each incoming record:

1. Compute its match key from the normalized name and brand (D1).
2. If the key is in the index, the product already exists. Write a link and leave the
   `Product` row untouched (D2). Any field the incoming record spells differently is
   recorded as discarded rather than silently dropped (D11).
3. If the key is absent, insert a new product, **add the key to the index**, and link.
   Updating the index is what stops two records for the same genuinely-new product
   inserting it twice (DS1) -- the supplied file happens not to contain that case, so
   it is covered by a constructed test rather than by the fixture.
4. Either way, the link may be suppressed by a unique index. That is not a failure; it
   is the specified behaviour for a duplicate listing (D5). Which constraint caught it
   is inferred and recorded.

The function returns a `Report` and prints nothing (DS6), which is what lets the
acceptance test assert on the numbers directly instead of parsing output.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .models import (
    DiscardedValue,
    MatchKey,
    Product,
    RecordError,
    RecordOutcome,
    Report,
    ReviewCandidate,
    SellerEntry,
    Verdict,
)
from .normalize import match_key, normalize, token_overlap
from .repository import CatalogRepository

__all__ = ["consolidate", "CANDIDATE_OVERLAP_THRESHOLD", "find_review_candidates"]

CANDIDATE_OVERLAP_THRESHOLD = 0.5
"""Jaccard token overlap a review candidate must **exceed**, not merely reach.

Strict for a measured reason (D11). `Hockey Stick Ice` against `Hockey Skates Ice`, and
`Bar Stools Set of 2` against `Nightstand Set of 2`, score exactly 0.5 and are different
products. `>=` would admit them the moment the search scope widened.
"""


def consolidate(
    entries: Sequence[SellerEntry],
    repository: CatalogRepository,
    *,
    errors: Iterable[RecordError] = (),
    collect_candidates: bool = True,
) -> Report:
    """Fold seller submissions into the catalog.

    `errors` carries problems found while loading the input, so the report accounts for
    every record in the file rather than only the ones that parsed.

    Assumes the caller has opened a transaction. Nothing here commits: the run is one
    unit of work owned by the caller (D8).
    """
    report = Report(records_read=len(entries) + len(list(errors)))
    report.errors.extend(errors)
    report.products_before = repository.product_count()

    index = repository.load_index()
    inserted_products: list[tuple[SellerEntry, int]] = []
    # Product ids this run created. A later record resolving to one of these matched a
    # product that did not exist when the run began, which is worth telling apart in the
    # audit trail even though both count as a match.
    created_here: set[int] = set()

    for entry in entries:
        key = match_key(entry.name, entry.brand)
        existing_id = index.get(key)

        if existing_id is not None:
            product_id = existing_id
            verdict = Verdict.MATCHED
            # Only compare against a row that predates the run. A product created a
            # moment ago holds this record's own values, so there is nothing discarded.
            if product_id not in created_here:
                _record_discarded(report, entry, repository.fetch_product(product_id))
        else:
            product_id = repository.insert_product(entry.name, entry.brand, entry.category)
            # DS1: without this the next record for the same new product inserts again.
            index[key] = product_id
            created_here.add(product_id)
            verdict = Verdict.INSERTED
            inserted_products.append((entry, product_id))

        written = repository.link(entry.seller_name, product_id, entry.entry_id)

        if written:
            report.links_created += 1
            if verdict is Verdict.MATCHED:
                report.matched += 1
            else:
                report.inserted += 1
            report.outcomes.append(
                RecordOutcome(
                    source_index=entry.source_index,
                    seller_name=entry.seller_name,
                    entry_id=entry.entry_id,
                    verdict=verdict,
                    product_id=product_id,
                    detail=_match_detail(verdict, product_id, created_here),
                )
            )
        else:
            # The product side already happened and stands; only the link was refused.
            if verdict is Verdict.MATCHED:
                report.matched += 1
            else:
                report.inserted += 1
            report.suppressed += 1
            report.outcomes.append(
                RecordOutcome(
                    source_index=entry.source_index,
                    seller_name=entry.seller_name,
                    entry_id=entry.entry_id,
                    verdict=Verdict.SUPPRESSED,
                    product_id=product_id,
                    detail=_suppression_reason(report, entry, product_id),
                )
            )

    if collect_candidates and inserted_products:
        report.candidates.extend(find_review_candidates(inserted_products, repository))

    report.products_after = repository.product_count()
    return report


def _match_detail(verdict: Verdict, product_id: int, created_here: set[int]) -> str:
    """Wording for the audit trail.

    `inserted` counts new `Product` rows, not records. When two records name the same
    absent product, the first inserts it and the second matches it -- so the pair is
    reported as one insertion and one match, and `inserted` stays equal to
    `products_after - products_before`. The second record is still a match, but against
    a row this run created, and the report says so rather than implying the catalog
    already held it.
    """
    if verdict is Verdict.INSERTED:
        return "new product created"
    if product_id in created_here:
        return "linked to a product created earlier in this run"
    return "linked to existing catalog product"


def _record_discarded(report: Report, entry: SellerEntry, product: Product | None) -> None:
    """Note every field the incoming record spells differently from the catalog.

    `D2` keeps the catalog row as it is. Recording what was dropped is what makes that
    a visible decision instead of silent data loss (see N3 in DATA-ISSUES.md).
    """
    if product is None:  # pragma: no cover - the id came from the index
        return
    for field_name, incoming, retained in (
        ("Name", entry.name, product.name),
        ("Brand", entry.brand, product.brand),
        ("Category", entry.category, product.category),
    ):
        if incoming != retained:
            report.discarded.append(
                DiscardedValue(
                    source_index=entry.source_index,
                    product_id=product.product_id,
                    field_name=field_name,
                    incoming=incoming,
                    retained=retained,
                )
            )


def _suppression_reason(report: Report, entry: SellerEntry, product_id: int) -> str:
    """Which unique index refused the link.

    Distinguishing the two matters: `(SellerName, SellerProductId)` means the identical
    listing arrived twice, while `(SellerName, ProductId)` means the same seller offered
    the same product under a different id. On the supplied file the first accounts for 1
    of the 12 suppressions and the second for 11.
    """
    seen_listing = any(
        o.seller_name == entry.seller_name and o.entry_id == entry.entry_id for o in report.outcomes
    )
    if seen_listing:
        return "duplicate listing: this seller already submitted this SellerProductId"
    seen_offer = any(
        o.seller_name == entry.seller_name and o.product_id == product_id for o in report.outcomes
    )
    if seen_offer:
        return "duplicate offer: this seller is already recorded against this product under another id"
    return "suppressed by a unique constraint"


def find_review_candidates(
    inserted: Sequence[tuple[SellerEntry, int]],
    repository: CatalogRepository,
) -> list[ReviewCandidate]:
    """Propose existing products that an inserted product might duplicate (D11).

    Advisory only. Nothing acts on these, because a wrong automatic merge is
    unrecoverable while a reported near-miss costs only attention.

    Scoped to inserted records, which is a correctness requirement rather than an
    optimization: run over matched records too, the same rule pairs `Hockey Stick Ice`
    with `Hockey Skates Ice`.
    """
    if not hasattr(repository, "iter_products"):  # pragma: no cover - fakes may omit it
        return []

    # Index the catalog by normalized brand once, so each inserted record only compares
    # against products that share its brand.
    by_brand: dict[str, list[Product]] = {}
    for product in repository.iter_products():
        if product.brand:
            by_brand.setdefault(normalize(product.brand), []).append(product)

    candidates: list[ReviewCandidate] = []
    for entry, new_product_id in inserted:
        brand_key = normalize(entry.brand)
        if not brand_key:
            continue
        for product in by_brand.get(brand_key, ()):
            if product.product_id == new_product_id:
                continue
            overlap = token_overlap(entry.name, product.name)
            if overlap > CANDIDATE_OVERLAP_THRESHOLD:
                candidates.append(
                    ReviewCandidate(
                        source_index=entry.source_index,
                        inserted_name=entry.name,
                        inserted_brand=entry.brand,
                        candidate_product_id=product.product_id,
                        candidate_name=product.name,
                        overlap=round(overlap, 4),
                    )
                )
    candidates.sort(key=lambda c: (-c.overlap, c.source_index))
    return candidates


def index_size(repository: CatalogRepository) -> int:
    """How many distinct match keys the catalog holds. Used by diagnostics."""
    return len(repository.load_index())


def resolve(index: dict[MatchKey, int], entry: SellerEntry) -> int | None:
    """The lookup, exposed for tests that want it without a repository."""
    return index.get(match_key(entry.name, entry.brand))
