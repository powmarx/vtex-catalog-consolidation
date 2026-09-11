"""Tests for input loading (D7)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from catalog_consolidation.models import SellerEntry
from catalog_consolidation.source import SourceFormatError, load_entries, parse_entries

ROOT = Path(__file__).resolve().parent.parent
ENTRIES = ROOT / "data" / "ProductEntry.json"


def record(**overrides) -> dict:
    base = {
        "Id": "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d",
        "SellerName": "MegaStore",
        "Name": "Smartphone Galaxy S23",
        "Brand": "Samsung",
        "Category": "Electronics",
    }
    base.update(overrides)
    return base


class TestSuppliedFile(unittest.TestCase):
    """The real input must load completely and cleanly."""

    @classmethod
    def setUpClass(cls):
        cls.entries, cls.errors = load_entries(ENTRIES)

    def test_all_269_records_load_with_no_errors(self):
        self.assertEqual(len(self.entries), 269)
        self.assertEqual(self.errors, [])

    def test_source_index_tracks_file_position(self):
        self.assertEqual([e.source_index for e in self.entries], list(range(269)))

    def test_the_three_malformed_ids_are_accepted(self):
        # D7: Id is opaque. Rejecting these would discard usable data.
        by_index = {e.source_index: e.entry_id for e in self.entries}
        self.assertEqual(by_index[92], "ddddeee-ffff-4000-1111-222233334444")
        self.assertEqual(by_index[180], "09835342345-4678-9abc-def012345678")
        self.assertEqual(by_index[268], "uddd0000-eeee-4111-ffff-aaaa22223333")

    def test_the_injection_payload_loads_as_ordinary_text(self):
        entry = next(e for e in self.entries if e.source_index == 180)
        self.assertEqual(entry.brand, "TestBrand'; SELECT 1; --")
        self.assertEqual(entry.name, "Security Test Product")

    def test_null_brands_stay_none(self):
        nulls = [e for e in self.entries if e.brand is None]
        self.assertEqual(len(nulls), 3)
        self.assertEqual(
            sorted(e.name for e in nulls),
            ["Bed Frame Wood King", "Cable Organizer Kit", "Round Rug 6 Feet"],
        )

    def test_doubled_internal_spaces_are_preserved_verbatim(self):
        # The match key handles these; the loader must not rewrite the data.
        doubled = [e for e in self.entries if "  " in e.name]
        self.assertEqual(len(doubled), 60)

    def test_listing_key_is_seller_plus_id(self):
        entry = self.entries[0]
        self.assertEqual(entry.listing_key, (entry.seller_name, entry.entry_id))

    def test_ids_are_not_unique_but_listing_keys_are_nearly_so(self):
        # D4: 14 ids repeat; only one (seller, id) pair collides.
        self.assertEqual(len({e.entry_id for e in self.entries}), 255)
        self.assertEqual(len({e.listing_key for e in self.entries}), 268)


class TestRecordLevelErrors(unittest.TestCase):
    """Bad records are collected, never raised, and the rest still load."""

    def test_missing_name_is_reported_and_the_record_dropped(self):
        entries, errors = parse_entries([record(), record(Name=None), record()])
        self.assertEqual(len(entries), 2)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].source_index, 1)
        self.assertIn("Name", errors[0].reason)

    def test_blank_and_whitespace_only_values_count_as_missing(self):
        for value in ["", "   ", "\t"]:
            with self.subTest(value=value):
                entries, errors = parse_entries([record(SellerName=value)])
                self.assertEqual(entries, [])
                self.assertEqual(len(errors), 1)
                self.assertIn("SellerName", errors[0].reason)

    def test_missing_id_is_reported(self):
        _, errors = parse_entries([record(Id=None)])
        self.assertIn("Id", errors[0].reason)

    def test_several_missing_fields_are_named_together(self):
        _, errors = parse_entries([{"Brand": "X"}])
        self.assertEqual(len(errors), 1)
        for field in ("Id", "SellerName", "Name"):
            self.assertIn(field, errors[0].reason)

    def test_error_carries_identifying_fields_when_it_can(self):
        _, errors = parse_entries([record(Name=None)])
        self.assertEqual(errors[0].seller_name, "MegaStore")
        self.assertEqual(errors[0].entry_id, "a1b2c3d4-e5f6-4a5b-8c9d-0e1f2a3b4c5d")

    def test_non_object_record_is_reported(self):
        entries, errors = parse_entries([record(), "not an object", 42, None])
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(errors), 3)
        self.assertIn("expected an object", errors[0].reason)

    def test_wrong_field_type_is_reported(self):
        _, errors = parse_entries([record(Brand=123)])
        self.assertEqual(len(errors), 1)
        self.assertIn("must be text or null", errors[0].reason)
        self.assertIn("Brand", errors[0].reason)

    def test_one_bad_record_does_not_cost_the_others(self):
        payload = [record(Name=f"Product {i}") for i in range(10)]
        payload[4] = record(Name=None)
        entries, errors = parse_entries(payload)
        self.assertEqual(len(entries), 9)
        self.assertEqual(len(errors), 1)

    def test_absent_optional_fields_become_none(self):
        entries, errors = parse_entries([{"Id": "x", "SellerName": "S", "Name": "N"}])
        self.assertEqual(errors, [])
        self.assertIsNone(entries[0].brand)
        self.assertIsNone(entries[0].category)

    def test_extra_unknown_fields_are_ignored(self):
        entries, errors = parse_entries([record(Price="9.99", Stock=3)])
        self.assertEqual(errors, [])
        self.assertIsInstance(entries[0], SellerEntry)

    def test_surrounding_whitespace_is_stripped(self):
        entries, _ = parse_entries([record(Name="  Padded Name  ", Brand=" Samsung ")])
        self.assertEqual(entries[0].name, "Padded Name")
        self.assertEqual(entries[0].brand, "Samsung")


class TestFileLevelErrors(unittest.TestCase):
    """A broken file is raised, not collected: there is nothing to salvage."""

    def _write(self, text: str) -> Path:
        tmp = Path(tempfile.mkdtemp()) / "input.json"
        tmp.write_text(text, encoding="utf-8")
        return tmp

    def test_missing_file(self):
        with self.assertRaises(SourceFormatError) as ctx:
            load_entries(Path(tempfile.mkdtemp()) / "absent.json")
        self.assertIn("cannot read", str(ctx.exception))

    def test_invalid_json(self):
        with self.assertRaises(SourceFormatError) as ctx:
            load_entries(self._write("{not json"))
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_json_object_instead_of_array(self):
        with self.assertRaises(SourceFormatError) as ctx:
            load_entries(self._write(json.dumps({"records": []})))
        self.assertIn("expected a JSON array", str(ctx.exception))

    def test_empty_array_is_valid_and_yields_nothing(self):
        entries, errors = load_entries(self._write("[]"))
        self.assertEqual(entries, [])
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
