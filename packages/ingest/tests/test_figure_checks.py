"""Misreads the sums can point at or that no sum covers (spec 12, Data flow step 10)."""

from datetime import date
from decimal import Decimal

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
from fra_ingest.figure_checks import (
    check_net_profit_tie,
    flag_fractions,
    flag_period_outliers,
    leading_digit_lost,
    one_digit_apart,
    single_digit_place,
)
from fra_ingest.label_match import LabelIndex
from fra_ingest.table_checks import run_checks

INDEX = LabelIndex(load_taxonomy())
P1 = Period(key="2024-12-31", end_date=date(2024, 12, 31), kind=PeriodKind.INSTANT)
P2 = Period(key="2023-12-31", end_date=date(2023, 12, 31), kind=PeriodKind.INSTANT)
BOX = BBox(left=1, top=1, right=2, bottom=2)


def item(
    n: int, label: str, *values: str, total: bool = False, flags: tuple[str, ...] = ()
) -> LineItem:
    cells = [
        Cell(
            period_key=period.key,
            reported=Decimal(value),
            raw_text=value,
            provenance=Provenance(page_no=1, bbox=BOX, table_ref="#/tables/0", row=n, col=2),
            flags=list(flags),
        )
        for period, value in zip((P1, P2), values, strict=False)
    ]
    return LineItem(id=f"r{n}", raw_label=label, is_subtotal=total, cells=cells)


def statement(
    items: list[LineItem], kind: StatementType = StatementType.BALANCE, sid: str = "s"
) -> Statement:
    return Statement(
        id=sid,
        document_sha256="a" * 64,
        type=kind,
        currency="EGP",
        scale=1,
        periods=[P1, P2],
        line_items=items,
    )


@pytest.mark.parametrize(
    ("difference", "place"),
    [("5", 0), ("-800000", 5), ("5000000", 6), ("12", None), ("0", None), ("0.5", None)],
)
def test_single_digit_place(difference: str, place: int | None) -> None:
    assert single_digit_place(Decimal(difference)) == place


def test_one_digit_apart_needs_the_same_length_and_sign() -> None:
    assert one_digit_apart(Decimal("7743342656"), Decimal("7743342651"))
    assert not one_digit_apart(Decimal("95"), Decimal("105"))
    assert not one_digit_apart(Decimal("-5"), Decimal("5"))
    assert not one_digit_apart(Decimal("120"), Decimal("210"))


ASSETS = [
    item(1, "Property", "4491", "3371"),
    item(2, "Goodwill", "126", "81"),
    item(3, "Total non-current assets", "4617", "3452", total=True),
    item(4, "Inventories", "3034", "1866"),
    item(5, "Cash", "518", "1003"),
    item(6, "Total current assets", "3552", "2869", total=True),
]


def test_the_only_cell_one_digit_can_settle_is_the_suspect() -> None:
    rows = [*ASSETS, item(7, "Total assets", "8169", "6326", total=True)]
    checked, results = run_checks(statement(rows), INDEX)
    failed = next(r for r in results if r.status == "fail")
    assert failed.difference == Decimal("5")
    assert failed.detail == "sum_based; single_digit:10^0; suspect:r7=6321"
    flagged = [
        (i.id, c.period_key)
        for i in checked.line_items
        for c in i.cells
        if "digit_suspect" in c.flags
    ]
    assert flagged == [("r7", P2.key)]
    assert checked.line_items[6].value_for(P2.key) == Decimal("6326")


def test_several_cells_that_could_carry_the_digit_are_all_suspect() -> None:
    rows = [
        item(1, "Property", "4491", "3371"),
        item(2, "Right of use", "292", "122"),
        item(3, "Goodwill", "126", "81"),
        item(4, "Total non-current assets", "4929", "3574", total=True),
    ]
    checked, results = run_checks(statement(rows), INDEX)
    failed = next(r for r in results if r.status == "fail")
    assert failed.detail == "single_digit:10^1"
    flagged = {i.id for i in checked.line_items for c in i.cells if "digit_suspect" in c.flags}
    # 4491 + 20 and 292 + 20 carry into a second digit, so neither is a candidate.
    assert flagged == {"r3", "r4"}


def test_a_difference_that_is_not_one_digit_names_nothing() -> None:
    rows = [*ASSETS, item(7, "Total assets", "8169", "6399", total=True)]
    checked, results = run_checks(statement(rows), INDEX)
    assert next(r for r in results if r.status == "fail").detail == "sum_based"
    assert not any("digit_suspect" in c.flags for i in checked.line_items for c in i.cells)


def test_a_row_whose_periods_differ_a_thousand_times_is_an_outlier() -> None:
    rows = [
        item(1, "Share capital", "140002731", "2731"),
        item(2, "Legal reserve", "999000", "1000"),
        item(3, "Retained earnings", "1000000", "1000"),
        item(4, "Earnings per share", "5000", "2", flags=("per_share",)),
    ]
    flagged = flag_period_outliers(statement(rows))
    outliers = [
        i.id for i in flagged.line_items if any("period_outlier" in c.flags for c in i.cells)
    ]
    assert outliers == ["r1", "r3"]


INCOME = statement(
    [item(1, "Revenue", "900", "800"), item(2, "Net profit for the year", "160", "140")],
    StatementType.INCOME,
    "inc",
)


def test_net_profit_ties_between_the_two_statements() -> None:
    comprehensive = statement(
        [item(1, "Net profit for the year", "160", "140"), item(2, "Translation", "-5", "3")],
        StatementType.COMPREHENSIVE_INCOME,
        "ci",
    )
    results = check_net_profit_tie(INCOME, comprehensive)
    assert [(r.kind, r.status, r.statement_id) for r in results] == [
        ("net_profit_tie", "pass", "ci")
    ] * 2
    assert results[0].line_item_ids == ["r2", "r1"]


def test_net_profit_that_equals_no_income_row_fails_the_tie() -> None:
    comprehensive = statement(
        [item(1, "Net profit for the year", "160", "141")], StatementType.COMPREHENSIVE_INCOME, "ci"
    )
    results = check_net_profit_tie(INCOME, comprehensive)
    assert [r.status for r in results] == ["fail", "fail"]
    assert results[0].detail == "no_income_row_equal"


def test_no_shared_period_means_no_tie_check() -> None:
    other = Period(key="FY2020", end_date=date(2020, 12, 31), kind=PeriodKind.DURATION, months=12)
    comprehensive = statement(
        [item(1, "Net profit", "160", "140")], StatementType.COMPREHENSIVE_INCOME, "ci"
    ).model_copy(update={"periods": [other], "line_items": []})
    assert check_net_profit_tie(INCOME, comprehensive) == []


def test_a_cell_that_is_the_only_candidate_of_one_check_is_the_suspect_of_another() -> None:
    # Total assets misses its sum by 5 and is that check's only candidate. The identity misses
    # by the same 5 and could blame either side; the first check settles which.
    rows = [
        *ASSETS,
        item(7, "Total assets", "8169", "6326", total=True),
        item(8, "Total liabilities and equity", "8169", "6321", total=True),
    ]
    checked, results = run_checks(statement(rows), INDEX)
    identity = next(r for r in results if r.kind == "balance_identity" and r.status == "fail")
    assert identity.detail.endswith("single_digit:10^0; suspect:r7=6321")
    flagged = [i.id for i in checked.line_items for c in i.cells if "digit_suspect" in c.flags]
    assert flagged == ["r7"]


def test_a_figure_that_lost_its_leading_digit_is_among_the_suspects() -> None:
    rows = [
        item(1, "Deferred tax", "2", "302"),
        item(2, "Leases", "140", "140"),
        item(3, "Borrowings", "228", "228"),
        item(4, "Total non-current liabilities", "670", "670", total=True),
    ]
    checked, results = run_checks(statement(rows), INDEX)
    failed = next(r for r in results if r.status == "fail")
    assert failed.difference == Decimal("300") and "single_digit:10^2" in failed.detail
    flagged = {i.id for i in checked.line_items for c in i.cells if "digit_suspect" in c.flags}
    assert "r1" in flagged


def test_a_leading_digit_is_lost_only_above_every_digit_read() -> None:
    assert leading_digit_lost(Decimal("2414061"), Decimal("302414061"))
    assert leading_digit_lost(Decimal("-2"), Decimal("-302"))
    assert not leading_digit_lost(Decimal("140"), Decimal("440"))
    assert not leading_digit_lost(Decimal("302"), Decimal("2"))
    assert not leading_digit_lost(Decimal("2"), Decimal("-302"))
    assert not leading_digit_lost(Decimal("0"), Decimal("20000"))
    assert not leading_digit_lost(Decimal("75"), Decimal("800075"))
    assert leading_digit_lost(Decimal("14061"), Decimal("3014061"))


def test_a_fraction_among_whole_amounts_is_flagged() -> None:
    rows = [
        item(1, "Borrowings", "2282057066", "1129283746"),
        item(2, "Lease liabilities", "230717192", "1327.5608"),
        item(3, "Deferred tax", "302414061", "240116669"),
        item(4, "Earnings per share", "2.18", "2.25", flags=("per_share",)),
        item(5, "Margin", "0.125", "0.130", flags=("percent",)),
    ]
    flagged = flag_fractions(statement(rows))
    found = [
        (i.id, c.period_key)
        for i in flagged.line_items
        for c in i.cells
        if "fraction_among_whole" in c.flags
    ]
    assert found == [("r2", P2.key)]


def test_a_statement_printed_with_decimals_throughout_is_not_flagged() -> None:
    rows = [
        item(1, "Revenue", "1234.5", "1100.2"),
        item(2, "Cost of sales", "-800.1", "-700.9"),
        item(3, "Gross profit", "434.4", "399.3"),
    ]
    flagged = flag_fractions(statement(rows))
    assert not any("fraction_among_whole" in c.flags for i in flagged.line_items for c in i.cells)


WHOLE = [
    item(1, "Revenue", "22064876", "20979512"),
    item(2, "Cost of sales", "-15177063", "-14315460"),
    item(3, "Gross profit", "6887813", "6664052"),
    item(4, "Selling expenses", "-3231061", "-2993918"),
    item(5, "Operating profit", "3656752", "3670134"),
]


def test_a_small_fractional_row_no_cue_names_is_not_an_ocr_fraction() -> None:
    rows = [*WHOLE, item(6, "Return per unit held", "2.48", "2.34")]
    flagged = flag_fractions(statement(rows))
    assert not any("fraction_among_whole" in c.flags for i in flagged.line_items for c in i.cells)


def test_a_large_fraction_or_one_beside_a_whole_amount_is_flagged() -> None:
    rows = [
        *WHOLE,
        item(6, "Lease liabilities", "1327.5608"),
        item(7, "Provisions", "99601868", "10.5"),
    ]
    flagged = flag_fractions(statement(rows))
    found = [
        (i.id, c.period_key)
        for i in flagged.line_items
        for c in i.cells
        if "fraction_among_whole" in c.flags
    ]
    assert found == [("r6", P1.key), ("r7", P2.key)]


def test_a_statement_in_decimals_with_some_round_figures_is_not_flagged() -> None:
    rows = [
        item(1, "Revenue", "234.5", "200.2"),
        item(2, "Cost of sales", "-60.0", "-50.1"),
        item(3, "Gross profit", "174.5", "150.1"),
        item(4, "Selling expenses", "-30.2", "-28.0"),
        item(5, "Operating profit", "144.3", "122.1"),
    ]
    flagged = flag_fractions(statement(rows))
    assert not any("fraction_among_whole" in c.flags for i in flagged.line_items for c in i.cells)


def test_net_profit_tie_acceptance_and_metadata_use_the_shared_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fra_ingest import check_tolerance

    monkeypatch.setattr(check_tolerance, "ROUNDING_UNIT", Decimal("0.25"))
    comprehensive = statement(
        [item(1, "Profit", "160.375", "140")], StatementType.COMPREHENSIVE_INCOME, "ci"
    )
    failed = check_net_profit_tie(INCOME, comprehensive)
    assert [r.status for r in failed] == ["fail", "fail"]
    assert all(r.tolerance is None and r.expected is None for r in failed)
    comprehensive = statement(
        [item(1, "Profit", "160.25", "140")], StatementType.COMPREHENSIVE_INCOME, "ci"
    )
    passed = check_net_profit_tie(INCOME, comprehensive)
    assert [r.status for r in passed] == ["pass", "pass"]
    assert all(r.tolerance == Decimal("0.25") for r in passed)


@pytest.mark.parametrize("sign", [-1, 1])
@pytest.mark.parametrize("outside", [False, True])
@pytest.mark.parametrize("scale", [1, 1000])
def test_net_profit_tie_boundary_is_inclusive_in_reported_units(
    sign: int, outside: bool, scale: int
) -> None:
    difference = sign * Decimal("0.500001" if outside else "0.5")
    comprehensive = statement(
        [item(1, "Net profit", str(Decimal(160) + difference), str(Decimal(140) + difference))],
        StatementType.COMPREHENSIVE_INCOME,
        "ci",
    ).model_copy(update={"scale": scale})
    income = INCOME.model_copy(update={"scale": scale})
    results = check_net_profit_tie(income, comprehensive)
    assert [r.status for r in results] == ["fail" if outside else "pass"] * 2
    if outside:
        assert all(r.expected is r.difference is r.tolerance is None for r in results)
        assert all(r.line_item_ids == ["r1"] and r.detail == "no_income_row_equal" for r in results)
    else:
        assert [r.expected for r in results] == [Decimal("160"), Decimal("140")]
        assert all(r.difference == difference and r.tolerance == Decimal("0.5") for r in results)
        assert all(r.line_item_ids == ["r2", "r1"] and r.detail == "" for r in results)


def test_net_profit_tie_requires_one_candidate_for_all_shared_periods() -> None:
    income = statement(
        [item(1, "Profit A", "160", "142"), item(2, "Profit B", "162", "140")],
        StatementType.INCOME,
        "inc",
    )
    comprehensive = statement(
        [item(3, "Net profit", "160", "140")], StatementType.COMPREHENSIVE_INCOME, "ci"
    )
    results = check_net_profit_tie(income, comprehensive)
    assert [r.status for r in results] == ["fail", "fail"]
    assert all(r.tolerance is r.expected is r.difference is None for r in results)


def test_net_profit_tie_filters_missing_head_periods() -> None:
    head = item(1, "Net profit", "160", "140")
    head = head.model_copy(
        update={"cells": [head.cells[0].model_copy(update={"reported": None}), head.cells[1]]}
    )
    comprehensive = statement([head], StatementType.COMPREHENSIVE_INCOME, "ci")
    results = check_net_profit_tie(INCOME, comprehensive)
    assert [(r.period_key, r.status, r.tolerance) for r in results] == [
        (P2.key, "pass", Decimal("0.5"))
    ]


def test_net_profit_tie_with_values_but_no_shared_periods_has_no_checks() -> None:
    comprehensive = statement(
        [item(1, "Profit", "160")], StatementType.COMPREHENSIVE_INCOME, "ci"
    ).model_copy(update={"periods": [P1]})
    income = INCOME.model_copy(update={"periods": [P2]})
    assert check_net_profit_tie(income, comprehensive) == []
