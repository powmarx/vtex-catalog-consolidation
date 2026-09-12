"""The match key. See D1 in docs/DECISIONS.md.

The only imports are the standard library's `unicodedata` and the `MatchKey` type alias
from `models.py` -- no I/O, no database, nothing that needs setting up. That is what
makes the matching rule testable in isolation. It is the highest-risk logic in the
project: the entire consolidation turns on whether two differently-spelled names are
judged to be the same product.

The rule, and why each step is there:

1. NFKD decomposition, then drop combining marks.
   `Câmera Canon EOS R6` and `Camera Canon EOS R6` are the same product.

2. Lowercase.
   The catalog itself is inconsistent: `simplehuman` and `Simplehuman` both appear,
   as do `Black+Decker` and `BLACK+DECKER`.

3. Drop every character that is not alphanumeric or whitespace -- WITHOUT
   substituting a space.
   This is the subtle one. The input has `Levi's` where the catalog has `Levis`.
   Replacing punctuation with a space yields `levi s`, which does not match `levis`.
   Removing it outright yields `levis`, which does. The same step collapses
   `12.9"`, `12.9''` and `12.9` onto one key.

4. Collapse runs of whitespace.
   60 of the 269 input records carry a doubled internal space.

`None` maps to the empty string and is treated as a value, not a wildcard: a record
with no brand matches only a product with no brand. Verified safe on this catalog --
no product name exists both with and without a brand, so the empty key cannot drift
onto the wrong product.

Known limitation: the rule is purely lexical. It cannot see that `Roteador` and
`Router` are the same word in different languages. That is D6, and the findings
report (D11) is how those misses are surfaced rather than hidden.
"""

from __future__ import annotations

import unicodedata

from .models import MatchKey

__all__ = ["normalize", "match_key", "tokens", "token_overlap"]


def normalize(value: str | None) -> str:
    """Reduce a name or brand to its comparison form.

    >>> normalize("Smartphone  Galaxy S23")
    'smartphone galaxy s23'
    >>> normalize("Câmera Canon EOS R6")
    'camera canon eos r6'
    >>> normalize("Levi's") == normalize("Levis")
    True
    >>> normalize('Monitor LG UltraWide 34"') == normalize("Monitor LG UltraWide 34")
    True
    >>> normalize(None)
    ''
    """
    if value is None:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(value))
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = without_accents.lower()
    # Note: dropped, not replaced with a space. See step 3 above.
    kept = "".join(ch for ch in lowered if ch.isalnum() or ch.isspace())
    return " ".join(kept.split())


def match_key(name: str | None, brand: str | None) -> MatchKey:
    """The key two products are considered the same by.

    Brand is included even though it is redundant on the supplied data -- matching on
    name alone finds the same 266 records, and no catalog name is shared by two
    brands. It is kept because two products sharing a name under different brands is
    an ordinary catalog scenario, and dropping the field would be fitting the key to
    one particular file.
    """
    return normalize(name), normalize(brand)


def tokens(value: str | None) -> frozenset[str]:
    """Normalized whitespace-separated tokens, for the D11 candidate rule."""
    return frozenset(normalize(value).split())


def token_overlap(left: str | None, right: str | None) -> float:
    """Jaccard similarity of two names' token sets, in [0.0, 1.0].

    Used only to propose review candidates (D11), never to decide a match. The
    threshold is a strict `> 0.5`: pairs such as `Hockey Stick Ice` against
    `Hockey Skates Ice` score exactly 0.5 and must not be proposed.

    >>> round(token_overlap("Roteador WiFi 6 TP-Link", "Router WiFi 6 TP-Link"), 3)
    0.6
    >>> token_overlap("Hockey Stick Ice", "Hockey Skates Ice")
    0.5
    >>> token_overlap("", "")
    0.0
    """
    a, b = tokens(left), tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
