"""Grid rows to line items with provenance (spec 11, Data flow step 7)."""

from decimal import Decimal

from fra_core.schemas import BBox, StatementType, TextSource
from fra_ingest.classify import Classification
from fra_ingest.header import parse_header
from fra_ingest.parts import PartialStatement, build_part, column_centre
from fra_ingest.table_grid import Grid, GridCell


def box(col: int, row: int, left_pad: float = 0) -> BBox:
    return BBox(
        left=100 * col + 10 + left_pad, top=20 * row + 5, right=100 * col + 90, bottom=20 * row + 15
    )


def grid(rows: list[list[str]], pads: dict[int, float] | None = None) -> Grid:
    cells = tuple(
        GridCell(
            text=t,
            row=r,
            col=c,
            bbox=box(c, r, (pads or {}).get(r, 0) if c == 0 else 0),
            page_no=156,
            is_column_header=r == 0,
        )
        for r, row in enumerate(rows)
        for c, t in enumerate(row)
        if t
    )
    return Grid(
        table_ref="#/tables/0",
        docling_path="docling/p155-164.json",
        page_no=156,
        page_width=800,
        num_rows=len(rows),
        num_cols=4,
        cells=cells,
    )


ROWS = [
    ["", "Notes", "31 December 2025 X '000", "31 December 2024 X '000"],
    ["Non-Current Assets", "", "", ""],
    ["Property, Plant and Equipment", "7", "26,058,632", "22,750,342"],
    ["Investments", "12", "-", "3,256"],
    ["Biological Assets", "11", "", "1,838,353"],
    ["Basic earnings per share", "", "2.10", "1.90"],
]


def part() -> PartialStatement:
    g = grid(ROWS, pads={2: 10, 3: 10, 4: 10})
    layout = parse_header(g, StatementType.BALANCE, None)
    classification = Classification(type=StatementType.BALANCE, confidence=0.8)
    return build_part(g, layout, classification, source=TextSource.TEXT)


def test_line_items_carry_values_notes_and_provenance() -> None:
    p = part()
    assert [i.raw_label for i in p.line_items] == [
        "Non-Current Assets",
        "Property, Plant and Equipment",
        "Investments",
        "Biological Assets",
        "Basic earnings per share",
    ]
    ppe = p.line_items[1]
    assert ppe.id == "p156-t0-r2" and ppe.note_ref == "7"
    cell = ppe.cells[0]
    assert (cell.period_key, cell.reported, cell.raw_text) == (
        "2025-12-31",
        Decimal("26058632"),
        "26,058,632",
    )
    assert (
        cell.provenance.page_no,
        cell.provenance.table_ref,
        cell.provenance.row,
        cell.provenance.col,
    ) == (156, "#/tables/0", 2, 2)
    assert cell.provenance.source is TextSource.TEXT
    assert p.line_items[0].cells == []


def test_a_dash_is_zero_and_an_empty_cell_is_missing() -> None:
    p = part()
    investments = p.line_items[2].cells
    assert investments[0].reported == Decimal("0")
    biological = p.line_items[3].cells
    assert biological[0].reported is None and "numbers_missing" in biological[0].flags
    assert biological[0].provenance.bbox.left == 210


def test_per_share_rows_are_marked() -> None:
    eps = part().line_items[4]
    assert all("per_share" in c.flags for c in eps.cells)


def test_periods_centres_and_indents() -> None:
    p = part()
    assert [x.key for x in p.periods] == ["2025-12-31", "2024-12-31"]
    assert p.column_centres == {"2025-12-31": 250.0, "2024-12-31": 350.0}
    assert p.indents["p156-t0-r2"] == 20.0 and p.indents["p156-t0-r1"] == 10.0
    assert (p.first_page, p.last_page, p.table_refs) == (
        156,
        156,
        ["docling/p155-164.json#/tables/0"],
    )


def test_column_centre_is_the_mean_of_its_cells() -> None:
    g = grid(ROWS)
    assert column_centre(g, 2, [2, 3]) == 250.0
    assert column_centre(g, 2, [1]) is None
