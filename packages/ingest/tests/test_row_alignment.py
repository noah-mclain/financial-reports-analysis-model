"""Values put back on their label's line by geometry (spec 12, Data flow step 6a)."""

from fra_core.schemas import BBox, StatementType
from fra_ingest.header import HeaderLayout, parse_header
from fra_ingest.row_alignment import realign_rows
from fra_ingest.table_grid import Grid, GridCell

LINE = 12.0  # one printed line; value boxes are 8 high


def cell(text: str, row: int, col: int, line: float, lines: float = 1) -> GridCell:
    """A cell whose box starts at printed line ``line``; a label may span several lines."""
    top = 100 + LINE * line
    height = LINE * lines - 2 if col == 0 else 8
    return GridCell(
        text=text,
        row=row,
        col=col,
        bbox=BBox(left=100 * col + 10, top=top, right=100 * col + 90, bottom=top + height),
        page_no=5,
    )


def grid(cells: list[GridCell], rows: int) -> Grid:
    header = [
        GridCell(
            text=t,
            row=0,
            col=c,
            bbox=BBox(left=100 * c + 10, top=80, right=100 * c + 90, bottom=90),
            page_no=5,
            is_column_header=True,
        )
        for c, t in ((1, "31 December 2024"), (2, "31 December 2023"))
    ]
    return Grid(
        table_ref="#/tables/0",
        docling_path="docling/p4-9.json",
        page_no=5,
        page_width=600,
        num_rows=rows,
        num_cols=3,
        cells=tuple([*header, *cells]),
    )


def aligned() -> Grid:
    return grid(
        [
            cell("Share capital", 1, 0, 0),
            cell("140", 1, 1, 0),
            cell("140", 1, 2, 0),
            cell("Retained earnings", 2, 0, 1),
            cell("4,085", 2, 1, 1),
            cell("3,244", 2, 2, 1),
            cell("Total equity", 3, 0, 2),
            cell("4,225", 3, 1, 2),
            cell("3,384", 3, 2, 2),
        ],
        rows=4,
    )


def layout(g: Grid) -> HeaderLayout:
    return parse_header(g, StatementType.BALANCE, None)


def test_an_aligned_grid_is_returned_unchanged() -> None:
    g = aligned()
    result = realign_rows(g, layout(g))
    assert result.grid == g and not result.moved and not result.unresolved


def shifted() -> Grid:
    """Edita 2024 AR, equity: the first label cell holds two printed lines, the values of the
    third line sit on an unlabelled row, and the third label holds the second line's values."""
    return grid(
        [
            cell("Equity attributable Non-controlling interests", 1, 0, 0, lines=2),
            cell("4,055", 1, 1, 0),
            cell("3,373", 1, 2, 0),
            cell("4,157", 2, 1, 2),
            cell("3,447", 2, 2, 2),
            cell("Total equity", 3, 0, 2),
            cell("102", 3, 1, 1),
            cell("74", 3, 2, 1),
        ],
        rows=4,
    )


def test_displaced_groups_move_to_the_label_line_that_contains_them() -> None:
    g = shifted()
    result = realign_rows(g, layout(g))
    fixed = result.grid
    assert [fixed.text(3, 1), fixed.text(3, 2)] == ["4,157", "3,447"]
    assert [fixed.text(2, 1), fixed.text(2, 2)] == ["102", "74"]
    assert [fixed.text(1, 1), fixed.text(1, 2)] == ["4,055", "3,373"]
    moved = fixed.cell(3, 1)
    assert moved is not None and moved.source_row == 2 and "row_realigned" in moved.flags
    assert sorted(result.moved) == [(1, 2, 3), (1, 3, 2), (2, 2, 3), (2, 3, 2)]
    assert result.merged_labels == [1] and not result.unresolved


def test_a_heading_cell_gives_its_values_to_the_label_below() -> None:
    g = grid(
        [
            cell("Liabilities Non-current liabilities", 1, 0, 0, lines=2),
            cell("2,282", 1, 1, 2),
            cell("1,129", 1, 2, 2),
            cell("Borrowings", 2, 0, 2),
            cell("19", 2, 1, 3),
            cell("17", 2, 2, 3),
            cell("Government grants", 3, 0, 3),
            cell("Employee benefits", 4, 0, 4),
            cell("75", 4, 1, 4),
            cell("55", 4, 2, 4),
        ],
        rows=5,
    )
    fixed = realign_rows(g, layout(g)).grid
    assert fixed.text(1, 1) == "" and fixed.text(2, 1) == "2,282" and fixed.text(3, 1) == "19"
    assert fixed.text(4, 1) == "75"


def test_a_wrapped_label_keeps_its_values_on_either_line() -> None:
    for value_line in (0, 1):
        g = grid(
            [
                cell("Financial assets at fair value through profit", 1, 0, 0, lines=2),
                cell("578", 1, 1, value_line),
                cell("0", 1, 2, value_line),
                cell("Cash", 2, 0, 2),
                cell("716", 2, 1, 2),
                cell("518", 2, 2, 2),
            ],
            rows=3,
        )
        result = realign_rows(g, layout(g))
        assert not result.moved and not result.unresolved and result.merged_labels == [1]


def test_a_group_on_no_label_line_is_left_and_flagged() -> None:
    g = grid(
        [
            cell("Share capital", 1, 0, 0),
            cell("140", 1, 1, 0),
            cell("140", 1, 2, 0),
            cell("Reserves", 2, 0, 1),
            cell("72", 2, 1, 5),
            cell("72", 2, 2, 5),
        ],
        rows=3,
    )
    result = realign_rows(g, layout(g))
    assert result.unresolved == [2] and not result.moved
    left = result.grid.cell(2, 1)
    assert left is not None and left.text == "72" and "row_misaligned" in left.flags


def test_a_mirrored_table_is_repaired_the_same_way() -> None:
    g = shifted()
    mirrored = g.model_copy(
        update={
            "cells": tuple(
                c.model_copy(
                    update={
                        "col": 2 - c.col,
                        "bbox": BBox(
                            left=500 - c.bbox.right,
                            top=c.bbox.top,
                            right=500 - c.bbox.left,
                            bottom=c.bbox.bottom,
                        ),
                    }
                )
                for c in g.cells
                if c.bbox is not None
            )
        }
    )
    fixed = realign_rows(mirrored, layout(mirrored)).grid
    assert [fixed.text(3, 0), fixed.text(3, 1)] == ["3,447", "4,157"]


def test_a_group_whose_label_line_is_taken_is_left_and_flagged() -> None:
    # Row 2's values sit on row 3's line, and row 3's own values sit above them: moving row 2
    # down would leave row 3 holding two groups, and nothing below is free.
    g = grid(
        [
            cell("Share capital", 1, 0, 0),
            cell("140", 1, 1, 0),
            cell("140", 1, 2, 0),
            cell("Reserves", 2, 0, 1),
            cell("72", 2, 1, 2),
            cell("70", 2, 2, 2),
            cell("Retained earnings", 3, 0, 2),
            cell("4,085", 3, 1, 2),
            cell("3,244", 3, 2, 2),
        ],
        rows=4,
    )
    result = realign_rows(g, layout(g))
    assert not result.moved and result.unresolved == [2]
    assert result.grid.text(2, 1) == "72" and result.grid.text(3, 1) == "4,085"


def test_a_shifted_table_with_a_value_that_has_no_box_is_flagged_not_repaired() -> None:
    cells = [c for c in shifted().cells if (c.row, c.col, c.text) != (2, 1, "4,157")]
    cells.append(GridCell(text="999", row=2, col=1, bbox=None, page_no=5))
    g = shifted().model_copy(update={"cells": tuple(cells)})
    result = realign_rows(g, layout(g))
    assert not result.moved and result.unresolved == [3]
    assert result.grid.text(3, 1) == "102" and result.grid.text(2, 1) == "999"
    off = result.grid.cell(3, 1)
    assert off is not None and "row_misaligned" in off.flags


def test_a_repair_that_would_put_figures_out_of_order_is_refused() -> None:
    # The grid lists the two labels in the reverse of their order on the page, each with the
    # other's values. Swapping them would leave the rows out of page order, so nothing moves.
    g = grid(
        [
            cell("Retained earnings", 1, 0, 2),
            cell("140", 1, 1, 0),
            cell("140", 1, 2, 0),
            cell("Share capital", 2, 0, 0),
            cell("4,085", 2, 1, 2),
            cell("3,244", 2, 2, 2),
        ],
        rows=3,
    )
    result = realign_rows(g, layout(g))
    assert not result.moved and result.unresolved == [1, 2]
    assert result.grid.text(1, 1) == "140" and result.grid.text(2, 1) == "4,085"
    flagged = [c for c in result.grid.cells if "row_misaligned" in c.flags]
    assert len(flagged) == 4
