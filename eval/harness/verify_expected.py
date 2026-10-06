"""Independent evidence for each figure of an expected file (spec 12, Expected files).

    PYTHONPATH=eval uv run python -m harness.verify_expected

An expected file is first compared with the page by a reader. This gives each figure a second,
independent check, so that a person only has to look at the figures nothing else vouches for:

- arithmetic: the figure sits in a sum, an identity or a tie that holds on the expected
  figures themselves. A wrong figure breaks its sum unless a second error cancels it exactly.
- reread: the cell's box on the page is read again on its own, from the PDF text layer on a
  digital page and by OCR of the enlarged crop on a scanned one, and gives the same figure.
- sibling: the same figure stands under the same period in the same statement of another
  filing of the issuer (the other language edition, or the next year's comparative column).

Writes var/eval/expected-verification.json and prints, per file, the figures left over.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import yaml

from fra_core.numbers import parse_number
from fra_core.schemas import (
    BBox,
    Cell,
    LineItem,
    Provenance,
    Statement,
    StatementType,
    TextSource,
)
from fra_core.taxonomy.loader import load_taxonomy
from fra_ingest.config import REPO_ROOT, load_config
from fra_ingest.figure_checks import check_net_profit_tie
from fra_ingest.label_match import LabelIndex, has_subtotal_cue
from fra_ingest.ocr import OcrEngine, VisionOcr
from fra_ingest.parts import is_per_share
from fra_ingest.structure import structure_pdf
from fra_ingest.table_checks import run_checks
from harness.expected import EXPECTED_DIR, MANIFEST, ExpectedStatement, load_expected
from harness.extraction import align_rows

OUT = REPO_ROOT / "var" / "eval"
CellKey = tuple[int, str]
_NOWHERE = BBox(left=0, top=0, right=1, bottom=1)
# Points added around a cell's box, and the zoom it is rendered at, before it is read again.
_PAD = 3.0
_ZOOM = 5.0


def as_statement(expected: ExpectedStatement, index: LabelIndex) -> Statement:
    """The expected rows as a statement the structure checks can run on. Row ``i`` is ``e<i>``."""
    items = []
    for i, row in enumerate(expected.rows):
        known = index.match(row.label, expected.type)
        total = (
            has_subtotal_cue(row.label)
            or not row.label.strip()
            or (known is not None and known.subtotal)
        )
        flags = ["per_share"] if is_per_share(row.label) else []
        cells = [
            Cell(
                period_key=key,
                reported=value,
                raw_text="" if value is None else str(value),
                provenance=Provenance(
                    page_no=1, bbox=_NOWHERE, table_ref="#/expected", row=i, col=0
                ),
                flags=flags if value is not None else [*flags, "numbers_missing"],
            )
            for key, value in row.values.items()
        ]
        items.append(LineItem(id=f"e{i}", raw_label=row.label, is_subtotal=total, cells=cells))
    return Statement(
        id=f"expected-{expected.type.value}",
        document_sha256="0" * 64,
        type=expected.type,
        currency=expected.currency,
        scale=expected.scale,
        periods=expected.periods,
        line_items=items,
    )


def _passing_cells(statement: Statement, checks: Sequence[Any]) -> set[CellKey]:
    valued = {
        (i.id, c.period_key)
        for i in statement.line_items
        for c in i.cells
        if c.reported is not None
    }
    cells: set[CellKey] = set()
    for check in checks:
        # A total equal to the one row above it is one figure printed twice, not a check.
        if check.status != "pass" or "single_addend" in check.detail:
            continue
        for item_id in check.line_item_ids:
            if (item_id, check.period_key) in valued:
                cells.add((int(item_id[1:]), check.period_key))
    return cells


def arithmetic_cells(expected: ExpectedStatement, index: LabelIndex | None = None) -> set[CellKey]:
    """The figures that sit in a sum or identity that holds on the expected figures."""
    index = index or LabelIndex(load_taxonomy())
    statement, checks = run_checks(as_statement(expected, index), index)
    return _passing_cells(statement, checks)


def tie_cells(
    income: ExpectedStatement, comprehensive: ExpectedStatement, index: LabelIndex
) -> tuple[set[CellKey], set[CellKey]]:
    """Net profit on both statements, when the two agree: (income cells, comprehensive cells)."""
    first, second = as_statement(income, index), as_statement(comprehensive, index)
    ties = [t for t in check_net_profit_tie(first, second) if t.status == "pass"]
    mine: set[CellKey] = set()
    theirs: set[CellKey] = set()
    for tie in ties:
        income_row, comprehensive_row = tie.line_item_ids
        mine.add((int(income_row[1:]), tie.period_key))
        theirs.add((int(comprehensive_row[1:]), tie.period_key))
    return mine, theirs


def sibling_cells(expected: ExpectedStatement, others: Sequence[Statement]) -> set[CellKey]:
    """The figures that stand, after scale, under the same period in a statement of the same
    type in another filing. A zero proves nothing."""
    seen = {
        (cell.period_key, cell.reported * other.scale)
        for other in others
        if other.type is expected.type
        for item in other.line_items
        for cell in item.cells
        if cell.reported
    }
    return {
        (i, key)
        for i, row in enumerate(expected.rows)
        for key, value in row.values.items()
        if value and (key, value * expected.scale) in seen
    }


def reread_cells(
    expected: ExpectedStatement, statement: Statement | None, read: Callable[[Cell], str | None]
) -> set[CellKey]:
    """The figures a second read of the cell's own box agrees with. ``read`` returns the text
    found in an extracted cell's box, or None."""
    if statement is None:
        return set()
    cells: set[CellKey] = set()
    aligned = align_rows(expected.rows, statement.line_items)
    for i, (row, position) in enumerate(zip(expected.rows, aligned, strict=True)):
        if position is None:
            continue
        by_period = {c.period_key: c for c in statement.line_items[position].cells}
        for key, value in row.values.items():
            cell = by_period.get(key)
            if value is None or cell is None or "bbox_synthesized" in cell.flags:
                continue
            text = read(cell)
            if text and parse_number(" ".join(text.split())).value == value:
                cells.add((i, key))
    return cells


def unvouched(expected: ExpectedStatement, vouched: set[CellKey]) -> list[CellKey]:
    """The figures no evidence vouches for, in row order."""
    return [
        (i, key)
        for i, row in enumerate(expected.rows)
        for key, value in row.values.items()
        if value is not None and (i, key) not in vouched
    ]


def box_reader(pdf: Path, engine: OcrEngine | None, language: str) -> Callable[[Cell], str | None]:
    """Reads an extracted cell's box again: the text layer where the page has one, else OCR of
    the box rendered large."""
    document = pdfium.PdfDocument(str(pdf))
    languages = ["ar-SA", "en-US"] if language == "ar" else ["en-US"]

    def read(cell: Cell) -> str | None:
        page = document[cell.provenance.page_no - 1]
        height, box = page.get_height(), cell.provenance.bbox
        if cell.provenance.source is TextSource.TEXT:
            return str(
                page.get_textpage().get_text_bounded(
                    left=box.left - 1,
                    bottom=height - box.bottom - 1,
                    right=box.right + 1,
                    top=height - box.top + 1,
                )
            )
        if engine is None:
            return None
        image = page.render(scale=_ZOOM).to_pil()
        crop = image.crop(
            (
                int((box.left - _PAD) * _ZOOM),
                int((box.top - _PAD) * _ZOOM),
                int((box.right + _PAD) * _ZOOM),
                int((box.bottom + _PAD) * _ZOOM),
            )
        )
        lines = engine.recognize(crop, languages)
        return " ".join(line.text for line in lines) or None

    return read


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m harness.verify_expected")
    parser.add_argument(
        "--no-ocr", action="store_true", help="skip the second read of scanned cells"
    )
    args = parser.parse_args(argv)
    config = load_config()
    index = LabelIndex(load_taxonomy())
    # Always Vision, whatever convert.ocr_engine says: the evidence is independent of the engine
    # that produced the extraction.
    engine = None if args.no_ocr else VisionOcr()
    entries = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["documents"]
    by_id = {d["id"]: d for d in entries}
    report: list[dict[str, Any]] = []

    def first_statements(document_id: str) -> dict[StatementType, Statement]:
        result = structure_pdf(MANIFEST.parent / by_id[document_id]["file"], config, None)
        found: dict[StatementType, Statement] = {}
        for s in result.statements:
            found.setdefault(s.type, s)
        return found

    for path in sorted(EXPECTED_DIR.glob("*.json")):
        expected = load_expected(path)
        entry = by_id[expected.id]
        own = first_statements(expected.id)
        siblings = [
            s
            for other in entries
            if other["issuer"] == entry["issuer"] and other["id"] != expected.id
            for s in first_statements(other["id"]).values()
        ]
        read = box_reader(MANIFEST.parent / entry["file"], engine, entry["language"])
        by_type = {s.type: s for s in expected.statements}
        ties: dict[StatementType, set[CellKey]] = {}
        income = by_type.get(StatementType.INCOME)
        comprehensive = by_type.get(StatementType.COMPREHENSIVE_INCOME)
        if income is not None and comprehensive is not None:
            ties[income.type], ties[comprehensive.type] = tie_cells(income, comprehensive, index)
        for statement in expected.statements:
            evidence = {
                "arithmetic": arithmetic_cells(statement, index) | ties.get(statement.type, set()),
                "reread": reread_cells(statement, own.get(statement.type), read),
                "sibling": sibling_cells(statement, siblings),
            }
            vouched = set().union(*evidence.values())
            left = unvouched(statement, vouched)
            figures = sum(1 for row in statement.rows for v in row.values.values() if v is not None)
            counts = {name: len(cells) for name, cells in evidence.items()}
            twice = sum(
                1 for key in vouched if sum(key in cells for cells in evidence.values()) > 1
            )
            print(
                f"{expected.id:34} {statement.type.value:22} figures {figures:3}  "
                f"arithmetic {counts['arithmetic']:3}  reread {counts['reread']:3}  "
                f"sibling {counts['sibling']:3}  two or more {twice:3}  none {len(left)}"
            )
            for i, key in left:
                row = statement.rows[i]
                print(f"    not vouched for: {row.label!r} {key} {row.values[key]}")
            report.append(
                {
                    "id": expected.id,
                    "type": statement.type.value,
                    "figures": figures,
                    **counts,
                    "two_or_more": twice,
                    "unvouched": [
                        {
                            "row": i,
                            "label": statement.rows[i].label,
                            "period": key,
                            "value": str(statement.rows[i].values[key]),
                        }
                        for i, key in left
                    ],
                }
            )
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "expected-verification.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
