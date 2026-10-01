"""The extraction eval's arithmetic (spec 12, Scoring)."""

from datetime import date
from decimal import Decimal

from harness.expected import ExpectedFile, ExpectedRow, ExpectedStatement
from harness.extraction import align_rows, report, score_statement

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

P = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
OTHER = Period(key="2024-12-31", end_date=date(2024, 12, 31), kind=PeriodKind.INSTANT)
BOX = BBox(left=1, top=1, right=2, bottom=2)
SHA = "a" * 64


def row(label: str, value: str, unconfirmed: bool = False) -> ExpectedRow:
    return ExpectedRow(
        label=label, values={P.key: Decimal(value)}, unconfirmed=[P.key] if unconfirmed else []
    )


def item(n: int, label: str, value: str | None, period: Period = P) -> LineItem:
    cells = (
        []
        if value is None
        else [
            Cell(
                period_key=period.key,
                reported=Decimal(value),
                raw_text=value,
                provenance=Provenance(page_no=1, bbox=BOX, table_ref="#/tables/0", row=n, col=1),
            )
        ]
    )
    return LineItem(id=f"r{n}", raw_label=label, cells=cells)


def statement_of(rows: list[ExpectedRow]) -> ExpectedStatement:
    return ExpectedStatement(
        type=StatementType.BALANCE,
        pages=[1],
        page_mode="digital",
        scale=1000,
        currency="SAR",
        periods=[P],
        rows=rows,
    )


def extracted_of(items: list[LineItem], period: Period = P) -> Statement:
    return Statement(
        id="s",
        document_sha256=SHA,
        type=StatementType.BALANCE,
        currency="SAR",
        scale=1000,
        periods=[period],
        line_items=items,
    )


def file_of(statement: ExpectedStatement, status: str) -> ExpectedFile:
    return ExpectedFile.model_validate(
        {"id": "doc", "sha256": SHA, "status": status, "statements": [statement]}
    )


def test_rows_align_by_label_or_by_a_shared_value() -> None:
    expected = [row("Inventories", "10"), row("Cash", "5"), row("Total", "15")]
    items = [
        item(1, "lnventories", "10"),
        item(2, "Notes heading", None),
        item(3, "Cash", "6"),
        item(4, "Total", "15"),
    ]
    assert align_rows(expected, items) == [0, 2, 3]


def test_a_misread_a_missing_row_and_an_extra_row_are_scored() -> None:
    expected = statement_of(
        [row("Inventories", "10"), row("Cash", "5"), row("Receivables", "7"), row("Total", "22")]
    )
    extracted = extracted_of(
        [
            item(1, "Inventories", "10"),
            item(2, "Cash", "6"),
            item(3, "Other", "99"),
            item(4, "Total", "22"),
        ]
    )
    score = score_statement(expected, extracted)
    assert (score.cells, score.right, score.extra_rows) == (4, 2, 1)


def test_a_wrong_sign_counts_against_sign_accuracy_and_cell_accuracy() -> None:
    score = score_statement(
        statement_of([row("Cost", "-60")]), extracted_of([item(1, "Cost", "60")])
    )
    assert (score.cells, score.right, score.sign_cells, score.sign_right) == (1, 0, 1, 0)


def test_periods_are_scored_by_key_date_kind_and_length() -> None:
    score = score_statement(
        statement_of([row("Cash", "5")]), extracted_of([item(1, "Cash", "5", OTHER)], OTHER)
    )
    assert (score.periods, score.periods_right) == (1, 0)
    same = score_statement(statement_of([row("Cash", "5")]), extracted_of([item(1, "Cash", "5")]))
    assert (same.periods, same.periods_right) == (1, 1)


def test_scale_and_currency_are_scored() -> None:
    wrong = extracted_of([item(1, "Cash", "5")]).model_copy(update={"scale": 1})
    score = score_statement(statement_of([row("Cash", "5")]), wrong)
    assert (score.metadata, score.metadata_right) == (2, 1)


def test_unconfirmed_cells_are_counted_apart() -> None:
    score = score_statement(
        statement_of([row("Cash", "5", unconfirmed=True), row("Total", "5")]),
        extracted_of([item(1, "Cash", "9"), item(2, "Total", "5")]),
    )
    assert (score.cells, score.right, score.unconfirmed) == (1, 1, 1)


def test_a_statement_that_was_not_extracted_misses_every_cell() -> None:
    score = score_statement(statement_of([row("Cash", "5"), row("Total", "5")]), None)
    assert (score.cells, score.right, score.periods, score.periods_right) == (2, 0, 1, 0)


def test_drafts_stay_out_of_the_gate_and_a_checked_miss_fails_it() -> None:
    wrong = extracted_of([item(1, "Cash", "9"), item(2, "Total", "5")])
    expected = statement_of([row("Cash", "5"), row("Total", "5")])
    lines, code = report([(file_of(expected, "draft"), {StatementType.BALANCE: wrong})])
    text = "\n".join(lines)
    assert code == 0 and "G1 not measured" in text and "provisional" in text
    assert "cells 2  right 1" in text
    lines, code = report([(file_of(expected, "checked"), {StatementType.BALANCE: wrong})])
    assert code == 1
    assert "digital  cells 2  right 1   50.00%  below 99.50%" in "\n".join(lines)


def test_a_checked_file_that_meets_every_threshold_passes() -> None:
    right = extracted_of([item(1, "Cash", "5"), item(2, "Total", "5")])
    expected = statement_of([row("Cash", "5"), row("Total", "5")])
    lines, code = report([(file_of(expected, "checked"), {StatementType.BALANCE: right})])
    assert code == 0 and "digital  cells 2  right 2  100.00%  meets 99.50%" in "\n".join(lines)
