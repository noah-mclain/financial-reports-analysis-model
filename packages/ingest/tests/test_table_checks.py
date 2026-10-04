"""Subtotal checks and the balance sheet identity (spec 11, Data flow step 10)."""

from datetime import date
from decimal import Decimal, getcontext, localcontext

import pytest

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
from fra_ingest import check_tolerance
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


def test_a_total_over_one_total_alone_is_skipped() -> None:
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


def comprehensive(total: str) -> Statement:
    return statement(
        [
            item(1, "Profit", "100"),
            item(2, "Items that may be reclassified", None),
            item(3, "Translation", "5"),
            item(4, "Hedges", "7"),
            item(5, "Total comprehensive income", total, subtotal=True),
        ],
        StatementType.COMPREHENSIVE_INCOME,
    )


def test_a_subtotal_that_covers_a_row_above_its_heading_is_found_by_the_sums() -> None:
    checks = check_subtotals(comprehensive("112"))
    assert [(c.status, c.detail) for c in checks] == [("pass", "sum_based")]
    assert checks[0].line_item_ids == ["r1", "r3", "r4", "r5"]


def test_a_subtotal_after_a_heading_that_no_sum_explains_is_uncertain() -> None:
    checks = check_subtotals(comprehensive("150"))
    assert [(c.status, c.detail) for c in checks] == [("skipped", "subtotal_scope_uncertain")]
    assert checks[0].expected == Decimal("12") and checks[0].actual == Decimal("150")


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


def test_a_zero_row_does_not_make_the_next_row_an_implicit_subtotal() -> None:
    checks = check_subtotals(
        statement(
            [
                item(1, "Current assets", None),
                item(2, "Inventories", "500"),
                item(3, "Derivatives", "0"),
                item(4, "Trade receivables", "500"),
                item(5, "Total current assets", "1000", subtotal=True),
            ]
        )
    )
    assert [(c.status, c.detail) for c in checks] == [("pass", "")]
    assert checks[0].line_item_ids == ["r2", "r3", "r4", "r5"]


def test_an_implicit_subtotal_may_include_a_zero_row_beside_two_others() -> None:
    checks = check_subtotals(
        statement(
            [
                item(1, "Share capital", "10"),
                item(2, "Treasury shares", "0"),
                item(3, "Retained earnings", "6"),
                item(4, "Equity attributable to owners", "16"),
                item(5, "Non-controlling interest", "2"),
                item(6, "Total equity", "18", subtotal=True),
            ]
        )
    )
    assert [(c.status, c.detail) for c in checks] == [("pass", "implicit_subtotal"), ("pass", "")]


def per_share(n: int, label: str, value: str) -> LineItem:
    plain = item(n, label, value)
    cells = [c.model_copy(update={"flags": ["per_share"]}) for c in plain.cells]
    return plain.model_copy(update={"cells": cells})


def test_per_share_rows_take_no_part_in_subtotal_checks() -> None:
    checks = check_subtotals(
        statement(
            [
                item(1, "Profit attributable to owners", "40"),
                item(2, "Profit attributable to non-controlling interests", "2"),
                per_share(3, "Basic earnings per share", "0.4"),
                per_share(4, "Diluted earnings per share", "0.4"),
                item(5, "Total profit for the year", "42", subtotal=True),
            ],
            StatementType.INCOME,
        )
    )
    assert [(c.status, c.detail) for c in checks] == [("pass", "")]
    assert checks[0].line_item_ids == ["r1", "r2", "r5"]
    assert checks[0].tolerance == Decimal("1.0")


def test_sum_groups_set_parents_implicit_totals_and_confirmed_blanks() -> None:
    rows = [
        item(1, "Share capital", "100"),
        item(2, "Retained earnings", "50"),
        item(3, "Equity attributable to owners", "150"),
        item(4, "Non-controlling interests", "10"),
        item(5, "Total equity", "160", subtotal=True),
    ]
    checked, results = run_checks(statement(rows), INDEX)
    by_id = {i.id: i for i in checked.line_items}
    assert by_id["r1"].parent_id == "r3" and by_id["r3"].parent_id == "r5"
    assert by_id["r3"].is_subtotal
    assert [r.detail for r in results if r.kind == "subtotal"] == ["implicit_subtotal", ""]


def test_a_total_found_by_sums_says_so() -> None:
    rows = [
        item(1, "Non-current assets", None),
        item(2, "Property", "100"),
        item(3, "Goodwill", "20"),
        item(4, "Total non-current assets", "120", subtotal=True),
        item(5, "Current assets", None),
        item(6, "Inventories", "30"),
        item(7, "Cash", "10"),
        item(8, "Total current assets", "40", subtotal=True),
        item(9, "Total assets", "160", subtotal=True),
    ]
    results = check_subtotals(statement(rows))
    assert [(r.status, r.detail) for r in results] == [
        ("pass", ""),
        ("pass", ""),
        ("pass", "sum_based"),
    ]
    assert results[-1].line_item_ids == ["r4", "r8", "r9"]


def test_subtotal_acceptance_and_metadata_use_the_shared_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(check_tolerance, "ROUNDING_UNIT", Decimal("0.25"))
    s = statement([item(1, "Cash", "10"), item(2, "Stock", "5"), item(3, "Total", "15.75", True)])
    result = check_subtotals(s)[0]
    assert result.status == "fail"
    assert result.difference == Decimal("0.75")
    assert result.tolerance == Decimal("0.50")


def test_identity_acceptance_and_metadata_use_the_shared_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(check_tolerance, "ROUNDING_UNIT", Decimal("0.25"))
    s = statement(
        [
            item(1, "Total assets", "100.375", True),
            item(2, "Total liabilities and equity", "100", True),
        ]
    )
    result = check_identity(s, INDEX)[0]
    assert result.status == "fail"
    assert result.difference == Decimal("0.375")
    assert result.tolerance == Decimal("0.25")


@pytest.mark.parametrize("sign", [-1, 1])
@pytest.mark.parametrize("outside", [False, True])
@pytest.mark.parametrize("scale", [1, 1000])
def test_run_fallback_counts_present_zero(sign: int, outside: bool, scale: int) -> None:
    difference = sign * Decimal("1.000001" if outside else "1")
    s = statement(
        [
            item(1, "Cash", "100"),
            item(2, "Other", "0"),
            item(3, "Total", str(Decimal(100) + difference), True),
        ]
    ).model_copy(update={"scale": scale})
    result = check_subtotals(s)[0]
    assert result.status == ("fail" if outside else "pass")
    assert result.expected == Decimal("100")
    assert result.difference == difference and result.tolerance == Decimal("1.0")
    assert result.line_item_ids == ["r1", "r2", "r3"] and result.detail == ""


@pytest.mark.parametrize("sign", [-1, 1])
@pytest.mark.parametrize("outside", [False, True])
@pytest.mark.parametrize("combined", [False, True])
@pytest.mark.parametrize("scale", [1, 1000])
def test_identity_boundary_and_reported_units(
    sign: int, outside: bool, combined: bool, scale: int
) -> None:
    tolerance = Decimal("0.5" if combined else "1.0")
    difference = sign * (tolerance + (Decimal("0.000001") if outside else Decimal(0)))
    rows = [item(1, "Total assets", str(Decimal(100) + difference), True)]
    if combined:
        rows.append(item(2, "Total liabilities and equity", "100", True))
    else:
        rows.extend([item(2, "Total liabilities", "60", True), item(3, "Total equity", "40", True)])
    s = statement(rows).model_copy(update={"scale": scale})
    result = check_identity(s, INDEX)[0]
    assert result.status == ("fail" if outside else "pass")
    assert result.expected == Decimal("100") and result.actual == Decimal(100) + difference
    assert result.difference == difference and result.tolerance == tolerance
    assert result.line_item_ids == [r.id for r in rows]
    assert result.id == "s:balance_identity:balance:2025-12-31"
    assert result.detail == "total assets against total liabilities and equity"


@pytest.mark.parametrize("combined", [False, True])
def test_missing_identity_values_preserve_skip_metadata(combined: bool) -> None:
    rows = [item(1, "Total assets", "100", True)]
    if combined:
        rows.append(item(2, "Total liabilities and equity", "?", True))
    else:
        rows.extend([item(2, "Total liabilities", "60", True), item(3, "Total equity", "?", True)])
    result = check_identity(statement(rows), INDEX)[0]
    assert result.status == "skipped" and result.detail == "missing_values"
    assert result.expected is result.actual is result.difference is result.tolerance is None
    assert result.line_item_ids == [r.id for r in rows]


@pytest.mark.parametrize("difference", ["0", "0.000001", "-0.000001"])
def test_single_addend_equality_is_exact_with_reported_tolerance(difference: str) -> None:
    s = statement(
        [item(1, "Cash", "10"), item(2, "Total", str(Decimal(10) + Decimal(difference)), True)]
    )
    result = check_subtotals(s)[0]
    if Decimal(difference) == 0:
        assert result.status == "pass" and result.detail == "single_addend"
        assert result.expected == result.actual == Decimal("10")
        assert result.difference == Decimal(0) and result.tolerance == Decimal("0.5")
        assert result.line_item_ids == ["r1", "r2"]
    else:
        assert result.status == "skipped" and result.detail == "subtotal_scope_unknown"
        assert result.expected is result.actual is result.difference is result.tolerance is None
        assert result.line_item_ids == ["r2"]


@pytest.mark.parametrize("difference", ["1", "-1", "1.000001", "-1.000001"])
def test_previous_total_plus_one_zero_row_keeps_two_addend_tolerance(difference: str) -> None:
    rows = [
        item(1, "Revenue", "100"),
        item(2, "Cost", "-60"),
        item(3, "Gross profit", "40", True),
        item(4, "Other", "0"),
        item(5, "Operating profit", str(Decimal(40) + Decimal(difference)), True),
    ]
    result = check_subtotals(statement(rows, StatementType.INCOME))[-1]
    if abs(Decimal(difference)) <= 1:
        assert result.status == "pass" and result.detail == "single_addend"
        assert result.expected == Decimal("40") and result.difference == Decimal(difference)
        assert result.tolerance == Decimal("1.0")
        assert result.line_item_ids == ["r3", "r4", "r5"]
    else:
        assert result.status == "skipped" and result.detail == "subtotal_scope_unknown"
        assert result.expected is result.actual is result.difference is result.tolerance is None


def test_high_precision_cancellation_preserves_values_and_serialized_metadata() -> None:
    before = getcontext().copy()
    with localcontext() as context:
        context.prec = 50
        rows = [
            item(1, "Revenue", "123456789012345678901234567890.123456789"),
            item(2, "Cost", "-123456789012345678901234567880.123456788"),
            item(3, "Total", "11.000000001", True),
        ]
        result = check_subtotals(statement(rows))[0]
        assert result.status == "pass"
        assert result.expected == Decimal("10.000000001")
        assert result.actual == Decimal("11.000000001")
        assert result.difference == Decimal("1.000000000")
        assert result.tolerance == Decimal("1.0")
        data = result.model_dump(mode="json")
        assert data["expected"] == "10.000000001"
        assert data["difference"] == "1.000000000" and data["tolerance"] == "1.0"
        assert context.prec == 50
    assert getcontext().prec == before.prec and getcontext().flags == before.flags


def test_low_precision_running_total_retains_left_associated_acceptance() -> None:
    before = getcontext().copy()
    with localcontext() as context:
        context.prec = 3
        rows = [
            item(1, "Prior total", "1000", True),
            item(2, "Adjustment", "-998"),
            item(3, "New total", "3.01", True),
        ]
        result = check_subtotals(statement(rows))[-1]
        # The acceptance path uses (actual - prior) - only, which rounds to 1;
        # the published difference uses actual - expected, which remains 1.01.
        assert result.status == "pass" and result.detail == ""
        assert result.expected == Decimal("2") and result.actual == Decimal("3.01")
        assert result.difference == Decimal("1.01") and result.tolerance == Decimal("1.0")
        assert result.line_item_ids == ["r1", "r2", "r3"]
        assert context.prec == 3
    assert getcontext().prec == before.prec and getcontext().flags == before.flags
