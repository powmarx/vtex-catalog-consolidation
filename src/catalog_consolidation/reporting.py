"""Rendering a `Report`. See D11 and DS9 in the docs.

JSON is the source of truth and Markdown is generated from it, so the two cannot
disagree. The console summary is a third view of the same object.

Nothing here decides anything; it only presents what the consolidator recorded. That
separation is what lets the acceptance tests assert on numbers instead of parsing text.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Report

__all__ = ["to_dict", "to_json", "to_markdown", "to_text", "write_report", "report_filename"]


def to_dict(report: Report, *, source: str | None = None, database: str | None = None) -> dict[str, Any]:
    """The canonical machine-readable form. Everything else is derived from this."""
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input": source,
        "database": database,
        "dry_run": report.dry_run,
        "summary": {
            "records_read": report.records_read,
            "matched": report.matched,
            "inserted": report.inserted,
            "links_created": report.links_created,
            "suppressed": report.suppressed,
            "failed": report.failed,
            "sellers_linked": report.sellers_linked,
            "products_before": report.products_before,
            "products_after": report.products_after,
        },
        "rejections": [
            {
                "source_index": e.source_index,
                "seller_name": e.seller_name,
                "entry_id": e.entry_id,
                "reason": e.reason,
            }
            for e in report.errors
        ],
        "suppressions": [
            {
                "source_index": o.source_index,
                "seller_name": o.seller_name,
                "entry_id": o.entry_id,
                "product_id": o.product_id,
                "reason": o.detail,
            }
            for o in report.outcomes
            if o.verdict.value == "suppressed"
        ],
        "review_candidates": [
            {
                "source_index": c.source_index,
                "inserted_name": c.inserted_name,
                "inserted_brand": c.inserted_brand,
                "candidate_product_id": c.candidate_product_id,
                "candidate_name": c.candidate_name,
                "overlap": c.overlap,
            }
            for c in report.candidates
        ],
        "discarded_values": [
            {
                "source_index": d.source_index,
                "product_id": d.product_id,
                "field": d.field_name,
                "incoming": d.incoming,
                "retained": d.retained,
            }
            for d in report.discarded
        ],
        "outcomes": [
            {
                "source_index": o.source_index,
                "seller_name": o.seller_name,
                "entry_id": o.entry_id,
                "verdict": o.verdict.value,
                "product_id": o.product_id,
                "detail": o.detail,
            }
            for o in report.outcomes
        ],
    }


def to_json(report: Report, *, source: str | None = None, database: str | None = None) -> str:
    return json.dumps(to_dict(report, source=source, database=database), indent=2, ensure_ascii=False)


def to_text(report: Report) -> str:
    """The terse console summary."""
    lines = [
        f"records read               {report.records_read:>6}",
        f"matched existing product   {report.matched:>6}",
        f"products inserted          {report.inserted:>6}",
        f"links created              {report.links_created:>6}",
        f"duplicate listings skipped {report.suppressed:>6}",
        f"records failed             {report.failed:>6}",
        f"sellers linked             {report.sellers_linked:>6}",
        f"catalog products           {report.products_before:>6} -> {report.products_after}",
    ]
    if report.dry_run:
        lines.append("")
        lines.append("DRY RUN - the transaction was rolled back; nothing was written.")
    if report.candidates:
        lines.append("")
        lines.append(f"{len(report.candidates)} review candidate(s): inserted as new, but a close match exists")
        for candidate in report.candidates:
            lines.append(
                f"  record {candidate.source_index}: {candidate.inserted_name!r} "
                f"~ product {candidate.candidate_product_id} {candidate.candidate_name!r} "
                f"(overlap {candidate.overlap:.3f})"
            )
    if report.errors:
        lines.append("")
        lines.append(f"{len(report.errors)} record(s) rejected")
        for error in report.errors[:10]:
            lines.append(f"  record {error.source_index}: {error.reason}")
        if len(report.errors) > 10:
            lines.append(f"  ... and {len(report.errors) - 10} more")
    return "\n".join(lines)


def to_markdown(payload: dict[str, Any]) -> str:
    """Human-readable findings, derived from the JSON rather than from the Report.

    Taking the dict as input is deliberate: it guarantees the Markdown can only describe
    what the JSON contains.
    """
    summary = payload["summary"]
    mode = " (dry run, nothing written)" if payload.get("dry_run") else ""
    lines = [
        f"# Consolidation findings{mode}",
        "",
        f"- Generated: {payload['generated_at']}",
        f"- Input: `{payload.get('input') or 'unknown'}`",
        f"- Database: `{payload.get('database') or 'unknown'}`",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Records read | {summary['records_read']} |",
        f"| Matched an existing product | {summary['matched']} |",
        f"| New products inserted | {summary['inserted']} |",
        f"| Links created | {summary['links_created']} |",
        f"| Duplicate listings suppressed | {summary['suppressed']} |",
        f"| Records rejected | {summary['failed']} |",
        f"| Distinct sellers linked | {summary['sellers_linked']} |",
        f"| Catalog products | {summary['products_before']} -> {summary['products_after']} |",
    ]

    candidates = payload["review_candidates"]
    lines += ["", "## Review candidates", ""]
    if candidates:
        lines += [
            "Inserted as new products, but a catalog product shares the brand and most of the "
            "name. Matching is deliberately conservative, so these are surfaced rather than "
            "merged automatically -- a wrong merge is unrecoverable.",
            "",
            "| Record | Inserted | Existing candidate | Overlap |",
            "| --- | --- | --- | --- |",
        ]
        for c in candidates:
            lines.append(
                f"| {c['source_index']} | `{c['inserted_name']}` | "
                f"{c['candidate_product_id']} `{c['candidate_name']}` | {c['overlap']:.3f} |"
            )
    else:
        lines.append("None.")

    suppressions = payload["suppressions"]
    lines += ["", "## Suppressed duplicate listings", ""]
    if suppressions:
        grouped: dict[str, int] = {}
        for s in suppressions:
            grouped[s["reason"]] = grouped.get(s["reason"], 0) + 1
        lines += ["| Reason | Records |", "| --- | --- |"]
        for reason, count in sorted(grouped.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {reason} | {count} |")
        lines += ["", "<details><summary>Every suppressed record</summary>", ""]
        lines += ["| Record | Seller | Listing id | Product |", "| --- | --- | --- | --- |"]
        for s in suppressions:
            lines.append(
                f"| {s['source_index']} | {s['seller_name']} | `{s['entry_id']}` | {s['product_id']} |"
            )
        lines += ["", "</details>"]
    else:
        lines.append("None.")

    rejections = payload["rejections"]
    lines += ["", "## Rejected records", ""]
    if rejections:
        lines += ["| Record | Seller | Reason |", "| --- | --- | --- |"]
        for r in rejections:
            lines.append(f"| {r['source_index']} | {r['seller_name'] or '-'} | {r['reason']} |")
    else:
        lines.append("None.")

    discarded = payload["discarded_values"]
    lines += ["", "## Values not written", ""]
    if discarded:
        by_field: dict[str, int] = {}
        for d in discarded:
            by_field[d["field"]] = by_field.get(d["field"], 0) + 1
        lines += [
            "Existing catalog rows are never updated, so where an incoming record spelled a "
            "field differently the catalog's value was kept. Recorded so the loss is auditable.",
            "",
            "| Field | Records |",
            "| --- | --- |",
        ]
        for field_name, count in sorted(by_field.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {field_name} | {count} |")
        interesting = [d for d in discarded if d["field"] != "Name"]
        if interesting:
            lines += [
                "",
                "Differences outside `Name`, which are the arguable ones:",
                "",
                "| Record | Product | Field | Incoming | Retained |",
                "| --- | --- | --- | --- | --- |",
            ]
            for d in interesting:
                lines.append(
                    f"| {d['source_index']} | {d['product_id']} | {d['field']} | "
                    f"`{d['incoming']}` | `{d['retained']}` |"
                )
    else:
        lines.append("None.")

    return "\n".join(lines) + "\n"


def report_filename(source: str | Path, *, when: datetime | None = None) -> str:
    """`<utc-timestamp>-<input-stem>`, so two runs of the same file stay comparable."""
    moment = when or datetime.now(timezone.utc)
    stem = Path(source).stem or "input"
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in stem)
    return f"{moment.strftime('%Y%m%dT%H%M%SZ')}-{safe}"


def write_report(
    report: Report,
    directory: str | Path,
    *,
    source: str | None = None,
    database: str | None = None,
    when: datetime | None = None,
) -> tuple[Path, Path]:
    """Write the JSON and its Markdown rendering. Returns both paths.

    Called only after the transaction has committed (DS9): a report written mid-run
    could describe rows that never existed.
    """
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    base = report_filename(source or "input", when=when)
    payload = to_dict(report, source=source, database=database)

    json_path = target / f"{base}.json"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    markdown_path = target / f"{base}.md"
    markdown_path.write_text(to_markdown(payload), encoding="utf-8")

    return json_path, markdown_path
