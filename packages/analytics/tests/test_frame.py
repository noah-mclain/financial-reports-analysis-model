"""Statements to the tidy frame: one row per mapped printed cell, scaled, with its provenance."""

from datetime import date
from decimal import Decimal

import pytest
from statement_builders import (
    INCOME_ROWS,
    acme,
    annual,
    build,
    closing,
    row,
)

from fra_analytics.frame import to_frame
from fra_core.schemas import StatementType


def test_a_mapped_cell_becomes_a_row_with_its_scaled_value_and_provenance() -> None:
    income, _ = acme(scale=1000)
    frame = to_frame([income])
    [revenue] = [r for r in frame.rows if r.canonical_id == "revenue" and r.period.key == "FY2025"]
    assert revenue.reported == Decimal("1000")
    assert revenue.scale == 1000
    assert revenue.value == Decimal("1000000")  # 1,000 thousand
    assert revenue.currency == "SAR"
    assert revenue.statement_id == "acme-income"
    assert revenue.line_item_id == "acme-income-r1"
    assert revenue.provenance == income.line_items[0].cells[0].provenance


def test_an_unmapped_row_is_not_in_the_frame_but_is_counted() -> None:
    income = build(
        "s",
        StatementType.INCOME,
        [annual(2025)],
        [row("revenue", {"FY2025": 10}), row(None, {"FY2025": 5}, flag="unmapped")],
    )
    frame = to_frame([income])
    assert [r.canonical_id for r in frame.rows] == ["revenue"]
    assert frame.statement("s").unmapped_rows == 1


def test_a_cell_that_does_not_apply_to_its_period_is_left_out() -> None:
    income = build("s", StatementType.INCOME, [annual(2025)], [row("revenue", {"FY2025": 10})])
    income.line_items[0].cells[0].reported = None
    assert to_frame([income]).rows == ()


def test_the_statement_units_caveats_and_flags_are_kept() -> None:
    [income, _] = acme(caveats=["scale_assumed_units"], flags=["scale_missing"])
    kept = to_frame([income]).statement("acme-income")
    assert (kept.scale, kept.currency) == (1, "SAR")
    assert kept.caveats == ("scale_assumed_units",)
    assert kept.flags == ("scale_missing",)
    assert kept.type is StatementType.INCOME


def test_periods_of_a_type_are_distinct_and_in_date_order() -> None:
    frame = to_frame(acme())
    assert [p.key for p in frame.periods(StatementType.INCOME)] == ["FY2024", "FY2025"]
    assert [p.key for p in frame.periods(StatementType.BALANCE)] == ["2024-12-31", "2025-12-31"]
    assert frame.periods(StatementType.CASH_FLOW) == []


def test_lookup_finds_a_cell_by_item_type_and_period_end_not_by_key() -> None:
    frame = to_frame(acme())
    [found] = frame.lookup(
        "total_assets", StatementType.BALANCE, end_date=date(2025, 12, 31), months=None
    )
    assert found.reported == Decimal("2000")
    assert (
        frame.lookup("revenue", StatementType.BALANCE, end_date=date(2025, 12, 31), months=12) == []
    )
    [flow] = frame.lookup("revenue", StatementType.INCOME, end_date=date(2025, 12, 31), months=12)
    assert flow.reported == Decimal("1000")
    assert (
        frame.lookup("revenue", StatementType.INCOME, end_date=date(2025, 12, 31), months=6) == []
    )


def test_two_statements_with_one_id_are_an_error_naming_it() -> None:
    income = build("dup", StatementType.INCOME, [annual(2025)], INCOME_ROWS)
    other = build("dup", StatementType.BALANCE, [closing(2025)], [])
    with pytest.raises(ValueError, match="dup"):
        to_frame([income, other])
