"""Run the docstring examples as tests.

`python -m doctest src/catalog_consolidation/normalize.py` cannot resolve the
package-relative imports, so the examples are loaded through `doctest.DocTestSuite`
instead. This keeps the examples in the module docstrings honest.
"""

from __future__ import annotations

import doctest
import unittest

from catalog_consolidation import normalize

MODULES = [normalize]


def load_tests(loader, tests, ignore):  # noqa: ARG001 - unittest protocol
    for module in MODULES:
        tests.addTests(doctest.DocTestSuite(module, optionflags=doctest.NORMALIZE_WHITESPACE))
    return tests


if __name__ == "__main__":
    unittest.main()
