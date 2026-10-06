"""Which rows each total covers, found by the sums (spec 12, Data flow step 10)."""

from datetime import date
from decimal import Decimal

import pytest

import fra_core.tolerance
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
from fra_ingest.sum_hierarchy import infer_sums

P1 = Period(key="2025-12-31", end_date=date(2025, 12, 31), kind=PeriodKind.INSTANT)
P2 = Period(key="2024-12-31", end_date=date(2024, 12, 31), kind=PeriodKind.INSTANT)
BOX = BBox(left=1, top=1, right=2, bottom=2)


def item(n: int, label: str, *values: str | None, total: bool = False) -> LineItem:
    """A row with one value per period: a number, "" for a blank cell, "?" for a cell that did
    not parse. No values at all makes a heading."""
    cells = []
    for period, value in zip((P1, P2), values, strict=False):
        assert value is not None
        flags = ["numbers_missing"] if value == "" else ["unparsed"] if value == "?" else []
        cells.append(
            Cell(
                period_key=period.key,
                reported=None if flags else Decimal(value),
                raw_text="" if value == "" else value,
                provenance=Provenance(page_no=1, bbox=BOX, table_ref="#/tables/0", row=n, col=2),
                flags=flags,
            )
        )
    return LineItem(id=f"r{n}", raw_label=label, is_subtotal=total, cells=cells)


def statement(items: list[LineItem], periods: int = 2) -> Statement:
    return Statement(
        id="s",
        document_sha256="a" * 64,
        type=StatementType.BALANCE,
        currency="SAR",
        scale=1000,
        periods=[P1, P2][:periods],
        line_items=items,
    )


BALANCE = [
    item(1, "Non-current assets"),
    item(2, "Property", "100", "90"),
    item(3, "Goodwill", "20", "20"),
    item(4, "Total non-current assets", "120", "110", total=True),
    item(5, "Current assets"),
    item(6, "Inventories", "30", "25"),
    item(7, "Cash", "10", "5"),
    item(8, "Total current assets", "40", "30", total=True),
    item(9, "Total assets", "160", "140", total=True),
]


def test_a_total_over_two_section_totals_is_found_across_headings() -> None:
    groups = {g.total_id: g for g in infer_sums(statement(BALANCE))}
    assert groups["r9"].addend_ids == ("r4", "r8")
    assert groups["r9"].basis == "sums"
    assert [o.status for o in groups["r9"].outcomes.values()] == ["pass", "pass"]
    assert groups["r4"].basis == "run" and groups["r4"].addend_ids == ("r2", "r3")


def test_a_running_total_takes_the_total_before_it() -> None:
    rows = [
        item(1, "Revenue", "100", "90"),
        item(2, "Cost of sales", "-60", "-50"),
        item(3, "Total gross profit", "40", "40", total=True),
        item(4, "Selling expenses", "-10", "-10"),
        item(5, "Administrative expenses", "-5", "-5"),
        item(6, "Total operating profit", "25", "25", total=True),
    ]
    groups = {g.total_id: g for g in infer_sums(statement(rows))}
    assert groups["r6"].addend_ids == ("r3", "r4", "r5")
    assert groups["r6"].basis == "run"


def test_a_row_without_a_cue_that_sums_the_rows_above_is_an_implicit_total() -> None:
    rows = [
        item(1, "Share capital", "100", "100"),
        item(2, "Retained earnings", "50", "40"),
        item(3, "Equity attributable to owners", "150", "140"),
        item(4, "Non-controlling interests", "10", "10"),
        item(5, "Total equity", "160", "150", total=True),
    ]
    groups = {g.total_id: g for g in infer_sums(statement(rows))}
    assert groups["r3"].implicit and groups["r3"].addend_ids == ("r1", "r2")
    assert groups["r5"].addend_ids == ("r3", "r4")


def test_a_suffix_that_fits_one_period_confirms_the_scope_and_the_other_period_fails() -> None:
    rows = [*BALANCE[:8], item(9, "Total assets", "160", "145", total=True)]
    group = next(g for g in infer_sums(statement(rows)) if g.total_id == "r9")
    assert group.addend_ids == ("r4", "r8") and group.basis == "sums"
    assert group.outcomes[P1.key].status == "pass"
    assert group.outcomes[P2.key].status == "fail"
    assert group.outcomes[P2.key].expected == Decimal("140")


def test_a_blank_addend_counts_as_zero_when_the_sum_holds_and_another_period_is_complete() -> None:
    rows = [
        item(1, "Share capital", "100", "100"),
        item(2, "Treasury shares", "-20", ""),
        item(3, "Retained earnings", "50", "40"),
        item(4, "Total equity", "130", "140", total=True),
    ]
    group = infer_sums(statement(rows))[0]
    assert group.outcomes[P1.key].blank_ids == ()
    assert group.outcomes[P2.key].status == "pass"
    assert group.outcomes[P2.key].blank_ids == ("r2",)


def test_a_blank_is_not_counted_when_no_period_is_complete() -> None:
    rows = [
        item(1, "Share capital", "100"),
        item(2, "Treasury shares", ""),
        item(3, "Retained earnings", "50"),
        item(4, "Total equity", "150", total=True),
    ]
    group = infer_sums(statement(rows, periods=1))[0]
    assert group.outcomes[P1.key].status == "skipped"
    assert group.outcomes[P1.key].detail == "missing_values"


def test_an_unparsed_addend_is_a_lost_value_not_a_blank() -> None:
    rows = [
        item(1, "Share capital", "100", "100"),
        item(2, "Reserves", "30", "?"),
        item(3, "Retained earnings", "50", "40"),
        item(4, "Total equity", "180", "140", total=True),
    ]
    group = infer_sums(statement(rows))[0]
    assert group.outcomes[P2.key].status == "skipped"
    assert group.outcomes[P2.key].detail == "missing_values"


def test_a_total_over_one_row_and_dashes_is_checked_against_its_run() -> None:
    rows = [
        item(1, "Assets"),
        item(2, "Investments", "0", "0"),
        item(3, "Cash", "70", "60"),
        item(4, "Total assets", "70", "65", total=True),
    ]
    group = infer_sums(statement(rows))[0]
    assert group.basis == "run"
    assert [o.status for o in group.outcomes.values()] == ["pass", "fail"]


def test_per_share_rows_join_no_sum() -> None:
    rows = [
        item(1, "Profit", "100", "90"),
        item(2, "Other income", "10", "10"),
        LineItem(
            id="r3",
            raw_label="Earnings per share",
            cells=[
                c.model_copy(update={"flags": ["per_share"]}) for c in item(3, "", "2", "1").cells
            ],
        ),
        item(4, "Total income", "110", "100", total=True),
    ]
    assert infer_sums(statement(rows))[0].addend_ids == ("r1", "r2")


def test_nothing_fits_so_the_run_is_judged_as_before() -> None:
    lone = [
        item(1, "Assets"),
        item(2, "Cash", "70", "60"),
        item(3, "Total", "99", "98", total=True),
    ]
    assert {o.detail for o in infer_sums(statement(lone))[0].outcomes.values()} == {
        "subtotal_scope_unknown"
    }
    cut = [
        item(1, "Property", "100", "90"),
        item(2, "Current assets"),
        item(3, "Inventories", "30", "25"),
        item(4, "Cash", "10", "5"),
        item(5, "Total assets", "999", "999", total=True),
    ]
    assert {o.detail for o in infer_sums(statement(cut))[0].outcomes.values()} == {
        "subtotal_scope_uncertain"
    }


def test_a_period_of_zeros_confirms_no_scope() -> None:
    rows = [
        item(1, "Land", "100", "0"),
        item(2, "Buildings", "150", "0"),
        item(3, "Equipment", "200", "0"),
        item(4, "Total property", "500", "0", total=True),
    ]
    group = infer_sums(statement(rows))[0]
    assert group.addend_ids == ("r1", "r2", "r3")
    assert group.outcomes[P1.key].status == "fail"
    assert group.outcomes[P1.key].expected == Decimal("450")


def test_a_total_over_a_single_row_is_checked_as_an_equality() -> None:
    rows = [
        item(1, "Non-current liabilities"),
        item(2, "Borrowings", "70", "60"),
        item(3, "Total non-current liabilities", "70", "65", total=True),
    ]
    group = infer_sums(statement(rows))[0]
    assert group.addend_ids == ("r2",)
    assert group.outcomes[P1.key].status == "pass"
    assert group.outcomes[P1.key].detail == "single_addend"
    assert group.outcomes[P2.key].status == "skipped"
    assert group.outcomes[P2.key].detail == "subtotal_scope_unknown"


def test_a_single_row_equality_is_exact() -> None:
    rows = [
        item(1, "Liabilities"),
        item(2, "Borrowings", "12.2", "70"),
        item(3, "Total liabilities", "12.6", "70", total=True),
    ]
    group = infer_sums(statement(rows))[0]
    assert group.outcomes[P1.key].status == "skipped"
    assert group.outcomes[P2.key].status == "pass"


def test_a_running_total_whose_one_new_row_is_zero_passes() -> None:
    rows = [
        item(1, "Revenue", "100", "90"),
        item(2, "Cost of sales", "-60", "-50"),
        item(3, "Total gross profit", "40", "40", total=True),
        item(4, "Other income", "0", "0"),
        item(5, "Total operating profit", "40", "40", total=True),
    ]
    group = infer_sums(statement(rows))[-1]
    assert group.total_id == "r5"
    assert [o.status for o in group.outcomes.values()] == ["pass", "pass"]
    assert group.outcomes[P1.key].addend_ids == ("r3", "r4")


def test_inferred_suffix_uses_the_shared_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fra_core.tolerance, "HALF_UNIT", Decimal("0.25"))
    rows = [
        item(1, "Capital", "10", "10"),
        item(2, "Reserves", "5", "5"),
        item(3, "Total equity", "15", "15.75", total=True),
    ]
    group = infer_sums(statement(rows))[0]
    assert group.confirmed
    assert group.outcomes[P1.key].status == "pass"
    assert group.outcomes[P2.key].status == "fail"
    assert group.outcomes[P2.key].expected == Decimal("15")


@pytest.mark.parametrize("sign", [-1, 1])
@pytest.mark.parametrize("outside", [False, True])
@pytest.mark.parametrize("zero", [False, True])
@pytest.mark.parametrize("scale", [1, 1000])
def test_confirmed_suffix_boundary_counts_explicit_zero_and_reports_units(
    sign: int, outside: bool, zero: bool, scale: int
) -> None:
    from fra_ingest.table_checks import check_subtotals

    tolerance = Decimal("1.5" if zero else "1.0")
    difference = sign * (tolerance + (Decimal("0.000001") if outside else Decimal(0)))
    rows = [item(1, "Capital", "100", "100"), item(2, "Reserves", "50", "50")]
    if zero:
        rows.append(item(3, "Treasasury", "0", "0"))
    rows.append(item(4, "Total equity", "150", str(Decimal(150) + difference), total=True))
    s = statement(rows).model_copy(update={"scale": scale})
    group = infer_sums(s)[0]
    assert group.confirmed and group.basis == "run"
    assert group.outcomes[P2.key].status == ("fail" if outside else "pass")
    assert group.outcomes[P2.key].blank_ids == ()
    assert group.outcomes[P2.key].addend_ids == tuple(r.id for r in rows[:-1])
    result = check_subtotals(s)[1]
    assert result.status == group.outcomes[P2.key].status
    assert result.expected == Decimal("150")
    assert result.actual == Decimal(150) + difference
    assert result.difference == difference and result.tolerance == tolerance
    assert result.line_item_ids == [r.id for r in rows]
    assert result.detail == ""


@pytest.mark.parametrize("outside", [False, True])
@pytest.mark.parametrize("sign", [-1, 1])
def test_confirmed_blank_is_excluded_from_tolerance(outside: bool, sign: int) -> None:
    from fra_core.taxonomy.loader import load_taxonomy
    from fra_ingest.label_match import LabelIndex
    from fra_ingest.table_checks import check_subtotals, run_checks

    difference = sign * Decimal("1.000001" if outside else "1")
    rows = [
        item(1, "Capital", "100", "100"),
        item(2, "Treasury", "-20", ""),
        item(3, "Reserves", "50", "40"),
        item(4, "Total equity", "130", str(Decimal(140) + difference), total=True),
    ]
    group = infer_sums(statement(rows))[0]
    outcome = group.outcomes[P2.key]
    result = check_subtotals(statement(rows))[1]
    checked, _ = run_checks(statement(rows), LabelIndex(load_taxonomy()))
    blank = checked.line_items[1].cells[1]
    assert blank.reported is None and blank.raw_text == ""
    assert blank.provenance == rows[1].cells[1].provenance
    assert checked.line_items[1].parent_id == "r4"
    assert statement(rows).line_items[1].cells[1].flags == ["numbers_missing"]
    if outside:
        assert outcome.status == result.status == "skipped"
        assert outcome.detail == result.detail == "missing_values"
        assert outcome.blank_ids == ()
        assert result.expected is result.actual is result.difference is result.tolerance is None
        assert result.line_item_ids == ["r4"]
        assert blank.flags == ["numbers_missing"]
    else:
        assert outcome.status == result.status == "pass"
        assert outcome.blank_ids == ("r2",)
        assert result.expected == Decimal("140")
        assert result.difference == difference and result.tolerance == Decimal("1.0")
        assert result.detail == "blank_as_zero"
        assert result.line_item_ids == ["r1", "r2", "r3", "r4"]
        assert blank.flags == ["numbers_missing", "blank_confirmed"]


def test_all_zero_rows_cannot_confirm_an_implicit_suffix() -> None:
    rows = [
        item(1, "Capital", "0", "0"),
        item(2, "Reserves", "0", "0"),
        item(3, "Equity", "0", "0"),
    ]
    assert infer_sums(statement(rows)) == []
