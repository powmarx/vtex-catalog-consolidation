"""Assert the counts the documentation states about the test suite itself.

`scripts/verify_docs.py` checks claims about the *data* -- that a documented figure still
matches what the artifacts contain. It cannot check a claim about the suite, because the
suite is what runs it.

So these live here. They exist because three such claims had already gone stale: the
README said 218 tests and 16 mutations, `TASKS.md` said 185 tests, and both cited a
check count from an earlier revision. Every one was a number written by hand and never
re-checked, which is the same failure mode the rest of the verification exists to
prevent -- just one level up.

Self-referential by nature: adding a test changes the count these assert. That is the
point. It fails immediately rather than drifting quietly.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def discovered_test_count() -> int:
    """Count tests without running them."""
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT / "tests"))
    return suite.countTestCases()


class TestDocumentedCounts(unittest.TestCase):
    def test_readme_test_count_is_current(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        match = re.search(r"(\d+) tests, in two kinds", readme)
        self.assertIsNotNone(match, "README no longer states a test count in the expected form")
        self.assertEqual(
            int(match.group(1)),
            discovered_test_count(),
            "README's test count is stale; update it or stop stating a number",
        )

    def test_tasks_test_count_is_current(self):
        tasks = (ROOT / "docs" / "TASKS.md").read_text(encoding="utf-8")
        match = re.search(r"(\d+) tests pass", tasks)
        self.assertIsNotNone(match, "TASKS.md no longer states a test count in the expected form")
        self.assertEqual(int(match.group(1)), discovered_test_count(), "TASKS.md's test count is stale")

    def test_readme_mutation_count_is_current(self):
        from verify_docs_selftest import MUTATIONS

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        match = re.search(r"applies (\d+) mutations", readme)
        self.assertIsNotNone(match, "README no longer states a mutation count")
        self.assertEqual(int(match.group(1)), len(MUTATIONS), "README's mutation count is stale")

    def test_no_document_pins_the_verifier_check_count(self):
        """A prose number for it cannot be asserted without circularity.

        `verify_docs.py` would have to count its own checks, and adding that check changes
        the total. Better to describe it than to pin a number nothing can verify.
        """
        for path in [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]:
            with self.subTest(path=path.name):
                self.assertIsNone(
                    re.search(r"\b\d{3} checks\b", path.read_text(encoding="utf-8")),
                    f"{path.name} pins a check count that nothing verifies",
                )


class TestDocumentedCommandsExist(unittest.TestCase):
    """Every command the README tells a reader to run must be runnable."""

    def setUp(self):
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_referenced_scripts_exist(self):
        for name in re.findall(r"python (scripts/[\w_]+\.py)", self.readme):
            with self.subTest(script=name):
                self.assertTrue((ROOT / name).exists(), f"README references a missing script: {name}")

    def test_referenced_module_is_importable(self):
        self.assertIn("python -m catalog_consolidation", self.readme)
        self.assertTrue((ROOT / "src" / "catalog_consolidation" / "__main__.py").exists())

    def test_referenced_data_files_exist(self):
        for name in re.findall(r"(data/[\w.]+)", self.readme):
            if ".local." in name:
                continue  # a working copy the reader creates
            with self.subTest(path=name):
                self.assertTrue((ROOT / name).exists(), f"README references a missing file: {name}")

    def test_every_documented_scenario_is_implemented(self):
        from generate_fixture import SCENARIOS

        # The options table below uses the same row shape, so require the first cell to
        # be a bare name rather than a `--flag`.
        documented = set(re.findall(r"^\| `([a-z][a-z-]*)` \|", self.readme, re.M))
        self.assertTrue(documented, "no scenario table found in the README")
        self.assertEqual(
            documented - set(SCENARIOS),
            set(),
            "the README documents a scenario the generator does not implement",
        )
        self.assertEqual(
            set(SCENARIOS) - documented,
            set(),
            "the generator implements a scenario the README does not document",
        )


if __name__ == "__main__":
    unittest.main()
