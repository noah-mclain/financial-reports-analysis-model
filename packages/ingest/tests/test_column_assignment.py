"""Conservative horizontal assignment of two merged OCR amounts."""

from collections.abc import Iterator
from decimal import Decimal

import pytest
from support import page, span
from test_parts import grid
from test_structure import INCOME_1, INCOME_HEADER, cells, document, inputs, table

from fra_core.numbers import parse_number
from fra_core.schemas import BBox, PageMode, StatementType, TextSource
from fra_ingest.classify import Classification
from fra_ingest.column_assignment import split_numeric_columns
from fra_ingest.config import IngestConfig
from fra_ingest.docling_json import DlDocument
from fra_ingest.header import HeaderLayout, parse_header
from fra_ingest.parts import PartialStatement, build_part
from fra_ingest.results import StructureResult
from fra_ingest.row_alignment import realign_rows
from fra_ingest.structure import StructureInputs, structure_document
from fra_ingest.table_grid import Grid, GridCell

LITERAL = "2,505,304,778 1,345,663,827"
LABEL = "TOTAL COMPREHENSIVE INCOME FOR THE YEAR"
WITNESSES = (1, 3, 5, 6, 9, 10, 11)
KIND = StatementType.COMPREHENSIVE_INCOME


def merged_grid() -> Grid:
    rows = [["", "Notes", "2025 EGP", "2024 EGP"]]
    rows.extend(
        [f"Line {r}", "", "1,234,567", "2,345,678"]
        if r in WITNESSES
        else [LABEL, "", LITERAL, ""]
        if r == 7
        else ["", "", "", ""]
        for r in range(1, 12)
    )
    return replace_cell(grid(rows), 7, 2, bbox=BBox(left=210, right=390, top=145, bottom=155))


def replace_cell(g: Grid, row: int, col: int, **changes: object) -> Grid:
    return g.model_copy(
        update={
            "cells": tuple(
                c.model_copy(update=changes) if (c.row, c.col) == (row, col) else c for c in g.cells
            )
        }
    )


def layout(g: Grid) -> HeaderLayout:
    return parse_header(g, KIND, None)


def part(g: Grid, header: HeaderLayout) -> PartialStatement:
    return build_part(g, header, Classification(type=KIND, confidence=1), source=TextSource.OCR)


def test_two_amounts_split_with_bound_periods_original_provenance_and_idempotence() -> None:
    original = merged_grid()
    header = layout(original)
    before = original.model_dump_json()
    unrepaired = part(original, header).line_items[4].cells
    assert [c.reported for c in unrepaired] == [None, None]
    assert unrepaired[0].raw_text == LITERAL
    repaired = split_numeric_columns(original, header)
    assert original.model_dump_json() == before
    assert split_numeric_columns(repaired, header) is repaired
    assert Grid.model_validate_json(repaired.model_dump_json()) == repaired
    source = original.cell(7, 2)
    assert source is not None
    for col in (2, 3):
        cell = repaired.cell(7, col)
        assert cell is not None
        assert (cell.source_row, cell.source_col) == (7, 2)
        assert cell.bbox == source.bbox
        assert cell.flags == ("numeric_columns_split",)
    built = part(repaired, header)
    assert PartialStatement.model_validate_json(built.model_dump_json()) == built
    amounts = built.line_items[4].cells
    assert [(c.period_key, c.reported, c.raw_text) for c in amounts] == [
        (header.value_cols[2].key, Decimal("2505304778"), "2,505,304,778"),
        (header.value_cols[3].key, Decimal("1345663827"), "1,345,663,827"),
    ]
    assert all(
        c.provenance.row == 7
        and c.provenance.col == 2
        and c.provenance.bbox == source.bbox
        and c.provenance.source is TextSource.OCR
        and "numeric_columns_split" in c.flags
        for c in amounts
    )


def test_rightmost_label_column_is_not_split() -> None:
    g = merged_grid()
    shifted = tuple(c.model_copy(update={"col": {0: 3, 1: 0, 2: 1, 3: 2}[c.col]}) for c in g.cells)
    g = g.model_copy(update={"cells": shifted})
    header = layout(g)
    assert header.label_col == 3
    assert split_numeric_columns(g, header) is g


def test_witnesses_from_another_table_section_do_not_establish_column_order() -> None:
    g = merged_grid()
    # The two remaining witnesses precede a new section; target row 7 follows it.
    g = g.model_copy(
        update={
            "cells": (
                *tuple(
                    c.model_copy(update={"text": ""})
                    if c.row in WITNESSES[2:] and c.col in (2, 3)
                    else c
                    for c in g.cells
                ),
                GridCell(
                    text="OTHER SECTION",
                    row=4,
                    col=0,
                    bbox=None,
                    page_no=g.page_no,
                    is_row_section=True,
                ),
            )
        }
    )
    assert split_numeric_columns(g, layout(g)) is g


def test_each_table_grid_needs_its_own_two_witnesses() -> None:
    g = merged_grid()
    g = g.model_copy(
        update={
            "cells": tuple(
                c.model_copy(update={"text": ""})
                if c.row in WITNESSES[1:] and c.col in (2, 3)
                else c
                for c in g.cells
            )
        }
    )
    assert split_numeric_columns(g, layout(g)) is g


def test_each_section_uses_its_own_witnesses_and_centres() -> None:
    g = merged_grid()
    cells = [
        c.model_copy(update={"text": ""})
        if c.row in WITNESSES and c.row not in (1, 3, 10, 11) and c.col in (2, 3)
        else c
        for c in g.cells
        if (c.row, c.col) not in {(9, 0), (9, 2), (9, 3)}
    ]
    # Section B has one witness, so its merged row must remain unsplit.
    cells.extend(
        [
            GridCell(
                text="SECTION B", row=8, col=0, bbox=None, page_no=g.page_no, is_row_section=True
            ),
            GridCell(
                text=LABEL,
                row=9,
                col=0,
                bbox=BBox(left=10, right=180, top=185, bottom=195),
                page_no=g.page_no,
            ),
            GridCell(
                text=LITERAL,
                row=9,
                col=2,
                bbox=BBox(left=210, right=390, top=185, bottom=195),
                page_no=g.page_no,
            ),
            GridCell(text="", row=9, col=3, bbox=None, page_no=g.page_no),
        ]
    )
    cells = [
        c.model_copy(update={"text": ""}) if c.row == 11 and c.col in (2, 3) else c for c in cells
    ]
    g = g.model_copy(update={"cells": tuple(cells)})
    repaired = split_numeric_columns(g, layout(g))
    assert repaired.text(7, 3) == "1,345,663,827"
    assert repaired.text(9, 2) == LITERAL
    assert repaired.text(9, 3) == ""


def test_each_section_splits_with_its_own_centres() -> None:
    g = merged_grid()
    cells = [
        c.model_copy(update={"text": ""})
        if c.row in WITNESSES and c.row not in (1, 3, 10, 11) and c.col in (2, 3)
        else c
        for c in g.cells
        if (c.row, c.col) not in {(9, 0), (9, 2), (9, 3)}
    ]
    cells.extend(
        [
            GridCell(
                text="SECTION B", row=8, col=0, bbox=None, page_no=g.page_no, is_row_section=True
            ),
            GridCell(
                text=LABEL,
                row=9,
                col=0,
                bbox=BBox(left=10, right=180, top=185, bottom=195),
                page_no=g.page_no,
            ),
            GridCell(
                text=LITERAL,
                row=9,
                col=2,
                bbox=BBox(left=610, right=790, top=185, bottom=195),
                page_no=g.page_no,
            ),
            GridCell(text="", row=9, col=3, bbox=None, page_no=g.page_no),
        ]
    )
    for row, top in ((10, 205), (11, 225)):
        for col, left, right in ((2, 610, 690), (3, 710, 790)):
            cells = [
                c.model_copy(
                    update={"bbox": BBox(left=left, right=right, top=top, bottom=top + 10)}
                )
                if c.row == row and c.col == col
                else c
                for c in cells
            ]
    g = g.model_copy(update={"cells": tuple(cells)})
    repaired = split_numeric_columns(g, layout(g))
    assert repaired.text(7, 3) == "1,345,663,827"
    assert repaired.text(9, 2) == "2,505,304,778"
    assert repaired.text(9, 3) == "1,345,663,827"


def test_row_left_unresolved_by_realignment_is_not_used_as_a_witness() -> None:
    g = merged_grid()
    # Keep two witness texts, but displace both and leave an unboxed value so neither can move.
    for row in WITNESSES[:2]:
        g = replace_cell(g, row, 2, bbox=BBox(left=210, right=290, top=1000, bottom=1010))
        g = replace_cell(g, row, 3, bbox=BBox(left=310, right=390, top=1000, bottom=1010))
    g = replace_cell(g, 1, 3, bbox=None)
    g = g.model_copy(
        update={
            "cells": tuple(
                c.model_copy(update={"text": ""})
                if c.row in WITNESSES[2:] and c.col in (2, 3)
                else c
                for c in g.cells
            )
        }
    )
    aligned = realign_rows(g, layout(g))
    assert len(aligned.unresolved) == 2
    first = aligned.grid.cell(1, 2)
    second_left = aligned.grid.cell(3, 2)
    second_right = aligned.grid.cell(3, 3)
    assert first is not None and "row_misaligned" in first.flags
    assert second_left is not None and "row_misaligned" in second_left.flags
    assert second_right is not None and "row_misaligned" in second_right.flags
    assert split_numeric_columns(aligned.grid, layout(aligned.grid)) is aligned.grid


def test_period_bindings_follow_the_layout_even_when_years_are_swapped() -> None:
    g = merged_grid()
    header = layout(g)
    header.value_cols = {2: header.value_cols[3], 3: header.value_cols[2]}
    amounts = part(split_numeric_columns(g, header), header).line_items[4].cells
    assert amounts[0].period_key == header.value_cols[2].key
    assert amounts[0].reported == Decimal("2505304778")
    assert amounts[1].period_key == header.value_cols[3].key


def test_split_preserves_an_existing_source_position_and_flags() -> None:
    g = replace_cell(merged_grid(), 7, 2, source_row=8, source_col=1, flags=("row_realigned",))
    amounts = part(split_numeric_columns(g, layout(g)), layout(g)).line_items[4].cells
    assert all((c.provenance.row, c.provenance.col) == (8, 1) for c in amounts)
    assert all("row_realigned" in c.flags and "numeric_columns_split" in c.flags for c in amounts)


def test_witness_centres_use_medians_and_separate_physical_column_bands() -> None:
    g = merged_grid()
    g = replace_cell(g, 7, 2, bbox=BBox(left=240, right=360, top=145, bottom=155))
    # Extreme centres change the mean but not the median; neither box crosses columns.
    g = replace_cell(g, 1, 2, bbox=BBox(left=0, right=100, top=25, bottom=35))
    g = replace_cell(g, 1, 3, bbox=BBox(left=510, right=590, top=25, bottom=35))
    assert split_numeric_columns(g, layout(g)).text(7, 3) == "1,345,663,827"

    # Every pair remains disjoint, but column bands overlap across different rows.
    g = replace_cell(g, 1, 2, bbox=BBox(left=310, right=390, top=25, bottom=35))
    assert split_numeric_columns(g, layout(g)) is g


@pytest.mark.parametrize(
    "literal",
    [
        "1,234 2,345 3,456",
        "-1,234 2,345",
        "+1,234 2,345",
        "(1,234) 2,345",
        "1,234.5 2,345",
        "1,234% 2,345",
        "1 234 2 345",
        "1,234 2,345(a)",
        "1,234¹ 2,345",
        "1,23 2,345",
        "1,234 words",
        "١,٢٣٤ ٢,٣٤٥",
        "1,234 567",
        "1,234",
        "2 505 304 778",
    ],
)
def test_unsupported_literals_return_the_original_grid(literal: str) -> None:
    g = replace_cell(merged_grid(), 7, 2, text=literal)
    assert split_numeric_columns(g, layout(g)) is g


def test_a_whole_literal_that_already_parses_is_never_split() -> None:
    g = replace_cell(merged_grid(), 7, 2, text="1,234 567")
    assert parse_number(g.text(7, 2)).value is not None
    assert split_numeric_columns(g, layout(g)) is g


@pytest.mark.parametrize(
    "change",
    [
        "occupied",
        "zero",
        "dash",
        "right_span",
        "right_col_span",
        "source_span",
        "source_row_span",
        "one_witness",
        "overlap",
        "crossed",
        "one_median",
        "source_unboxed",
        "label_unboxed",
        "witness_unboxed",
        "source_infinite",
        "witness_nan",
        "label_infinite",
        "wrong_y",
        "unlabelled",
        "section",
        "misaligned",
        "label_misaligned",
        "witness_misaligned",
        "duplicate",
        "touching",
        "ambiguous_columns",
    ],
)
def test_unsupported_geometry_or_rows_return_the_original_grid(change: str) -> None:
    g = merged_grid()
    header = layout(g)
    if change in {"occupied", "zero", "dash", "right_span", "right_col_span"}:
        right = GridCell(
            text={"occupied": "3,456", "zero": "0", "dash": "-"}.get(change, ""),
            row=7,
            col=3,
            bbox=None,
            page_no=g.page_no,
            row_span=2 if change == "right_span" else 1,
            col_span=2 if change == "right_col_span" else 1,
        )
        g = g.model_copy(update={"cells": (*g.cells, right)})
    elif change == "source_span":
        g = replace_cell(g, 7, 2, col_span=2)
    elif change == "source_row_span":
        g = replace_cell(g, 7, 2, row_span=2)
    elif change in {"one_witness", "witness_misaligned"}:
        g = g.model_copy(
            update={
                "cells": tuple(
                    c
                    if c.row not in WITNESSES[1:] or c.col not in (2, 3)
                    else c.model_copy(
                        update={"text": ""}
                        if change == "one_witness"
                        else {"flags": ("row_misaligned",)}
                    )
                    for c in g.cells
                )
            }
        )
    elif change in {"source_unboxed", "label_unboxed", "witness_unboxed"}:
        row, col = {"source_unboxed": (7, 2), "label_unboxed": (7, 0), "witness_unboxed": (1, 2)}[
            change
        ]
        g = replace_cell(g, row, col, bbox=None)
    elif change in {"source_infinite", "witness_nan", "label_infinite"}:
        row, col = {"source_infinite": (7, 2), "witness_nan": (1, 2), "label_infinite": (7, 0)}[
            change
        ]
        g = replace_cell(
            g,
            row,
            col,
            bbox=BBox(
                left=210,
                right=float("nan") if change == "witness_nan" else float("inf"),
                top=145,
                bottom=155,
            ),
        )
    elif change in {"one_median", "wrong_y"}:
        g = replace_cell(
            g,
            7,
            2,
            bbox=BBox(
                left=210,
                right=300 if change == "one_median" else 390,
                top=100 if change == "wrong_y" else 145,
                bottom=110 if change == "wrong_y" else 155,
            ),
        )
    elif change in {"unlabelled", "section", "misaligned", "label_misaligned"}:
        g = replace_cell(
            g,
            7,
            2 if change == "misaligned" else 0,
            **(
                {"text": ""}
                if change == "unlabelled"
                else {"is_row_section": True}
                if change == "section"
                else {"flags": ("row_misaligned",)}
            ),
        )
    elif change == "duplicate":
        source = g.cell(7, 2)
        assert source is not None
        g = g.model_copy(update={"cells": (*g.cells, source)})
    else:
        for row in WITNESSES:
            if change == "crossed":
                g = replace_cell(
                    g,
                    row,
                    2,
                    bbox=BBox(left=310, right=390, top=20 * row + 5, bottom=20 * row + 15),
                )
                g = replace_cell(
                    g,
                    row,
                    3,
                    bbox=BBox(left=210, right=290, top=20 * row + 5, bottom=20 * row + 15),
                )
            else:
                left = 290 if change == "touching" else 280 if change == "overlap" else 250
                # Disjoint within a row, but not independent physical column bands.
                if change == "ambiguous_columns" and row != 1:
                    continue
                g = replace_cell(
                    g,
                    row,
                    3 if change != "ambiguous_columns" else 2,
                    bbox=BBox(left=left, right=390, top=20 * row + 5, bottom=20 * row + 15),
                )
    assert split_numeric_columns(g, header) is g


@pytest.mark.parametrize(
    "change", ["three", "unbound", "conflicted", "non_adjacent", "same_period"]
)
def test_ambiguous_bindings_return_the_original_grid(change: str) -> None:
    g = merged_grid()
    header = layout(g)
    if change == "three":
        header.value_cols[4] = header.value_cols[3]
    elif change == "unbound":
        header.unbound_cols = [3]
    elif change == "conflicted":
        header.evidence.append("period_conflict:3")
    elif change == "same_period":
        header.value_cols[3] = header.value_cols[2]
    else:
        header.value_cols[4] = header.value_cols.pop(3)
    assert split_numeric_columns(g, header) is g


def test_an_explicit_empty_unit_destination_and_two_witnesses_are_enough() -> None:
    g = merged_grid()
    g = g.model_copy(
        update={
            "cells": (
                *(c for c in g.cells if c.row not in WITNESSES[2:]),
                GridCell(text="  ", row=7, col=3, bbox=None, page_no=g.page_no),
            )
        }
    )
    repaired = split_numeric_columns(g, layout(g))
    assert repaired.text(7, 3) == "1,345,663,827"
    assert sum(c.row == 7 and c.col == 3 for c in repaired.cells) == 1


@pytest.mark.parametrize("mode,split", [(PageMode.IMAGE, True), (PageMode.TEXT, False)])
def test_structure_only_splits_logical_ocr_grids(mode: PageMode, split: bool) -> None:
    doc = document()
    table = doc.tables[0]
    rows = [
        ["", "Notes", "31 December 2025 SAR", "31 December 2024 SAR"],
        ["Inventories", "", "1,234", "2,345"],
        ["Cash", "", "3,456", "4,567"],
        ["Total assets", "", LITERAL, ""],
    ]
    # Construct synthetic Docling data only; no renderer, OCR engine or conversion.
    synthetic = type(table.data).model_validate(
        {"num_rows": 4, "num_cols": 4, "table_cells": cells(rows)}
    )
    doc = doc.model_copy(update={"tables": [table.model_copy(update={"data": synthetic})]})
    merged = doc.tables[0].data.table_cells[-1]
    assert merged.bbox is not None
    merged = merged.model_copy(update={"bbox": merged.bbox.model_copy(update={"right": 630})})
    data = synthetic.model_copy(update={"table_cells": [*synthetic.table_cells[:-1], merged]})
    doc = doc.model_copy(update={"tables": [table.model_copy(update={"data": data})]})
    supplied = inputs([("synthetic.json", doc)]).model_copy(
        update={"page_modes": {1: mode}, "visual_pages": set()}
    )
    result, _ = structure_document(supplied, IngestConfig())
    amounts = result.statements[0].line_items[-1].cells
    assert any("numeric_columns_split" in c.flags for c in amounts) is split
    assert [c.reported for c in amounts] == (
        [Decimal("2505304778"), Decimal("1345663827")] if split else [None, None]
    )
    if split:
        review = result.reviews[0]
        assert review.status == "needs_review"
        assert "numeric_columns_split:2" in review.reasons
        assert "numeric_columns_split:2" in review.warnings


def test_structure_does_not_split_image_grid_with_rightmost_label() -> None:
    doc = document()
    table = doc.tables[0]
    rows = [
        ["Notes", "31 December 2025 SAR", "31 December 2024 SAR", ""],
        ["", "1,234", "2,345", "Inventories"],
        ["", "3,456", "4,567", "Cash"],
        ["", LITERAL, "", "Total assets"],
    ]
    data = type(table.data).model_validate(
        {"num_rows": 4, "num_cols": 4, "table_cells": cells(rows)}
    )
    merged = next(c for c in data.table_cells if c.text == LITERAL)
    assert merged.bbox is not None
    merged = merged.model_copy(update={"bbox": merged.bbox.model_copy(update={"l": 210, "r": 480})})
    data = data.model_copy(
        update={"table_cells": [merged if c.text == LITERAL else c for c in data.table_cells]}
    )
    doc = doc.model_copy(update={"tables": [table.model_copy(update={"data": data})]})
    supplied = inputs([("synthetic.json", doc)]).model_copy(
        update={"page_modes": {1: PageMode.IMAGE}, "visual_pages": set()}
    )
    result, _ = structure_document(supplied, IngestConfig())
    amounts = result.statements[0].line_items[-1].cells
    assert [c.reported for c in amounts] == [None, None]
    assert all("numeric_columns_split" not in c.flags for c in amounts)


def section_cell(g: Grid, row: int) -> GridCell:
    return GridCell(
        text=f"SECTION {row}", row=row, col=0, bbox=None, page_no=g.page_no, is_row_section=True
    )


def amount_cells(g: Grid, row: int, label: str, left: str, right: str) -> list[GridCell]:
    top = 100 + 20 * row
    return [
        GridCell(
            text=text,
            row=row,
            col=col,
            bbox=BBox(left=x0, right=x1, top=top, bottom=top + 10),
            page_no=g.page_no,
        )
        for col, text, x0, x1 in (
            (0, label, 10, 180),
            (2, left, 210, 290 if right else 390),
            (3, right, 310, 390),
        )
        if text
    ] + ([] if right else [GridCell(text="", row=row, col=3, bbox=None, page_no=g.page_no)])


def sectioned_grid() -> Grid:
    base = grid([["", "Notes", "2025 EGP", "2024 EGP"]])
    cells = list(base.cells)
    row = 1
    for section in range(3):
        cells.append(section_cell(base, row))
        row += 1
        for _ in range(2):
            cells.extend(amount_cells(base, row, f"Line {row}", "1,234,567", "2,345,678"))
            row += 1
        if section != 1:
            cells.extend(
                amount_cells(base, row, LABEL, f"{section + 1},505,304,778 1,345,663,827", "")
            )
            row += 1
    return base.model_copy(update={"cells": tuple(cells), "num_rows": row})


def test_each_of_three_sections_splits_only_its_own_merged_row() -> None:
    g = sectioned_grid()
    header = layout(g)
    repaired = split_numeric_columns(g, header)
    changed = [c for c in repaired.cells if "numeric_columns_split" in c.flags]
    assert [(c.row, c.col, c.text, c.source_row, c.source_col) for c in changed] == [
        (4, 2, "1,505,304,778", 4, 2),
        (4, 3, "1,345,663,827", 4, 2),
        (11, 2, "3,505,304,778", 11, 2),
        (11, 3, "1,345,663,827", 11, 2),
    ]
    assert all(c.flags == ("numeric_columns_split",) for c in changed)
    assert repaired.text(6, 2) == "1,234,567"
    untouched = [c for c in repaired.cells if c.row not in (4, 11)]
    assert untouched == [c for c in g.cells if c.row not in (4, 11)]


def test_split_scans_grid_cells_a_bounded_number_of_times() -> None:
    class Counting(tuple[GridCell, ...]):
        scans = 0

        def __iter__(self) -> Iterator[GridCell]:
            Counting.scans += 1
            return super().__iter__()

    base = grid([["", "Notes", "2025 EGP", "2024 EGP"]])
    cells = list(base.cells)
    row = 1
    for _ in range(30):
        cells.append(section_cell(base, row))
        row += 1
        for _ in range(8):
            cells.extend(amount_cells(base, row, f"Line {row}", "1,234,567", "2,345,678"))
            row += 1
        cells.extend(amount_cells(base, row, LABEL, "2,505,304,778 1,345,663,827", ""))
        row += 1
    g = base.model_copy(update={"cells": tuple(cells), "num_rows": row})
    header = layout(g)
    counted = Grid.model_construct(**{**dict(g), "cells": Counting(g.cells)})
    Counting.scans = 0
    repaired = split_numeric_columns(counted, header)
    assert Counting.scans <= 10
    assert sum("numeric_columns_split" in c.flags for c in repaired.cells) == 60


def only_witnesses(g: Grid, keep: tuple[int, ...]) -> Grid:
    return g.model_copy(
        update={
            "cells": tuple(
                c.model_copy(update={"text": ""})
                if c.row in WITNESSES and c.row not in keep and c.col in (2, 3)
                else c
                for c in g.cells
            )
        }
    )


def move_witness(g: Grid, row: int, top: float, bottom: float) -> Grid:
    for col, left, right in ((2, 210, 290), (3, 310, 390)):
        g = replace_cell(g, row, col, bbox=BBox(left=left, right=right, top=top, bottom=bottom))
    return g


def test_two_witness_rows_with_identical_rectangles_are_one_observation() -> None:
    g = only_witnesses(merged_grid(), (1, 3))
    g = move_witness(g, 1, 25, 35)
    g = move_witness(g, 3, 25, 35)
    assert split_numeric_columns(g, layout(g)) is g


def test_witness_rows_whose_bands_overlap_by_one_point_are_not_distinct() -> None:
    g = only_witnesses(merged_grid(), (1, 3))
    g = move_witness(g, 1, 25, 35)
    g = move_witness(g, 3, 35, 45)
    assert split_numeric_columns(g, layout(g)) is g


def test_two_identical_witness_rows_and_one_distinct_row_split() -> None:
    g = only_witnesses(merged_grid(), (1, 3, 5))
    g = move_witness(g, 1, 25, 35)
    g = move_witness(g, 3, 25, 35)
    repaired = split_numeric_columns(g, layout(g))
    assert repaired.text(7, 2) == "2,505,304,778"
    assert repaired.text(7, 3) == "1,345,663,827"


def test_two_distinct_witness_rows_still_split() -> None:
    g = only_witnesses(merged_grid(), (1, 3))
    repaired = split_numeric_columns(g, layout(g))
    assert repaired.text(7, 2) == "2,505,304,778"
    assert repaired.text(7, 3) == "1,345,663,827"


def move_label(g: Grid, row: int, left: float, right: float) -> Grid:
    cell = g.cell(row, 0)
    assert cell is not None and cell.bbox is not None
    return replace_cell(
        g, row, 0, bbox=BBox(left=left, right=right, top=cell.bbox.top, bottom=cell.bbox.bottom)
    )


def test_a_label_physically_right_of_the_amounts_is_not_split_despite_its_column_number() -> None:
    g = merged_grid()
    for row in (*WITNESSES, 7):
        g = move_label(g, row, 410, 580)
    header = layout(g)
    assert header.label_col == 0
    assert split_numeric_columns(g, header) is g


def test_witnesses_with_labels_physically_right_of_their_amounts_are_not_counted() -> None:
    g = only_witnesses(merged_grid(), (1, 3))
    for row in (1, 3):
        g = move_label(g, row, 410, 580)
    assert split_numeric_columns(g, layout(g)) is g


def test_witnesses_and_merged_row_with_physically_left_labels_still_split_left_to_right() -> None:
    g = only_witnesses(merged_grid(), (1, 3))
    for row in (1, 3, 7):
        label, amount = g.cell(row, 0), g.cell(row, 2)
        assert label is not None and label.bbox is not None
        assert amount is not None and amount.bbox is not None
        assert label.bbox.right <= amount.bbox.left
    repaired = split_numeric_columns(g, layout(g))
    assert repaired.text(7, 2) == "2,505,304,778"
    assert repaired.text(7, 3) == "1,345,663,827"


def test_a_merged_row_whose_own_label_is_physically_right_is_not_split() -> None:
    g = move_label(only_witnesses(merged_grid(), (1, 3)), 7, 410, 580)
    assert split_numeric_columns(g, layout(g)) is g


INCOME_MERGED = ("Total comprehensive income", "Other income", "Finance cost")


def _income_tail(header: list[str], mode: PageMode, *, titled: bool = False) -> StructureInputs:
    """Page 1 income rows, then a low-confidence OCR tail whose merged rows pull the centre of
    the left value column past the continuation tolerance."""
    tail = [
        header,
        ["Profit for the year", "", "40", "35"],
        ["Basic earnings per share", "", "4", "3"],
        *([label, "", LITERAL, ""] for label in INCOME_MERGED),
    ]
    data = type(document().tables[0].data).model_validate(
        {"num_rows": len(tail), "num_cols": 4, "table_cells": cells(tail)}
    )
    widened = [
        c.model_copy(update={"bbox": c.bbox.model_copy(update={"right": 630})})
        if c.text == LITERAL and c.bbox is not None
        else c
        for c in data.table_cells
    ]
    data = data.model_copy(update={"table_cells": widened})
    doc = DlDocument.model_validate(
        {
            "tables": [table(0, 1, INCOME_1), table(1, 2, tail)],
            "texts": [],
            "pages": {
                str(n): {"page_no": n, "size": {"width": 800, "height": 1000}} for n in (1, 2)
            },
        }
    )
    doc = doc.model_copy(
        update={"tables": [doc.tables[0], doc.tables[1].model_copy(update={"data": data})]}
    )
    base = inputs([("docling/p1-2.json", doc)])
    # Locate places the income statement on pages 1 and 2, so the tail may continue it.
    located = base.located.model_copy(
        update={
            "ranges": [span(StatementType.INCOME, 1, 2)],
            "pages": [page(1, StatementType.INCOME), page(2)],
        }
    )
    return base.model_copy(
        update={
            "located": located,
            "page_texts": {1: "Statement of profit or loss"},
            "title_types": {
                1: (StatementType.INCOME,),
                2: (StatementType.INCOME,) if titled else (),
            },
            "cue_types": {2: (StatementType.INCOME,)},
            "page_modes": {1: PageMode.TEXT, 2: mode},
            "visual_pages": set(),
        }
    )


def _tail_amounts(result: StructureResult) -> list[tuple[str, list[Decimal | None]]]:
    income = [s for s in result.statements if s.type is StatementType.INCOME]
    assert len(income) == 1
    return [
        (i.raw_label, [c.reported for c in i.cells])
        for i in income[0].line_items
        if i.raw_label in INCOME_MERGED
    ]


SPLIT = [Decimal("2505304778"), Decimal("1345663827")]


def test_a_low_confidence_ocr_tail_whose_merged_rows_skew_its_centre_still_continues() -> None:
    result, _ = structure_document(_income_tail(INCOME_HEADER, PageMode.IMAGE), IngestConfig())
    income = [s for s in result.statements if s.type is StatementType.INCOME]
    assert len(income) == 1 and income[0].source_pages == [1, 2]
    assert _tail_amounts(result) == [(label, SPLIT) for label in INCOME_MERGED]


def test_a_digital_tail_is_not_split_and_its_skewed_centre_is_unchanged() -> None:
    result, _ = structure_document(_income_tail(INCOME_HEADER, PageMode.TEXT), IngestConfig())
    income = [s for s in result.statements if s.type is StatementType.INCOME]
    assert len(income) == 1 and income[0].source_pages == [1]
    assert not any(t.statement_id for t in result.tables if t.table_ref == "#/tables/1")


def test_a_table_inheriting_its_right_period_is_bound_and_split() -> None:
    undated = ["", "Notes", "2025 SAR '000", ""]
    supplied = _income_tail(undated, PageMode.IMAGE)
    result, _ = structure_document(supplied, IngestConfig())
    income = [s for s in result.statements if s.type is StatementType.INCOME]
    assert len(income) == 1 and income[0].source_pages == [1, 2]
    tail = [d for d in result.tables if d.page_no == 2]
    assert any("continuation_of:income" in e for d in tail for e in d.evidence)
    assert "inherited:3:FY2024" in income[0].flags
    assert _tail_amounts(result) == [(label, SPLIT) for label in INCOME_MERGED]
