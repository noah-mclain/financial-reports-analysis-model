"""Build a local PDF provenance review map; consistency is never called accuracy.

Saved snapshots are compared without rerunning extraction or accessing holdout selection.
Use on dev/fit only here. Holdout annotation needs its separate controlled evaluation process.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
from pathlib import Path
from typing import Any, Literal

import pypdfium2 as pdfium
import yaml

from fra_core.schemas import LineItem, Statement, StatementType
from fra_ingest.config import REPO_ROOT, load_config
from fra_ingest.ocr import make_engine
from fra_ingest.pages import sha256_file
from fra_ingest.results import StructureResult
from fra_ingest.structure import structure_pdf
from harness.expected import draft_expected
from harness.independent_gold import FrozenGold, Reading

MANIFEST = REPO_ROOT / "eval/golden/manifest.yaml"
ROW_PREFIXES = {
    StatementType.BALANCE: "bc",
    StatementType.INCOME: "ic",
    StatementType.COMPREHENSIVE_INCOME: "oci",
    StatementType.CASH_FLOW: "cf",
    StatementType.EQUITY: "eq",
}


def row_reference(statement_type: StatementType, row_id: str) -> str:
    """Return a display-only, statement-namespaced extraction row reference."""
    try:
        prefix = ROW_PREFIXES[statement_type]
    except KeyError as error:
        raise ValueError(f"no review row reference prefix for {statement_type.value}") from error
    return f"{prefix}:{row_id}"


def render_row_identity(observation: dict[str, Any]) -> str:
    """Render extraction identity and duplicate-category context for the review table."""
    identity = (
        f"{html.escape(observation['row_ref'])}: {html.escape(str(observation['canonical_id']))}"
    )
    if observation["shared_count"] is None:
        return identity
    peers = html.escape(", ".join(observation["peer_row_refs"]))
    return f"{identity} · shared canonical ID ({observation['shared_count']} rows): {peers}."


def approved_gold(
    reading: Path,
    output: Path,
    *,
    reference_scope: Literal[
        "full_primary_statements", "reviewed_packet_slots"
    ] = "reviewed_packet_slots",
) -> None:
    human = Reading.model_validate_json(reading.read_text(encoding="utf-8"))
    gold = FrozenGold(method="assisted_review", readings=(human,), reference_scope=reference_scope)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(gold.model_dump_json(indent=2) + "\n")


def source_position(row: LineItem) -> tuple[int, str, int]:
    """Where a row sits in the source: the page, table and table row of its first cell. A
    repeated label is told apart by this, which does not move when another run omits a row."""
    where = row.cells[0].provenance
    return where.page_no, where.table_ref, where.row


def draft_positions(
    draft: dict[str, Any], first: StructureResult
) -> dict[tuple[str, str, tuple[int, str, int], str], tuple[int, int]]:
    """For each cell of the draft reading, its statement and row in the draft, keyed like the
    cell map: statement type, label, source position and period."""
    sources: dict[str, Statement] = {}
    for extracted in first.statements:
        sources.setdefault(extracted.type.value, extracted)
    positions = {}
    for si, statement in enumerate(draft["statements"]):
        rows = [i for i in sources[statement["type"]].line_items if i.cells]
        for ri, (row, item) in enumerate(zip(statement["rows"], rows, strict=True)):
            for period in row["values"]:
                key = (statement["type"], row["label"], source_position(item), period)
                positions[key] = (si, ri)
    return positions


def cell_map(results: list[StructureResult]) -> list[dict[str, Any]]:
    """Union of cells across snapshots; missing observations are explicit, not agreement."""
    records: dict[tuple[str, str, tuple[int, str, int], str], dict[str, Any]] = {}
    for run, result in enumerate(results):
        for statement in result.statements:
            row_groups: dict[str, dict[tuple[int, str, int], str]] = {}
            for row in statement.line_items:
                if row.cells and row.canonical_id is not None:
                    row_groups.setdefault(row.canonical_id, {})[source_position(row)] = (
                        row_reference(statement.type, row.id)
                    )
            for row in statement.line_items:
                if not row.cells:
                    continue
                position = source_position(row)
                for cell in row.cells:
                    key = (statement.type.value, row.raw_label, position, cell.period_key)
                    record = records.setdefault(
                        key,
                        {
                            "statement": statement.type.value,
                            "label": row.raw_label,
                            "period": cell.period_key,
                            "position": dict(
                                zip(("page_no", "table_ref", "row"), position, strict=True)
                            ),
                            "runs": [None] * len(results),
                        },
                    )
                    if record["runs"][run] is not None:
                        raise ValueError("duplicate cells for the same row and period")
                    record["runs"][run] = {
                        "value": None if cell.reported is None else str(cell.reported),
                        "raw_text": cell.raw_text,
                        "canonical_id": row.canonical_id,
                        "row_ref": row_reference(statement.type, row.id),
                        "shared_count": (
                            len(row_groups[row.canonical_id])
                            if row.canonical_id is not None
                            and len(row_groups[row.canonical_id]) > 1
                            else None
                        ),
                        "peer_row_refs": (
                            sorted(
                                ref
                                for peer_position, ref in row_groups[row.canonical_id].items()
                                if peer_position != position
                            )
                            if row.canonical_id is not None
                            and len(row_groups[row.canonical_id]) > 1
                            else []
                        ),
                        "source": cell.provenance.model_dump(mode="json"),
                        "flags": cell.flags,
                    }
    for record in records.values():
        observed = record["runs"]
        record["consistent"] = (
            len(results) > 1
            and all(r is not None for r in observed)
            and len({(r["value"], r["canonical_id"]) for r in observed if r is not None}) == 1
        )
    return list(records.values())


def build_review(document_id: str, snapshots: list[Path], output: Path) -> Path:
    entries = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["documents"]
    entry = next((e for e in entries if e["id"] == document_id), None)
    if entry is None:
        raise ValueError("review-map currently accepts dev golden documents only")
    pdf_path = MANIFEST.parent / entry["file"]
    digest = sha256_file(pdf_path)
    if not snapshots:
        raise ValueError("at least one saved snapshot is required")
    results = [
        StructureResult.model_validate_json(p.read_text(encoding="utf-8"), extra="ignore")
        for p in snapshots
    ]
    if any(r.sha256 != digest for r in results):
        raise ValueError("every snapshot must match the source PDF hash")
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(pdf_path, output / "source.pdf")
    # This viewer consumes the statement/cell projection, not newer ancillary schema fields.
    # Preserve the exact full inputs beside the map, including fields this checkout cannot render.
    for number, snapshot in enumerate(snapshots, start=1):
        shutil.copyfile(snapshot, output / f"snapshot-{number}.json")
    mapping = cell_map(results)
    evidence = {
        "document": document_id,
        "sha256": digest,
        "comparison": "Saved snapshots; agreement does not establish independent accuracy.",
        "snapshots": [
            {
                "sha256": sha256_file(p),
                "structure_version": r.version,
                "settings_hash": r.settings_hash,
            }
            for p, r in zip(snapshots, results, strict=True)
        ],
        "cells": mapping,
    }
    (output / "map.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    draft = draft_expected(document_id, results[0]).model_dump(mode="json")
    draft = {k: draft[k] for k in ("id", "sha256", "statements")}
    draft.update(reader="", source="pdf_reviewed", complete=False)
    first_statements: dict[str, Statement] = {}
    for extracted in results[0].statements:
        first_statements.setdefault(extracted.type.value, extracted)
    baseline = draft_positions(draft, results[0])
    for statement in draft["statements"]:
        extracted = first_statements[statement["type"]]
        rows = [i for i in extracted.line_items if i.cells]
        for row, item in zip(statement["rows"], rows, strict=True):
            row["canonical_id"] = item.canonical_id
    (output / "reading-draft.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2) + "\n"
    )
    pdf = pdfium.PdfDocument(str(pdf_path))
    pages = sorted({r["source"]["page_no"] for cell in mapping for r in cell["runs"] if r})
    views: list[str] = []
    try:
        for number in pages:
            page = pdf[number - 1]
            bitmap = page.render(scale=1.5)
            try:
                bitmap.to_pil().save(output / f"page-{number}.png")
                width, height = page.get_size()
            finally:
                bitmap.close()
                page.close()
            boxes = []
            for index, cell in enumerate(mapping):
                for run, observation in enumerate(cell["runs"]):
                    if observation is None or observation["source"]["page_no"] != number:
                        continue
                    box = observation["source"]["bbox"]
                    boxes.append(
                        f'<a href="#cell-{index}"><rect x="{box["left"]}" y="{box["top"]}" '
                        f'width="{box["right"] - box["left"]}" '
                        f'height="{box["bottom"] - box["top"]}">'
                        f"<title>Cell {index + 1}, snapshot {run + 1}</title></rect></a>"
                    )
            views.append(
                f'<section id="page-{number}"><h2>PDF page {number}</h2><div class="page">'
                f'<img src="page-{number}.png" alt="Original PDF page {number}">'
                f'<svg viewBox="0 0 {width} {height}">{"".join(boxes)}</svg></div></section>'
            )
    finally:
        pdf.close()
    rows_html = []
    for index, cell in enumerate(mapping):
        observations = []
        for run, observation in enumerate(cell["runs"]):
            if observation is None:
                observations.append(f"<div>Snapshot {run + 1}: MISSING</div>")
                continue
            source = observation["source"]
            observations.append(
                f"<div>Snapshot {run + 1}: <b>{html.escape(str(observation['value']))}</b> "
                f'<a href="#page-{source["page_no"]}">page {source["page_no"]}</a>, '
                f"table {html.escape(source['table_ref'])}, row {source['row']}, "
                f"col {source['col']} — {render_row_identity(observation)}</div>"
            )
        status = "agrees" if cell["consistent"] else "REVIEW: differs / missing / only one snapshot"
        where = cell["position"]
        key = (
            cell["statement"],
            cell["label"],
            (where["page_no"], where["table_ref"], where["row"]),
            cell["period"],
        )
        position = baseline.get(key)
        controls = "Not in first draft: add/correct this row in the answer editor below."
        if position is not None:
            si, ri = position
            initial = draft["statements"][si]["rows"][ri]
            value = initial["values"][cell["period"]]
            period = html.escape(cell["period"], quote=True)
            value_text = html.escape("" if value is None else str(value), quote=True)
            canonical = html.escape(initial["canonical_id"] or "", quote=True)
            controls = (
                f'<div class="answer" data-si="{si}" data-ri="{ri}" data-period="{period}">'
                f'<label>Value <input class="value" value="{value_text}"></label>'
                f'<label>Taxonomy id <input class="canonical" value="{canonical}"></label>'
                '<label><input class="confirmed" type="checkbox">Checked against PDF</label></div>'
            )
        rows_html.append(
            f'<tr id="cell-{index}"><td>{index + 1}</td><td>{html.escape(cell["statement"])}</td>'
            f'<td dir="auto">{html.escape(cell["label"])}</td>'
            f"<td>{html.escape(cell['period'])}</td>"
            f"<td>{''.join(observations)}</td><td>{status}</td><td>{controls}</td></tr>"
        )
    template = Path(__file__).with_suffix(".html").read_text(encoding="utf-8")
    embedded = json.dumps(draft, ensure_ascii=False).replace("<", "\\u003c")
    substitutions = {
        "__TITLE__": html.escape(document_id),
        "__ROWS__": "".join(rows_html),
        "__PAGES__": "".join(views),
        "__DRAFT__": embedded,
        "__RUN_LINKS__": " · ".join(
            f'<a href="snapshot-{i}.json">Full snapshot {i}</a>'
            for i in range(1, len(snapshots) + 1)
        ),
    }
    # One pass: PDF text containing placeholder names is data, not template syntax.
    page_html = re.sub(
        r"__(?:TITLE|ROWS|PAGES|DRAFT|RUN_LINKS)__",
        lambda match: substitutions[match.group()],
        template,
    )
    target = output / "index.html"
    target.write_text(page_html, encoding="utf-8")
    return target


def capture(document_id: str, output: Path, runs: int = 2) -> Path:
    """Repeat a dev extraction with separate empty artifact roots; never a holdout command."""
    if runs < 2 or runs > 3:
        raise ValueError("capture requires two or three fresh runs")
    entries = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["documents"]
    entry = next((e for e in entries if e["id"] == document_id), None)
    if entry is None:
        raise ValueError("capture accepts dev golden documents only")
    config = load_config()
    engine = make_engine(config)
    output.mkdir(parents=True, exist_ok=False)
    snapshots = []
    for number in range(1, runs + 1):
        run_config = config.model_copy(update={"artifact_root": output / f"artifacts-{number}"})
        result = structure_pdf(MANIFEST.parent / entry["file"], run_config, engine, use_cache=False)
        snapshot = output / f"run-{number}.json"
        snapshot.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
        (output / f"config-{number}.json").write_text(
            run_config.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        snapshots.append(snapshot)
    return build_review(document_id, snapshots, output / "review")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PDF review map and human-assisted gold")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--id", required=True)
    build.add_argument("--snapshot", type=Path, action="append", required=True)
    build.add_argument("--out", type=Path, required=True)
    fresh = commands.add_parser("capture")
    fresh.add_argument("--id", required=True)
    fresh.add_argument("--out", type=Path, required=True)
    fresh.add_argument("--runs", type=int, default=2)
    approve = commands.add_parser("approve")
    approve.add_argument("reading", type=Path)
    approve.add_argument("output", type=Path)
    approve.add_argument(
        "--full-source",
        action="store_true",
        help="declare all primary source statements/rows checked, including omissions",
    )
    args = parser.parse_args(argv)
    if args.command == "build":
        print(build_review(args.id, args.snapshot, args.out))
    elif args.command == "capture":
        print(capture(args.id, args.out, args.runs))
    else:
        approved_gold(
            args.reading,
            args.output,
            reference_scope=(
                "full_primary_statements" if args.full_source else "reviewed_packet_slots"
            ),
        )
        print("Human-assisted gold frozen; consistency alone was not accepted as accuracy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
