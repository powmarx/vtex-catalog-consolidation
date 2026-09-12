"""Reading seller submissions from a JSON file. See D7 in docs/DECISIONS.md.

Per-record problems are collected, not raised: one malformed record must not cost the
other 268. A record that cannot be turned into a `SellerEntry` is dropped with a
`RecordError` naming its position in the file, and loading continues.

What is deliberately *not* validated:

- `Id` shape. It is an opaque token, only ever stored and compared for equality.
  Three of the 269 supplied ids fail a UUID check -- one has a group of 7 hex digits,
  one has four groups instead of five, one contains a non-hex character. Rejecting
  them would discard data the system has no business rejecting.
- Field contents. The record whose brand is `TestBrand'; SELECT 1; --` is ordinary
  data. Parameterized queries are what make it safe, not input filtering.

What *is* rejected: anything that cannot satisfy the schema's NOT NULL columns, since
the insert would fail anyway and failing here names the offending record instead of
aborting mid-transaction.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from .models import RecordError, SellerEntry

__all__ = ["load_entries", "parse_entries", "SourceFormatError"]

REQUIRED_TEXT_FIELDS = ("Id", "SellerName", "Name")
"""Fields that must be present and non-blank. They map to NOT NULL columns."""

OPTIONAL_TEXT_FIELDS = ("Brand", "Category")
"""Nullable in both the input and the schema."""


class SourceFormatError(Exception):
    """The file as a whole is unusable: unreadable, not JSON, or not a JSON array.

    Distinct from a per-record problem. There is nothing partial to salvage, so this
    is raised rather than collected, and the CLI turns it into exit code 2.
    """


def load_entries(path: str | Path) -> tuple[list[SellerEntry], list[RecordError]]:
    """Read a file of seller submissions.

    Returns the entries that could be parsed and an error per record that could not.
    Raises `SourceFormatError` if the file itself is unusable.
    """
    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceFormatError(f"cannot read {file_path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise SourceFormatError(f"{file_path} is not valid UTF-8: {exc}") from exc

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SourceFormatError(f"{file_path} is not valid JSON: {exc}") from exc

    return parse_entries(payload, origin=str(file_path))


def parse_entries(payload: object, origin: str = "<input>") -> tuple[list[SellerEntry], list[RecordError]]:
    """Turn already-decoded JSON into entries and errors.

    Separated from file reading so the record-level rules can be tested without
    touching the filesystem.
    """
    if not isinstance(payload, list):
        raise SourceFormatError(f"{origin}: expected a JSON array of records, found {type(payload).__name__}")

    entries: list[SellerEntry] = []
    errors: list[RecordError] = []

    for index, raw in enumerate(payload):
        entry, error = _parse_record(raw, index)
        if entry is not None:
            entries.append(entry)
        if error is not None:
            errors.append(error)

    return entries, errors


def _parse_record(raw: object, index: int) -> tuple[SellerEntry | None, RecordError | None]:
    if not isinstance(raw, dict):
        return None, RecordError(index, f"expected an object, found {type(raw).__name__}")

    # Pull the identifying fields first so an error can name the record even when the
    # record is the thing that is broken.
    seller_name = _clean(raw.get("SellerName"))
    entry_id = _clean(raw.get("Id"))

    # Blankness is judged on the stripped form for every field, but only Name, Brand and
    # Category are *stored* stripped. Id keeps its original bytes -- see _verbatim().
    missing = [f for f in REQUIRED_TEXT_FIELDS if not _clean(raw.get(f))]
    if missing:
        return None, RecordError(
            index,
            f"missing or blank required field(s): {', '.join(missing)}",
            seller_name=seller_name,
            entry_id=entry_id,
        )

    wrong_type = [
        f
        for f in REQUIRED_TEXT_FIELDS + OPTIONAL_TEXT_FIELDS
        if raw.get(f) is not None and not isinstance(raw.get(f), str)
    ]
    if wrong_type:
        return None, RecordError(
            index,
            f"field(s) must be text or null: {', '.join(wrong_type)}",
            seller_name=seller_name,
            entry_id=entry_id,
        )

    return (
        SellerEntry(
            # Preserved exactly, never stripped. See _verbatim() for why.
            entry_id=_verbatim(raw.get("Id")),  # type: ignore[arg-type]
            seller_name=seller_name,  # type: ignore[arg-type]
            name=_clean(raw.get("Name")),  # type: ignore[arg-type]
            brand=_clean(raw.get("Brand")),
            category=_clean(raw.get("Category")),
            source_index=index,
        ),
        None,
    )


def _verbatim(value: object) -> str | None:
    """Return an identifier exactly as supplied, or None if it is absent or blank.

    `Id` is the seller's own identifier for their listing, and D7 treats it as an opaque
    token: only ever stored and compared for equality. So it must not be rewritten.

    This was a bug. The loader used to strip surrounding whitespace from every field
    including `Id`, which turned `' 42'` and `'42 '` into the same `'42'` -- two distinct
    listings collapsing into one, with the second silently suppressed as a duplicate. A
    synthetic fixture caught it; the supplied file has no such ids.

    The point is worth stating because it is the same mistake D3 is about. That decision
    widened `SellerProductId` to TEXT so the *database* could not rewrite an identifier
    through integer affinity. Stripping it in the loader rewrote it one layer earlier, so
    the migration was defending a value that had already been altered.

    Blankness is still judged on the stripped form: an id of `'   '` is no identifier at
    all and is reported as missing.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return value  # type: ignore[return-value]  # caller reports the type error
    return value if value.strip() else None


def _clean(value: object) -> str | None:
    """Normalize absent, null and blank to None; strip surrounding whitespace.

    Stripping is safe here and not a `D2` violation: it does not alter any value in
    the supplied file, where no field carries leading or trailing whitespace. It
    exists so a value that is only whitespace is treated as absent rather than
    stored as a blank that then fails a NOT NULL check downstream.

    Internal spacing is left alone -- the 60 doubled internal spaces are stored
    verbatim and handled by the match key, not by rewriting the data.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return value  # type: ignore[return-value]  # caller reports the type error
    stripped = value.strip()
    return stripped or None


def iter_names(entries: Iterable[SellerEntry]) -> Iterable[str]:
    """Convenience for callers that only need the names."""
    return (entry.name for entry in entries)
