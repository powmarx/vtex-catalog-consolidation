"""Value objects passed between the modules.

All frozen. Nothing here knows about SQL, files, or the command line, so every
other module can be tested against these rather than against a database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# A product's identity for matching purposes: normalized (name, brand).
# See D1 in docs/DECISIONS.md. Deliberately excludes Category, which sellers
# disagree on for the same product.
MatchKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class SellerEntry:
    """One product submission from one seller, as it appears in the input file.

    `entry_id` is the seller's own identifier. It is opaque: only ever stored and
    compared for equality, never parsed. It is not unique across sellers (D4), so
    it does not identify a listing on its own.
    """

    entry_id: str
    seller_name: str
    name: str
    brand: str | None
    category: str | None
    source_index: int
    """Position in the input file. Carried so findings can point at a record."""

    @property
    def listing_key(self) -> tuple[str, str]:
        """What actually identifies a seller's listing. See D4."""
        return self.seller_name, self.entry_id


@dataclass(frozen=True, slots=True)
class Product:
    """A row of the catalog's Product table."""

    product_id: int
    name: str
    brand: str | None
    category: str | None


class Verdict(str, Enum):
    """What the consolidator decided about one input record."""

    MATCHED = "matched"
    """Resolved to a product already in the catalog; a link was written."""

    INSERTED = "inserted"
    """No match found; a new product was created and linked."""

    SUPPRESSED = "suppressed"
    """A duplicate listing. Not written, and which constraint caught it is recorded."""

    REJECTED = "rejected"
    """Could not be processed. See RecordError."""


@dataclass(frozen=True, slots=True)
class RecordError:
    """A record that could not be processed, and why. See D7.

    Collected rather than raised, so one bad record cannot cost the rest of the file.
    """

    source_index: int
    reason: str
    seller_name: str | None = None
    entry_id: str | None = None


@dataclass(frozen=True, slots=True)
class RecordOutcome:
    """The per-record audit trail behind the aggregate counts. Feeds D11's report."""

    source_index: int
    seller_name: str
    entry_id: str
    verdict: Verdict
    product_id: int | None = None
    detail: str = ""
    """Why, in words. For SUPPRESSED, names the constraint that caught it."""


@dataclass(frozen=True, slots=True)
class DiscardedValue:
    """A field value D2 declined to write onto an existing catalog row.

    Recorded so the loss is auditable instead of invisible. See N3 in DATA-ISSUES.md.
    """

    source_index: int
    product_id: int
    field_name: str
    incoming: str | None
    retained: str | None


@dataclass(frozen=True, slots=True)
class ReviewCandidate:
    """A product inserted as new, for which a plausible existing match was found.

    Produced by the D11 candidate rule. Advisory only: nothing in the ingest acts
    on these, because a wrong automatic merge is unrecoverable.
    """

    source_index: int
    inserted_name: str
    inserted_brand: str | None
    candidate_product_id: int
    candidate_name: str
    overlap: float


@dataclass
class Report:
    """The result of a run. Returned by the consolidator, rendered by the caller.

    The consolidator prints nothing (DS6), which is what lets the acceptance test
    assert on these numbers directly rather than parsing output.
    """

    records_read: int = 0
    matched: int = 0
    inserted: int = 0
    links_created: int = 0
    suppressed: int = 0
    errors: list[RecordError] = field(default_factory=list)
    outcomes: list[RecordOutcome] = field(default_factory=list)
    discarded: list[DiscardedValue] = field(default_factory=list)
    candidates: list[ReviewCandidate] = field(default_factory=list)
    products_before: int = 0
    products_after: int = 0
    dry_run: bool = False

    @property
    def failed(self) -> int:
        return len(self.errors)

    @property
    def sellers_linked(self) -> int:
        return len({o.seller_name for o in self.outcomes if o.verdict is not Verdict.REJECTED})

    def exit_code(self) -> int:
        """0 clean, 1 if any record failed. Suppressing duplicates is success, not failure."""
        return 1 if self.errors else 0
