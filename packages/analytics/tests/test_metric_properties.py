"""Metric invariants (blueprint 05, T2): the properties that hold for any statements."""

from __future__ import annotations

from fractions import Fraction

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from statement_builders import POLICY, Row, annual, build, closing, row

from fra_analytics.frame import to_frame
from fra_analytics.metrics.registry import compute
from fra_core.schemas import MetricUnit, MetricValue, Statement, StatementType

# Items printed with a natural minus sign are drawn as magnitudes and printed negative.
INCOME_ITEMS = (
    "revenue",
    "cost_of_revenue",
    "gross_profit",
    "depreciation_amortization",
    "operating_income",
    "finance_costs",
    "net_income",
    "net_income_attributable_parent",
)
BALANCE_ITEMS = (
    "cash_and_equivalents",
    "short_term_investments",
    "trade_receivables",
    "inventories",
    "total_current_assets",
    "total_assets",
    "trade_payables",
    "short_term_borrowings",
    "current_portion_long_term_debt",
    "lease_liabilities_current",
    "total_current_liabilities",
    "long_term_borrowings",
    "lease_liabilities_non_current",
    "equity_attributable_parent",
    "total_equity",
)
EXPENSES = {"cost_of_revenue", "depreciation_amortization", "finance_costs"}

# Two years of each item: this year and last. Positive for the invariants that need a sensible
# statement; the range includes zero and negatives where the property says so.
positive = st.integers(min_value=1, max_value=10**9)
anything = st.integers(min_value=-(10**6), max_value=10**6)


def figures(amount: st.SearchStrategy[int]) -> st.SearchStrategy[dict[str, tuple[int, int]]]:
    return st.fixed_dictionaries(
        {item: st.tuples(amount, amount) for item in (*INCOME_ITEMS, *BALANCE_ITEMS)}
    )


def statements(drawn: dict[str, tuple[int, int]], scale: int) -> list[Statement]:
    def rows(items: tuple[str, ...], keys: tuple[str, str]) -> list[Row]:
        return [
            row(
                item,
                {keys[0]: -drawn[item][0] if item in EXPENSES else drawn[item][0],
                 keys[1]: -drawn[item][1] if item in EXPENSES else drawn[item][1]},
            )
            for item in items
        ]  # fmt: skip

    return [
        build(
            "i", StatementType.INCOME, [annual(2025), annual(2024)],
            rows(INCOME_ITEMS, ("FY2025", "FY2024")), scale=scale,
        ),
        build(
            "b", StatementType.BALANCE, [closing(2025), closing(2024)],
            rows(BALANCE_ITEMS, ("2025-12-31", "2024-12-31")), scale=scale,
        ),
    ]  # fmt: skip


def run(drawn: dict[str, tuple[int, int]], scale: int) -> dict[tuple[str, str], MetricValue]:
    return {
        (m.metric_id, m.period_key): m for m in compute(to_frame(statements(drawn, scale)), POLICY)
    }


def close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


@settings(max_examples=100, deadline=None, derandomize=True)
@given(drawn=figures(positive), k=st.sampled_from([1_000, 1_000_000]))
def test_ratios_times_and_days_do_not_change_with_the_unit(
    drawn: dict[str, tuple[int, int]], k: int
) -> None:
    base, scaled = run(drawn, 1), run(drawn, k)
    assert base.keys() == scaled.keys()
    for key, metric in base.items():
        other = scaled[key]
        assert (other.value is None) == (metric.value is None), key
        assert other.flags == metric.flags, key
        if metric.unit is not MetricUnit.CURRENCY and metric.value is not None:
            assert other.value is not None
            assert close(other.value, metric.value), key


@settings(max_examples=100, deadline=None, derandomize=True)
@given(drawn=figures(positive), k=st.sampled_from([1_000, 1_000_000]))
def test_currency_metrics_scale_linearly_with_the_unit(
    drawn: dict[str, tuple[int, int]], k: int
) -> None:
    base, scaled = run(drawn, 1), run(drawn, k)
    amounts = [key for key, m in base.items() if m.unit is MetricUnit.CURRENCY]
    assert amounts
    for key in amounts:
        value, other = base[key].value, scaled[key].value
        assert value is not None
        assert other is not None
        assert close(other, k * value), key


@settings(max_examples=100, deadline=None, derandomize=True)
@given(drawn=figures(anything))
def test_any_figures_give_a_finite_value_or_a_null_with_a_flag(
    drawn: dict[str, tuple[int, int]],
) -> None:
    # Zero and negative figures everywhere: never an infinity, never a NaN, never a null that
    # does not say why (MetricValue rejects each of those when it is built).
    produced = compute(to_frame(statements(drawn, 1)), POLICY)
    assert produced
    for metric in produced:
        assert metric.value is not None or metric.flags, metric.metric_id


@settings(max_examples=100, deadline=None, derandomize=True)
@example(a=1, b=55158913)  # 1 + g cancels in float here; the exact ratio does not
@given(a=positive, b=positive)
def test_growth_matches_the_exact_ratio_and_is_zero_for_no_change(a: int, b: int) -> None:
    def growth(now: int, before: int) -> float:
        income = build(
            "i",
            StatementType.INCOME,
            [annual(2025), annual(2024)],
            [row("revenue", {"FY2025": now, "FY2024": before})],
        )
        value = {(m.metric_id, m.period_key): m for m in compute(to_frame([income]), POLICY)}[
            ("revenue_growth", "FY2025")
        ].value
        assert value is not None
        return value

    # Growth forward and back, each against the exact rational result.
    for now, before in [(b, a), (a, b)]:
        exact = float(Fraction(now, before) - 1)
        assert growth(now, before) == pytest.approx(exact, rel=1e-12, abs=0)
    assert growth(a, a) == 0.0


@settings(max_examples=100, deadline=None, derandomize=True)
@given(drawn=figures(positive))
def test_the_average_of_two_equal_balances_is_the_balance(
    drawn: dict[str, tuple[int, int]],
) -> None:
    equal = {**drawn, "total_assets": (drawn["total_assets"][0], drawn["total_assets"][0])}
    roa = run(equal, 1)[("roa", "FY2025")].value
    assert roa is not None
    assert close(roa, drawn["net_income"][0] / drawn["total_assets"][0])
