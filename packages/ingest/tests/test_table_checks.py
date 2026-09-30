"""Subtotal checks and the balance sheet identity (spec 11, Data flow step 10)."""

from datetime import date
from decimal import Decimal

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
from fra_core.taxonomy.loader import load_taxonomy
from fra_ingest.label_match import LabelIndex
from fra_ingest.table_checks import check_identity, check_subtotals, run_checks

INDEX = LabelIndex(load_taxonomy())
P = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
BOX = BBox(left=1, top=1, right=2, bottom=2)


def item(n: int, label: str, value: str | None, subtotal: bool = False) -> LineItem:
    cells = (
        []
        if value is None
        else [
            Cell(
                period_key=P.key,
                reported=Decimal(value) if value != "?" else None,
                raw_text=value,
                provenance=Provenance(page_no=1, bbox=BOX, table_ref="#/tables/0", row=n, col=2),
            )
        ]
    )
    return LineItem(id=f"r{n}", raw_label=label, is_subtotal=subtotal, cells=cells)


def statement(items: list[LineItem], kind: StatementType = StatementType.BALANCE) -> Statement:
    return Statement(
        id="s",
        document_sha256="a" * 64,
        type=kind,
        currency="SAR",
        scale=1000,
        periods=[P],
        line_items=items,
    )


def test_a_subtotal_over_its_rows_passes_within_the_d6_tolerance() -> None:
    checks = check_subtotals(
        statement(
            [
                item(1, "Current assets", None),
                item(2, "Inventories", "10"),
                item(3, "Cash", "5"),
                item(4, "Total current assets", "16", subtotal=True),
            ]
        )
    )
    assert [c.status for c in checks] == ["pass"]
    assert checks[0].tolerance == Decimal("1.0") and checks[0].difference == Decimal("1")


def test_a_subtotal_that_misses_fails() -> None:
    checks = check_subtotals(
        statement(
            [
                item(1, "Inventories", "10"),
                item(2, "Cash", "5"),
                item(3, "Total current assets", "20", subtotal=True),
            ]
        )
    )
    assert checks[0].status == "fail" and checks[0].expected == Decimal("15")


def test_a_running_total_counts_the_previous_subtotal() -> None:
    income = statement(
        [
            item(1, "Revenue", "100"),
            item(2, "Cost of sales", "-60"),
            item(3, "Gross profit", "40", subtotal=True),
            item(4, "Selling", "-10"),
            item(5, "Admin", "-5"),
            item(6, "Operating profit", "25", subtotal=True),
        ],
        StatementType.INCOME,
    )
    assert [c.status for c in check_subtotals(income)] == ["pass", "pass"]


def test_a_subtotal_over_subtotals_is_skipped() -> None:
    checks = check_subtotals(
        statement(
            [
                item(1, "Inventories", "10"),
                item(2, "Cash", "5"),
                item(3, "Total current assets", "15", subtotal=True),
                item(4, "Total assets", "15", subtotal=True),
            ]
        )
    )
    assert [c.status for c in checks] == ["pass", "skipped"]
    assert checks[1].detail == "subtotal_scope_unknown"


def test_identity_through_a_printed_liabilities_and_equity_total() -> None:
    s = statement(
        [
            item(1, "Total assets", "100", True),
            item(2, "Total liabilities", "60", True),
            item(3, "Total equity", "40", True),
            item(4, "Total liabilities and equity", "100", True),
        ]
    )
    checks = check_identity(s, INDEX)
    assert [c.status for c in checks] == ["pass"]


def test_identity_through_the_two_totals_and_its_failure() -> None:
    s = statement(
        [
            item(1, "Total assets", "100", True),
            item(2, "Total liabilities", "60", True),
            item(3, "Total equity", "30", True),
        ]
    )
    checks = check_identity(s, INDEX)
    assert checks[0].status == "fail" and checks[0].difference == Decimal("10")


def test_identity_is_skipped_when_totals_are_missing() -> None:
    s = statement([item(1, "Total assets", "100", True)])
    checks = check_identity(s, INDEX)
    assert checks[0].status == "skipped" and checks[0].detail == "identity_totals_not_found"


def test_run_checks_flags_the_statement() -> None:
    s = statement(
        [
            item(1, "Inventories", "10"),
            item(2, "Cash", "5"),
            item(3, "Total current assets", "20", True),
            item(4, "Total assets", "100", True),
        ]
    )
    checked, results = run_checks(s, INDEX)
    assert "subtotal_failed" in checked.flags and "identity_totals_not_found" in checked.flags
    assert results
