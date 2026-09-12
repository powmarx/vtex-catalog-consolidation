#!/usr/bin/env python3
"""Prove that verify_docs.py can actually fail.

A verifier that passes unconditionally is worse than no verifier, because it
manufactures confidence. This applies a set of deliberate mutations to a throwaway
copy of the repository and asserts that each one is caught.

It found a real gap on first run: a check asserted only that the preferred SQL
conflict clause appeared *somewhere* in DESIGN.md, which survived the DS3 heading
being inverted to recommend the opposite clause. That check is now pinned to the
prescription itself.

Usage:
    python scripts/verify_docs_selftest.py
    python scripts/verify_docs_selftest.py -v

Exit code 0 when every mutation is caught and the unmutated control passes.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def patch(root: Path, rel: str, old: str, new: str, *, count: int = 1) -> None:
    """Replace `old` with `new` in a file.

    `count=-1` replaces every occurrence. That matters: three mutations initially went
    undetected because they changed only the first of two copies, and the surviving copy
    kept the corresponding check green. The checks were then tightened as well, but a
    mutation that leaves the defect half-applied is not testing what it claims to.
    """
    path = root / rel
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise AssertionError(f"anchor not found in {rel}: {old[:60]!r}")
    path.write_text(text.replace(old, new) if count < 0 else text.replace(old, new, count), encoding="utf-8", newline="")


def mutate_catalog(root: Path) -> None:
    db = root / "data" / "catalog.db"
    db.chmod(0o666)
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO Product (Name) VALUES ('injected')")
    conn.commit()
    conn.close()


def mutate_entries(root: Path) -> None:
    path = root / "data" / "ProductEntry.json"
    path.chmod(0o666)
    path.write_text(path.read_text(encoding="utf-8").replace("MegaStore", "MegaStore2", 1), encoding="utf-8", newline="")


# Each mutation is a plausible way the documentation or the artifacts could rot.
MUTATIONS: list[tuple[str, object]] = [
    ("invert the DS3 prescription", lambda r: patch(r, "docs/DESIGN.md", "uses `ON CONFLICT DO NOTHING`, not `OR IGNORE`", "uses `OR IGNORE`, not `ON CONFLICT DO NOTHING`")),
    ("soften DS3's rationale", lambda r: patch(r, "docs/DESIGN.md", "`INSERT OR IGNORE` would be a bug here", "`INSERT OR IGNORE` is fine here")),
    ("let TASKS drift back to OR IGNORE", lambda r: patch(r, "docs/TASKS.md", "**Not `INSERT OR IGNORE`**", "or `INSERT OR IGNORE`")),
    ("undo D3's admission of its earlier error", lambda r: patch(r, "docs/DECISIONS.md", "That was wrong", "That was fine")),
    ("bury D3's 'changes nothing here' admission", lambda r: patch(r, "docs/DECISIONS.md", "migration is therefore defensive", "migration is therefore essential")),
    ("overstate how many records D3 affects", lambda r: patch(r, "docs/DECISIONS.md", "would alter **0 of 269**", "would alter **31 of 269**")),
    ("make D3's coercion table sound like a live failure", lambda r: patch(r, "docs/DECISIONS.md", "| Seller sends | `INTEGER` column would store |", "| Seller sends | `INTEGER` column stores |")),
    ("remove DS8's pragma warning", lambda r: patch(r, "docs/DESIGN.md", "silently ignored inside a transaction", "always applied")),
    ("change an acceptance number", lambda r: patch(r, "docs/DECISIONS.md", "| `SellerProduct` rows after ingest | 257 |", "| `SellerProduct` rows after ingest | 258 |")),
    ("break the DATA-ISSUES table arithmetic", lambda r: patch(r, "docs/DATA-ISSUES.md", "| `ProductEntry.json` | 3 | 4 | 3 | 10 |", "| `ProductEntry.json` | 4 | 4 | 3 | 11 |")),
    ("delete an issue heading", lambda r: patch(r, "docs/DATA-ISSUES.md", "### H6 — Null brands", "### HX — Null brands")),
    ("delete a decision heading", lambda r: patch(r, "docs/DECISIONS.md", "### D9 — No `Seller` table", "### DX — No `Seller` table")),
    ("delete a design heading", lambda r: patch(r, "docs/DESIGN.md", "### DS8 —", "### DSX —")),
    ("weaken D11's strict threshold to >=", lambda r: patch(r, "docs/DECISIONS.md", "strictly greater than 0.5", "at least 0.5")),
    ("drop D11's candidate score evidence", lambda r: patch(r, "docs/DECISIONS.md", "| 0.667 | yes |", "| high | yes |")),
    ("remove DS9's threading rationale", lambda r: patch(r, "docs/DESIGN.md", "the GIL prevents real parallelism", "it runs in parallel")),
    ("un-gitignore the reports directory", lambda r: patch(r, ".gitignore", "reports/", "report-output/")),
    ("invent a counter-example not in the catalog", lambda r: patch(r, "docs/MERGE-ANALYSIS.md", "Hockey Stick Ice", "Cricket Bat Willow", count=-1)),
    ("get the quantisation arithmetic wrong", lambda r: patch(r, "docs/MERGE-ANALYSIS.md", "| score | 0.333 | **0.500** | 0.600 |", "| score | 0.333 | **0.500** | 0.650 |")),
    ("drop D6's link to the analysis", lambda r: patch(r, "docs/DECISIONS.md", "MERGE-ANALYSIS.md", "MISSING.md", count=-1)),
    ("leave a decision defined but unreferenced", lambda r: patch(r, "docs/DESIGN.md", "`D11`", "`D99`")),
    ("misstate the post-ingest Brand delta", lambda r: patch(r, "docs/SCHEMA.md", "| Distinct `Brand` values | 639 | 640 |", "| Distinct `Brand` values | 639 | 645 |")),
    ("claim existing rows are updated", lambda r: patch(r, "docs/SCHEMA.md", "Nothing in the existing 975 rows changes", "Existing rows are refreshed from the input")),
    ("state the wrong DDL in SCHEMA", lambda r: patch(r, "docs/SCHEMA.md", "SellerProductId INTEGER NOT NULL", "SellerProductId TEXT NOT NULL")),
    ("drop a document from the reading order", lambda r: patch(r, "README.md", "[`docs/DATA-ISSUES.md`](docs/DATA-ISSUES.md)", "`docs/DATA-ISSUES.md`")),
    ("point the reading order at a missing document", lambda r: patch(r, "README.md", "](docs/SCHEMA.md)", "](docs/SCHEMAS.md)")),
    ("remove the reading order entirely", lambda r: patch(r, "README.md", "## Where to start", "## Documents")),
    ("misstate the English/Portuguese split", lambda r: patch(r, "docs/DECISIONS.md", "Of 269 input records, 266 are English-named", "Of 269 input records, 240 are English-named")),
    ("drop D6's production-inversion point", lambda r: patch(r, "docs/DECISIONS.md", "inverted relative to production", "similar to production")),
    ("reintroduce a claim about the reader's business", lambda r: patch(r, "docs/DECISIONS.md", "That ratio is an artifact of the exercise", "VTEX operates in Brazil, so that ratio is an artifact of the exercise")),
    ("point a translation pair at the wrong catalog row", lambda r: patch(r, "docs/DECISIONS.md", "`Roteador`/`Router` (#21)", "`Roteador`/`Router` (#210)")),
    ("modify catalog.db", mutate_catalog),
    ("modify ProductEntry.json", mutate_entries),
    ("leave trailing whitespace in a doc", lambda r: patch(r, "docs/SCHEMA.md", "# Schema reference", "# Schema reference  ")),
    ("unbalance a code fence", lambda r: patch(r, "docs/DESIGN.md", "```mermaid", "``mermaid")),
]


def run_verifier(root: Path) -> tuple[int, list[str]]:
    proc = subprocess.run(
        [sys.executable, str(root / "scripts" / "verify_docs.py")],
        capture_output=True,
        text=True,
    )
    failures = [line.strip() for line in proc.stdout.splitlines() if line.strip().startswith("[")]
    return proc.returncode, failures


def with_copy(fn):
    tmp = Path(tempfile.mkdtemp())
    root = tmp / "repo"
    shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.local.db"))
    try:
        return fn(root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prove verify_docs.py can fail.")
    parser.add_argument("-v", "--verbose", action="store_true", help="show the failure each mutation triggers")
    args = parser.parse_args(argv)

    print("control: unmutated copy must pass")
    code, failures = with_copy(run_verifier)
    if code != 0:
        print(f"  FAIL - the control run exited {code}. The verifier disagrees with the committed docs.")
        for f in failures:
            print(f"    {f}")
        return 1
    print("  ok\n")

    print(f"{len(MUTATIONS)} mutations, each must be caught")
    missed: list[str] = []
    skipped: list[str] = []
    for label, mutate in MUTATIONS:
        def attempt(root: Path, mutate=mutate):
            mutate(root)
            return run_verifier(root)

        try:
            code, failures = with_copy(attempt)
        except AssertionError as exc:
            skipped.append(f"{label}: {exc}")
            print(f"  SKIP {label}")
            continue

        if code == 1:
            print(f"  ok   {label}")
            if args.verbose and failures:
                print(f"         -> {failures[0]}")
        else:
            missed.append(label)
            print(f"  MISS {label}  (verifier exited {code})")

    print()
    if skipped:
        print(f"{len(skipped)} mutation(s) could not be applied - the anchor text has changed:")
        for s in skipped:
            print(f"  {s}")
        print("Update the anchor so the mutation is exercised again.\n")
    if missed:
        print(f"{len(missed)} mutation(s) NOT caught by verify_docs.py:")
        for m in missed:
            print(f"  {m}")
        print("\nAdd a check that covers each, then re-run.")
        return 1
    if skipped:
        return 1
    print(f"all {len(MUTATIONS)} mutations caught; the verifier is load-bearing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
