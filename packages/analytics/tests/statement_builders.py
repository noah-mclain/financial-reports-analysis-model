"""Small statements for the analytics tests, built from the figures a test writes down."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from fra_analytics.policy import Policy
from fra_core.schemas import (
    BBox,
    Caveat,
    Cell,
    LineItem,
    MappingSource,
    Period,
    PeriodKind,
    Provenance,
    Statement,
    StatementType,
)
from fra_core.schemas.caveat import CaveatId

POLICY = Policy(include_lease_liabilities=True, day_count_basis=365)
SHA = "0" * 64

Figures = Mapping[str, int | str]


def annual(year: int) -> Period:
    return Period(key=f"FY{year}", end_date=date(year, 12, 31), kind=PeriodKind.DURATION, months=12)


def closing(year: int) -> Period:
    return Period(key=f"{year}-12-31", end_date=date(year, 12, 31), kind=PeriodKind.INSTANT)


def duration(key: str, end: date, months: int) -> Period:
    return Period(key=key, end_date=end, kind=PeriodKind.DURATION, months=months)


def instant(end: date) -> Period:
    return Period(key=end.isoformat(), end_date=end, kind=PeriodKind.INSTANT)


class Row(NamedTuple):
    """One row of a test statement: the canonical item it maps to (None leaves it unmapped),
    its figures by period key, and the mapping flag an unmapped row carries."""

    canonical_id: str | None
    figures: Figures
    flag: str | None = None


def row(canonical_id: str | None, figures: Figures, *, flag: str | None = None) -> Row:
    return Row(canonical_id, figures, flag)


def build(
    statement_id: str,
    kind: StatementType,
    periods: Sequence[Period],
    rows: Sequence[Row],
    *,
    scale: int = 1,
    currency: str = "SAR",
    caveats: Sequence[CaveatId] = (),
    flags: Sequence[str] = (),
) -> Statement:
    keys = [p.key for p in periods]
    items = []
    for number, (canonical_id, figures, flag) in enumerate(rows, start=1):
        cells = [
            Cell(
                period_key=key,
                reported=Decimal(str(figures[key])),
                raw_text=str(figures[key]),
                provenance=Provenance(
                    page_no=1,
                    bbox=BBox(left=0, top=number, right=10, bottom=number + 1),
                    table_ref="#/tables/0",
                    row=number,
                    col=column,
                ),
            )
            for column, key in enumerate(keys, start=1)
            if key in figures
        ]
        items.append(
            LineItem(
                id=f"{statement_id}-r{number}",
                raw_label=canonical_id or f"row {number}",
                canonical_id=canonical_id,
                mapping_source=MappingSource.LEXICON if canonical_id else None,
                mapping_flag="unmapped" if flag else None,
                cells=cells,
            )
        )
    return Statement(
        id=statement_id,
        document_sha256=SHA,
        type=kind,
        currency=currency,
        scale=scale,
        periods=list(periods),
        line_items=items,
        caveats=[Caveat(id=c) for c in caveats],
        flags=list(flags),
    )


# Acme, in whole currency units, annual 2024 and 2025. Every expected value in the tests is
# worked out by hand from these figures.
INCOME_ROWS = [
    row("revenue", {"FY2025": 1000, "FY2024": 800}),
    row("cost_of_revenue", {"FY2025": -600, "FY2024": -520}),
    row("gross_profit", {"FY2025": 400, "FY2024": 280}),
    row("depreciation_amortization", {"FY2025": -50, "FY2024": -40}),
    row("operating_income", {"FY2025": 200, "FY2024": 120}),
    row("finance_costs", {"FY2025": -40, "FY2024": -30}),
    row("net_income", {"FY2025": 120, "FY2024": 60}),
    row("net_income_attributable_parent", {"FY2025": 100, "FY2024": 50}),
    row("net_income_attributable_nci", {"FY2025": 20, "FY2024": 10}),
]
BALANCE_ROWS = [
    row("cash_and_equivalents", {"2025-12-31": 150, "2024-12-31": 100}),
    row("short_term_investments", {"2025-12-31": 50, "2024-12-31": 20}),
    row("trade_receivables", {"2025-12-31": 250, "2024-12-31": 150}),
    row("inventories", {"2025-12-31": 300, "2024-12-31": 200}),
    row("total_current_assets", {"2025-12-31": 800, "2024-12-31": 500}),
    row("total_non_current_assets", {"2025-12-31": 1200, "2024-12-31": 1100}),
    row("total_assets", {"2025-12-31": 2000, "2024-12-31": 1600}),
    row("trade_payables", {"2025-12-31": 180, "2024-12-31": 140}),
    row("short_term_borrowings", {"2025-12-31": 100, "2024-12-31": 80}),
    row("current_portion_long_term_debt", {"2025-12-31": 50, "2024-12-31": 40}),
    row("lease_liabilities_current", {"2025-12-31": 20, "2024-12-31": 10}),
    row("total_current_liabilities", {"2025-12-31": 350, "2024-12-31": 280}),
    row("long_term_borrowings", {"2025-12-31": 350, "2024-12-31": 300}),
    row("lease_liabilities_non_current", {"2025-12-31": 80, "2024-12-31": 60}),
    row("total_non_current_liabilities", {"2025-12-31": 650, "2024-12-31": 570}),
    row("total_liabilities", {"2025-12-31": 1000, "2024-12-31": 850}),
    row("equity_attributable_parent", {"2025-12-31": 900, "2024-12-31": 700}),
    row("non_controlling_interests", {"2025-12-31": 100, "2024-12-31": 50}),
    row("total_equity", {"2025-12-31": 1000, "2024-12-31": 750}),
]


def acme(
    *,
    scale: int = 1,
    currency: str = "SAR",
    caveats: Sequence[CaveatId] = (),
    flags: Sequence[str] = (),
) -> list[Statement]:
    return [
        build(
            "acme-income",
            StatementType.INCOME,
            [annual(2025), annual(2024)],
            INCOME_ROWS,
            scale=scale,
            currency=currency,
            caveats=caveats,
            flags=flags,
        ),
        build(
            "acme-balance",
            StatementType.BALANCE,
            [closing(2025), closing(2024)],
            BALANCE_ROWS,
            scale=scale,
            currency=currency,
            caveats=caveats,
            flags=flags,
        ),
    ]


def replace_row(rows: Sequence[Row], canonical_id: str, new: Row | None) -> list[Row]:
    """The rows with the one mapped to ``canonical_id`` replaced, or dropped when ``new`` is None."""
    out: list[Row] = []
    found = False
    for entry in rows:
        if entry.canonical_id == canonical_id:
            found = True
            if new is not None:
                out.append(new)
        else:
            out.append(entry)
    assert found, f"{canonical_id} is not in the rows"
    return out
