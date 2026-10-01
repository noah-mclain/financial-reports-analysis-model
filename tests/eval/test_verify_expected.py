"""Independent evidence for each figure of an expected file (spec 12, Expected files)."""

from datetime import date
from decimal import Decimal

from harness.expected import ExpectedRow, ExpectedStatement
from harness.verify_expected import arithmetic_cells, reread_cells, sibling_cells, unvouched

from fra_core.schemas import (
    BBox,
    Cell,
    LineItem,
    Period,
    PeriodKind,
    Provenance,
    Statement,
    StatementType,
)

P1 = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
P2 = Period(key="2024-12-31", end_date=date(2024, 12, 31), kind=PeriodKind.INSTANT)
BOX = BBox(left=10, top=10, right=20, bottom=20)


def row(label: str, first: str | None, second: str | None = None) -> ExpectedRow:
    values = {P1.key: Decimal(first) if first is not None else None}
    if second is not None:
        values[P2.key] = Decimal(second)
    return ExpectedRow(label=label, values=values)


def expected(rows: list[ExpectedRow]) -> ExpectedStatement:
    return ExpectedStatement(
        type=StatementType.BALANCE,
        pages=[5],
        page_mode="scanned",
        scale=1,
        currency="EGP",
        periods=[P1, P2],
        rows=rows,
    )


def extracted(rows: list[tuple[str, str, str]], scale: int = 1) -> Statement:
    items = [
        LineItem(
            id=f"r{n}",
            raw_label=label,
            cells=[
                Cell(
                    period_key=period.key,
                    reported=Decimal(value),
                    raw_text=value,
                    provenance=Provenance(
                        page_no=5, bbox=BOX, table_ref="#/tables/0", row=n, col=c
                    ),
                )
                for c, (period, value) in enumerate(((P1, first), (P2, second)), start=1)
            ],
        )
        for n, (label, first, second) in enumerate(rows)
    ]
    return Statement(
        id="s",
        document_sha256="a" * 64,
        type=StatementType.BALANCE,
        currency="EGP",
        scale=scale,
        periods=[P1, P2],
        line_items=items,
        source_pages=[5],
    )


ROWS = [
    row("Property", "100", "90"),
    row("Goodwill", "20", "20"),
    row("Total non-current assets", "120", "115"),
    row("Earnings per share", "2.18", "1.09"),
]


def test_figures_in_a_sum_that_holds_are_vouched_for_by_arithmetic() -> None:
    cells = arithmetic_cells(expected(ROWS))
    assert cells == {(0, P1.key), (1, P1.key), (2, P1.key)}


def test_a_total_equal_to_one_row_vouches_for_nothing() -> None:
    rows = [row("Borrowings", "70", "60"), row("Total liabilities", "70", "60")]
    assert arithmetic_cells(expected(rows)) == set()


def test_a_figure_found_in_another_filing_for_the_same_period_is_vouched_for() -> None:
    other = extracted(
        [("Fixed assets", "100000", "90000"), ("Something else", "7", "20000")], scale=1
    )
    thousands = expected(ROWS).model_copy(update={"scale": 1000})
    cells = sibling_cells(thousands, [other])
    assert cells == {(0, P1.key), (0, P2.key), (1, P2.key)}


def test_a_sibling_statement_of_another_type_or_a_zero_vouches_for_nothing() -> None:
    income = extracted([("Revenue", "100", "90")]).model_copy(update={"type": StatementType.INCOME})
    assert sibling_cells(expected(ROWS), [income]) == set()
    zeros = extracted([("Investments", "0", "0")])
    assert sibling_cells(expected([row("Investments", "0", "0")]), [zeros]) == set()


def test_a_cell_read_again_from_its_box_is_vouched_for_when_it_agrees() -> None:
    statement = extracted([("Property", "100", "95"), ("Goodwill", "20", "20")])
    answers = {(0, 1): "100", (0, 2): "90", (1, 1): "20", (1, 2): "garbled"}

    def read(page_no: int, box: BBox, row_no: int, col: int) -> str:
        return answers[(row_no, col)]

    cells = reread_cells(
        expected(ROWS[:2]),
        statement,
        lambda cell: read(
            cell.provenance.page_no, cell.provenance.bbox, cell.provenance.row, cell.provenance.col
        ),
    )
    # Row 0, second period: the extraction read 95, the page comparison corrected it to 90,
    # and the second read gives 90 too.
    assert cells == {(0, P1.key), (0, P2.key), (1, P1.key)}


def test_what_no_evidence_vouches_for_is_listed() -> None:
    statement = expected(ROWS)
    vouched = arithmetic_cells(statement) | {(3, P1.key)}
    assert unvouched(statement, vouched) == [(0, P2.key), (1, P2.key), (2, P2.key), (3, P2.key)]
