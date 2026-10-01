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
    flag_period_outliers,
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
