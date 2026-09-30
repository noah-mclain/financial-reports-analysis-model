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


def statement(
    items: list[LineItem],
    kind: StatementType = StatementType.BALANCE,
    periods: list[Period] | None = None,
) -> Statement:
    return Statement(
        id="s",
        document_sha256="a" * 64,
        type=kind,
        currency="SAR",
        scale=1000,
        periods=periods or [P],
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


def test_a_plain_row_equal_to_the_rows_before_it_is_an_implicit_subtotal() -> None:
    checks = check_subtotals(
        statement(
            [
                item(1, "Equity and liabilities", None),
                item(2, "Share capital", "10"),
                item(3, "Retained earnings", "6"),
                item(4, "Equity attributable to owners", "16"),
                item(5, "Non-controlling interest", "2"),
                item(6, "Total equity", "18", subtotal=True),
            ]
        )
    )
    assert [(c.status, c.detail) for c in checks] == [("pass", "implicit_subtotal"), ("pass", "")]
    assert checks[0].line_item_ids == ["r2", "r3", "r4"]
    assert checks[1].line_item_ids == ["r4", "r5", "r6"]


def test_a_single_matching_row_is_not_an_implicit_subtotal() -> None:
    checks = check_subtotals(
        statement(
            [
                item(1, "Cash", "5"),
                item(2, "Deposits", "5"),
                item(3, "Other", "7"),
                item(4, "Total", "17", subtotal=True),
            ]
        )
    )
    assert [c.status for c in checks] == ["pass"]
    assert checks[0].line_item_ids == ["r1", "r2", "r3", "r4"]


def test_a_heading_keeps_the_running_total() -> None:
    income = statement(
        [
            item(1, "Revenue", "100"),
            item(2, "Cost of sales", "-60"),
            item(3, "Gross profit", "40", subtotal=True),
            item(4, "Operating expenses", None),
            item(5, "Selling", "-10"),
            item(6, "Admin", "-5"),
            item(7, "Operating profit", "25", subtotal=True),
        ],
        StatementType.INCOME,
    )
    checks = check_subtotals(income)
    assert [c.status for c in checks] == ["pass", "pass"]
    assert checks[1].line_item_ids == ["r3", "r5", "r6", "r7"]


def test_a_subtotal_after_a_heading_that_misses_both_candidates_is_uncertain() -> None:
    checks = check_subtotals(
        statement(
            [
                item(1, "Profit", "100"),
                item(2, "Items that may be reclassified", None),
                item(3, "Translation", "5"),
                item(4, "Hedges", "7"),
                item(5, "Total comprehensive income", "112", subtotal=True),
            ],
            StatementType.COMPREHENSIVE_INCOME,
        )
    )
    assert [(c.status, c.detail) for c in checks] == [("skipped", "subtotal_scope_uncertain")]
    assert checks[0].expected == Decimal("12") and checks[0].actual == Decimal("112")


def test_a_clearly_wrong_subtotal_at_the_start_still_fails() -> None:
    checks = check_subtotals(
        statement(
            [
                item(1, "Inventories", "10"),
                item(2, "Cash", "5"),
                item(3, "Total current assets", "40", subtotal=True),
            ]
        )
    )
    assert [(c.status, c.detail) for c in checks] == [("fail", "")]


def test_an_implicit_subtotal_of_the_last_rows_replaces_them_in_the_run() -> None:
    checks = check_subtotals(
        statement(
            [
                item(1, "Profit for the year", "100"),
                item(2, "Actuarial loss", "-3"),
                item(3, "Translation", "5"),
                item(4, "Hedges", "7"),
                item(5, "Other comprehensive income", "9"),
                item(6, "Total comprehensive income", "109", subtotal=True),
            ],
            StatementType.COMPREHENSIVE_INCOME,
        )
    )
    assert [(c.status, c.detail) for c in checks] == [("pass", "implicit_subtotal"), ("pass", "")]
    assert checks[0].line_item_ids == ["r2", "r3", "r4", "r5"]
    assert checks[1].line_item_ids == ["r1", "r5", "r6"]


def test_a_row_equal_to_no_sum_of_the_last_rows_stays_plain() -> None:
    checks = check_subtotals(
        statement(
            [
                item(1, "Inventories", "10"),
                item(2, "Receivables", "4"),
                item(3, "Cash", "5"),
                item(4, "Total current assets", "19", subtotal=True),
            ]
        )
    )
    assert [(c.status, c.detail) for c in checks] == [("pass", "")]
    assert checks[0].line_item_ids == ["r1", "r2", "r3", "r4"]


def test_a_wrong_total_right_under_its_heading_fails() -> None:
    checks = check_subtotals(
        statement(
            [
                item(1, "Current assets", None),
                item(2, "Inventories", "10"),
                item(3, "Cash", "5"),
                item(4, "Total current assets", "20015", subtotal=True),
            ]
        )
    )
    assert [(c.status, c.detail) for c in checks] == [("fail", "")]


def test_a_missing_value_under_a_heading_is_skipped_as_missing() -> None:
    checks = check_subtotals(
        statement(
            [
                item(1, "Current assets", None),
                item(2, "Inventories", "10"),
                item(3, "Cash", "?"),
                item(4, "Total current assets", "15", subtotal=True),
            ]
        )
    )
    assert [(c.status, c.detail) for c in checks] == [("skipped", "missing_values")]


def test_each_period_under_a_heading_is_judged_on_its_own() -> None:
    prior = Period(key="2024-12-31", end_date=date(2024, 12, 31), kind=PeriodKind.INSTANT)

    def two(n: int, label: str, values: tuple[str, str] | None, subtotal: bool = False) -> LineItem:
        base = item(n, label, None, subtotal)
        if values is None:
            return base
        cells = [
            item(n, label, v).cells[0].model_copy(update={"period_key": key})
            for key, v in zip((P.key, prior.key), values, strict=True)
        ]
        return base.model_copy(update={"cells": cells})

    checks = check_subtotals(
        statement(
            [
                two(1, "Current assets", None),
                two(2, "Inventories", ("10", "8")),
                two(3, "Cash", ("5", "4")),
                two(4, "Total current assets", ("15", "20"), subtotal=True),
            ],
            periods=[P, prior],
        )
    )
    assert [(c.period_key, c.status) for c in checks] == [(P.key, "pass"), (prior.key, "fail")]
