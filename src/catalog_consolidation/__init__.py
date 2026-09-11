"""Catalog consolidation for a marketplace.

Ingests a file of product submissions from many sellers into an existing catalog
without duplicating products, recording which sellers offer each product.

The reasoning behind every behaviour is in docs/DECISIONS.md, referenced throughout
the code as D1-D11; the structure is in docs/DESIGN.md as DS1-DS9.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
