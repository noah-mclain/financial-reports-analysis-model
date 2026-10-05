"""The metric registry: formulas, their inputs and the order they are computed in.

``compute(frame, policy)`` follows the blueprint's shape (04, 1B.2) with a plain-typed frame in
place of a pandas one. Every formula is a function of the reader in ``inputs``; the rules D1 to
D5 sit in the reader and in ``compute``, not in each formula.

Blueprint 02, 2.6 lists metrics this registry leaves out, each for a stated reason:

- Free cash flow, FCF margin, cash conversion and the cash-flow add-back to EBITDA need a cash
  flow statement, and the structure stage converts balance, income and comprehensive income
  statements only. EBITDA is operating income plus depreciation and amortisation from the
  income statement, and null when that line is not printed there.
- ``revenue_cagr`` is one value over several periods, not one per period, and needs three or
  more annual periods: it does not fit a registry of per-period formulas. It is left until a
  caller needs it.
- ``eps_growth`` is a growth rate of ``eps_basic``, which the taxonomy has; it is not built
  because the week 2 plan asks for about twelve metrics and this registry already holds more.

Rules for the nulls (D1): a margin is computed on a negative base and flagged; every other
ratio is null on a zero or negative denominator; growth from a zero or negative base is null.
A component a total needs (borrowings, short-term investments) that is not printed leaves the
total null and names the component: it is never read as zero.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from fra_analytics.frame import Frame, FrameRow
from fra_analytics.metrics.inputs import Inputs
from fra_analytics.policy import Policy
from fra_analytics.unit_caveats import unit_notes
from fra_core.schemas import MetricInput, MetricUnit, MetricValue, Period, StatementType

FORMULA_VERSION = "1"

Formula = Callable[[Inputs], Decimal | None]
Interim = Literal["compute", "skip"]


@dataclass(frozen=True)
class MetricSpec:
    id: str
    formula: str
    unit: MetricUnit
    anchor: StatementType
    """The statement type whose periods the metric is computed for."""
    inputs: tuple[str, ...]
    """Canonical items the formula reads, for validation against the taxonomy."""
    fn: Formula
    interim: Interim = "compute"
    """D5: ``skip`` for a ratio of a flow to a stock, which is not annualized in v1."""


# Debt and EBITDA are shared by several metrics -------------------------------------------

_DEBT = (
    "short_term_borrowings",
    "current_portion_long_term_debt",
    "long_term_borrowings",
)
_LEASES = ("lease_liabilities_current", "lease_liabilities_non_current")
_QUICK_ASSETS = ("cash_and_equivalents", "short_term_investments", "trade_receivables")
_CASH = ("cash_and_equivalents", "short_term_investments")


def _total_debt(i: Inputs) -> Decimal | None:
    items = (*_DEBT, *_LEASES) if i.include_leases else _DEBT  # D3
    return i.total(*(i.closing(item) for item in items))


def _net_debt(i: Inputs) -> Decimal | None:
    debt = _total_debt(i)
    cash = i.total(*(i.closing(item) for item in _CASH))
    return None if debt is None or cash is None else debt - cash


def _ebitda(i: Inputs) -> Decimal | None:
    return i.total(i.flow("operating_income"), i.flow("depreciation_amortization"))


# Formulas --------------------------------------------------------------------------------


def _margin(numerator: str) -> Formula:
    # D1: the one family computed on a negative base, and flagged.
    return lambda i: i.ratio(i.flow(numerator), i.flow("revenue"), negative="flag")


def _roa(i: Inputs) -> Decimal | None:
    return i.ratio(i.flow("net_income"), i.average("total_assets"))


def _roe(i: Inputs) -> Decimal | None:
    return i.ratio(
        i.flow("net_income_attributable_parent"),
        i.average("equity_attributable_parent"),
    )


def _current_ratio(i: Inputs) -> Decimal | None:
    return i.ratio(i.closing("total_current_assets"), i.closing("total_current_liabilities"))


def _quick_ratio(i: Inputs) -> Decimal | None:
    quick = i.total(*(i.closing(item) for item in _QUICK_ASSETS))
    return i.ratio(quick, i.closing("total_current_liabilities"))


def _debt_to_equity(i: Inputs) -> Decimal | None:
    return i.ratio(_total_debt(i), i.closing("total_equity"))


def _net_debt_to_ebitda(i: Inputs) -> Decimal | None:
    return i.ratio(_net_debt(i), _ebitda(i))


def _interest_coverage(i: Inputs) -> Decimal | None:
    return i.ratio(i.flow("operating_income"), i.flow("finance_costs"))


def _asset_turnover(i: Inputs) -> Decimal | None:
    return i.ratio(i.flow("revenue"), i.average("total_assets"))


def _days(stock: str, flow: str) -> Formula:
    def formula(i: Inputs) -> Decimal | None:
        share = i.ratio(i.average(stock), i.flow(flow))
        days = i.days()
        return None if share is None or days is None else share * days

    return formula


_DSO = _days("trade_receivables", "revenue")
_DIO = _days("inventories", "cost_of_revenue")
_DPO = _days("trade_payables", "cost_of_revenue")


def _cash_conversion_cycle(i: Inputs) -> Decimal | None:
    # Every part is read before they are combined, so each missing input is named.
    dso, dio, dpo = _DSO(i), _DIO(i), _DPO(i)
    return None if dso is None or dio is None or dpo is None else dso + dio - dpo


def _cash_ratio(i: Inputs) -> Decimal | None:
    return i.ratio(i.closing("cash_and_equivalents"), i.closing("total_current_liabilities"))


def _growth(item: str) -> Formula:
    return lambda i: i.growth(i.flow(item), i.prior_flow(item))


INCOME = StatementType.INCOME
BALANCE = StatementType.BALANCE
_RATIO, _TIMES, _DAYS, _CURRENCY = (
    MetricUnit.RATIO,
    MetricUnit.TIMES,
    MetricUnit.DAYS,
    MetricUnit.CURRENCY,
)

REGISTRY: tuple[MetricSpec, ...] = (
    MetricSpec(
        "gross_margin",
        "gross_profit / revenue",
        _RATIO,
        INCOME,
        ("gross_profit", "revenue"),
        _margin("gross_profit"),
    ),
    MetricSpec(
        "operating_margin",
        "operating_income / revenue",
        _RATIO,
        INCOME,
        ("operating_income", "revenue"),
        _margin("operating_income"),
    ),
    MetricSpec(
        "net_margin",
        "net_income / revenue",
        _RATIO,
        INCOME,
        ("net_income", "revenue"),
        _margin("net_income"),
    ),
    MetricSpec(
        "ebitda",
        "operating_income + depreciation_amortization",
        _CURRENCY,
        INCOME,
        ("operating_income", "depreciation_amortization"),
        _ebitda,
    ),
    MetricSpec(
        "roa",
        "net_income / avg(total_assets)",
        _RATIO,
        INCOME,
        ("net_income", "total_assets"),
        _roa,
        interim="skip",
    ),
    MetricSpec(
        "roe",
        "net_income_attributable_parent / avg(equity_attributable_parent)",
        _RATIO,
        INCOME,
        ("net_income_attributable_parent", "equity_attributable_parent"),
        _roe,
        interim="skip",
    ),
    MetricSpec(
        "current_ratio",
        "total_current_assets / total_current_liabilities",
        _TIMES,
        BALANCE,
        ("total_current_assets", "total_current_liabilities"),
        _current_ratio,
    ),
    MetricSpec(
        "quick_ratio",
        "(cash_and_equivalents + short_term_investments + trade_receivables)"
        " / total_current_liabilities",
        _TIMES,
        BALANCE,
        (*_QUICK_ASSETS, "total_current_liabilities"),
        _quick_ratio,
    ),
    MetricSpec(
        "cash_ratio",
        "cash_and_equivalents / total_current_liabilities",
        _TIMES,
        BALANCE,
        ("cash_and_equivalents", "total_current_liabilities"),
        _cash_ratio,
    ),
    MetricSpec(
        "total_debt",
        "short_term_borrowings + current_portion_long_term_debt + long_term_borrowings"
        " (+ lease_liabilities_current + lease_liabilities_non_current)",
        _CURRENCY,
        BALANCE,
        (*_DEBT, *_LEASES),
        _total_debt,
    ),
    MetricSpec(
        "debt_to_equity",
        "total_debt / total_equity",
        _TIMES,
        BALANCE,
        (*_DEBT, *_LEASES, "total_equity"),
        _debt_to_equity,
    ),
    MetricSpec(
        "net_debt",
        "total_debt - cash_and_equivalents - short_term_investments",
        _CURRENCY,
        BALANCE,
        (*_DEBT, *_LEASES, *_CASH),
        _net_debt,
    ),
    MetricSpec(
        "net_debt_to_ebitda",
        "net_debt / ebitda",
        _TIMES,
        INCOME,
        (*_DEBT, *_LEASES, *_CASH, "operating_income", "depreciation_amortization"),
        _net_debt_to_ebitda,
        interim="skip",
    ),
    MetricSpec(
        "interest_coverage",
        "operating_income / abs(finance_costs)",
        _TIMES,
        INCOME,
        ("operating_income", "finance_costs"),
        _interest_coverage,
    ),
    MetricSpec(
        "asset_turnover",
        "revenue / avg(total_assets)",
        _TIMES,
        INCOME,
        ("revenue", "total_assets"),
        _asset_turnover,
        interim="skip",
    ),
    MetricSpec(
        "dso",
        "avg(trade_receivables) / revenue x days",
        _DAYS,
        INCOME,
        ("trade_receivables", "revenue"),
        _DSO,
    ),
    MetricSpec(
        "dio",
        "avg(inventories) / cost_of_revenue x days",
        _DAYS,
        INCOME,
        ("inventories", "cost_of_revenue"),
        _DIO,
    ),
    MetricSpec(
        "dpo",
        "avg(trade_payables) / cost_of_revenue x days",
        _DAYS,
        INCOME,
        ("trade_payables", "cost_of_revenue"),
        _DPO,
    ),
    MetricSpec(
        "cash_conversion_cycle",
        "dso + dio - dpo",
        _DAYS,
        INCOME,
        ("trade_receivables", "inventories", "trade_payables", "revenue", "cost_of_revenue"),
        _cash_conversion_cycle,
    ),
    MetricSpec(
        "revenue_growth",
        "revenue_t / revenue_(t-1) - 1",
        _RATIO,
        INCOME,
        ("revenue",),
        _growth("revenue"),
    ),
    MetricSpec(
        "net_income_growth",
        "net_income_t / net_income_(t-1) - 1",
        _RATIO,
        INCOME,
        ("net_income",),
        _growth("net_income"),
    ),
)


# Compute ---------------------------------------------------------------------------------


def _metric_input(row: FrameRow) -> MetricInput:
    return MetricInput(
        statement_id=row.statement_id,
        line_item_id=row.line_item_id,
        canonical_id=row.canonical_id,
        period_key=row.period.key,
        reported=row.reported,
        scale=row.scale,
        currency=row.currency,
        provenance=row.provenance,
    )


def _not_annual_flag(period: Period) -> str:
    """Why a flow-to-stock ratio is skipped for a period that is not a year."""
    if period.months is None:
        return "period_not_a_duration"
    if period.months > 12:
        return "period_longer_than_year"
    return "interim_not_annualized"  # D5


def _compute_one(spec: MetricSpec, frame: Frame, policy: Policy, period: Period) -> MetricValue:
    def result(
        value: Decimal | None,
        flags: list[str],
        inputs: dict[str, list[MetricInput]] | None = None,
        caveats: tuple[str, ...] = (),
    ) -> MetricValue:
        return MetricValue(
            metric_id=spec.id,
            period_key=period.key,
            value=None if value is None else float(value),
            unit=spec.unit,
            formula=spec.formula,
            inputs=inputs or {},
            formula_version=FORMULA_VERSION,
            flags=flags,
            caveats=list(caveats),
        )

    if spec.interim == "skip" and not period.is_annual:
        return result(None, [_not_annual_flag(period)])
    inputs = Inputs(frame, policy, period)
    value = spec.fn(inputs)
    flags = list(inputs.flags)
    used = {role: [_metric_input(r) for r in rows] for role, rows in inputs.cells.items() if rows}
    notes = unit_notes(
        spec.unit,
        [
            frame.statement(sid)
            for sid in {r.statement_id for rows in inputs.cells.values() for r in rows}
        ],
    )
    flags.extend(f for f in notes.flags if f not in flags)
    if notes.blocked:
        value = None
    return result(value, flags, used, notes.caveats)


def compute(frame: Frame, policy: Policy) -> list[MetricValue]:
    """Every metric of the registry for every period of the statements it is anchored on, in
    registry order then period order. A metric with a missing input is present, null, with a
    flag naming the input. A metric whose anchoring statement type is not in the frame has no
    period to be computed for and is absent."""
    return [
        _compute_one(spec, frame, policy, period)
        for spec in REGISTRY
        for period in frame.periods(spec.anchor)
    ]
