"""Command line entry point. See DS4, DS5 and DS9 in the docs.

    python -m catalog_consolidation --database PATH --input PATH [--dry-run]
                                    [--report {text,json}] [--reports-dir DIR]

`--database` has no default, deliberately (DS4). The repository ships
`data/catalog.db` as a pristine baseline, and a default would make it possible to
mutate that by forgetting an argument. Copy it first:

    copy data\\catalog.db data\\catalog.local.db
    python -m catalog_consolidation --database data/catalog.local.db --input data/ProductEntry.json

Exit codes:
    0  the run succeeded. Suppressing duplicate listings is success, not failure.
    1  the run completed but at least one record could not be processed.
    2  the run could not start or could not finish: bad arguments, unreadable input,
       a database problem, or an error that rolled the transaction back.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import migration, reporting
from .consolidator import consolidate
from .models import Report
from .repository import RepositoryError, SqliteCatalogRepository, connect
from .source import SourceFormatError, load_entries

__all__ = ["main", "build_parser", "run"]

EXIT_OK = 0
EXIT_RECORD_FAILURES = 1
EXIT_USAGE = 2

DEFAULT_REPORTS_DIR = "reports"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="catalog-consolidation",
        description=(
            "Consolidate product submissions from multiple sellers into an existing "
            "catalog without duplicating products."
        ),
        epilog=(
            "--database is required on purpose: data/catalog.db is a pristine baseline "
            "and no invocation should be able to mutate it by accident."
        ),
    )
    parser.add_argument("--database", required=True, metavar="PATH", help="SQLite catalog to consolidate into")
    parser.add_argument("--input", required=True, metavar="PATH", help="JSON file of seller submissions")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run everything inside a transaction and roll it back; the database is left untouched",
    )
    parser.add_argument(
        "--report",
        choices=("text", "json"),
        default="text",
        help="how to print the summary (default: text)",
    )
    parser.add_argument(
        "--reports-dir",
        metavar="DIR",
        default=DEFAULT_REPORTS_DIR,
        help=f"where to write the findings files (default: {DEFAULT_REPORTS_DIR})",
    )
    parser.add_argument(
        "--no-report-file",
        action="store_true",
        help="print the summary but do not write findings files",
    )
    parser.add_argument(
        "--no-candidates",
        action="store_true",
        help="skip the review-candidate search",
    )
    return parser


def run(
    database: str | Path,
    source: str | Path,
    *,
    dry_run: bool = False,
    collect_candidates: bool = True,
) -> Report:
    """Load, migrate and consolidate as one transaction. Returns the report.

    The migration shares the transaction with the ingest, which is what makes a dry run
    honest: committing the schema change while rolling back the inserts would mutate the
    file during an operation named dry run (DS5). SQLite makes DDL and `user_version`
    transactional, so the whole thing rolls back together.
    """
    entries, errors = load_entries(source)
    connection = connect(database)
    try:
        repository = SqliteCatalogRepository(connection)
        with repository.transaction(commit=not dry_run):
            migration.apply(connection)
            report = consolidate(entries, repository, errors=errors, collect_candidates=collect_candidates)
        report.dry_run = dry_run
        return report
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        report = run(
            args.database,
            args.input,
            dry_run=args.dry_run,
            collect_candidates=not args.no_candidates,
        )
    except SourceFormatError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except RepositoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except migration.MigrationError as exc:
        print(f"error: migration failed, database unchanged: {exc}", file=sys.stderr)
        return EXIT_USAGE
    # No `except sqlite3.Error` here on purpose: the repository translates SQLite
    # failures into RepositoryError, so this module never imports sqlite3 and the
    # storage boundary in DESIGN.md holds. A test asserts that.

    if args.report == "json":
        print(reporting.to_json(report, source=str(args.input), database=str(args.database)))
    else:
        print(reporting.to_text(report))

    # Written after the transaction has closed, never during it (DS9).
    if not args.no_report_file:
        try:
            json_path, markdown_path = reporting.write_report(
                report,
                args.reports_dir,
                source=str(args.input),
                database=str(args.database),
            )
        except OSError as exc:
            # The consolidation already succeeded; failing to file the report must not
            # turn a good run into a failed one, but it must be visible.
            print(f"warning: could not write findings files: {exc}", file=sys.stderr)
        else:
            if args.report != "json":
                print()
                print(f"findings: {json_path}")
                print(f"          {markdown_path}")

    return report.exit_code()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
