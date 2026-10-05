"""Each metric on a small statement, with every value worked out by hand in the test.

The statements are Acme's in ``statement_builders``: annual 2024 and 2025, whole SAR.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import pytest
from statement_builders import (
    BALANCE_ROWS,
    INCOME_ROWS,
    POLICY,
    Row,
    acme,
    annual,
    build,
    closing,
    duration,
    instant,
    replace_row,
    row,
)

from fra_analytics.frame import primary_statements, to_frame
from fra_analytics.metrics.registry import REGISTRY, compute
from fra_analytics.policy import Policy
from fra_core.schemas import MetricUnit, MetricValue, Period, Statement, StatementType
from fra_core.taxonomy.loader import load_taxonomy

Metrics = dict[tuple[str, str], MetricValue]


def metrics(statements: Sequence[Statement], policy: Policy = POLICY) -> Metrics:
    return {(m.metric_id, m.period_key): m for m in compute(to_frame(statements), policy)}


def approx(expected: float) -> object:
    return pytest.approx(expected, rel=1e-12)


def acme_with(
    income: Sequence[Row] = INCOME_ROWS, balance: Sequence[Row] = BALANCE_ROWS
) -> list[Statement]:
    return [
        build("acme-income", StatementType.INCOME, [annual(2025), annual(2024)], income),
        build("acme-balance", StatementType.BALANCE, [closing(2025), closing(2024)], balance),
    ]


# (metric, period, unit, value). Every value is the hand calculation written beside it.
ACME_VALUES = [
    ("gross_margin", "FY2025", MetricUnit.RATIO, 400 / 1000),
    ("gross_margin", "FY2024", MetricUnit.RATIO, 280 / 800),
    ("operating_margin", "FY2025", MetricUnit.RATIO, 200 / 1000),
    ("net_margin", "FY2025", MetricUnit.RATIO, 120 / 1000),
    ("net_margin", "FY2024", MetricUnit.RATIO, 60 / 800),
    ("ebitda", "FY2025", MetricUnit.CURRENCY, 200 + 50),
    ("ebitda", "FY2024", MetricUnit.CURRENCY, 120 + 40),
    ("roa", "FY2025", MetricUnit.RATIO, 120 / ((2000 + 1600) / 2)),  # 120 / 1800
    ("roe", "FY2025", MetricUnit.RATIO, 100 / ((900 + 700) / 2)),  # 100 / 800 = 0.125
    ("current_ratio", "2025-12-31", MetricUnit.TIMES, 800 / 350),
    ("current_ratio", "2024-12-31", MetricUnit.TIMES, 500 / 280),
    ("quick_ratio", "2025-12-31", MetricUnit.TIMES, (150 + 50 + 250) / 350),
    ("quick_ratio", "2024-12-31", MetricUnit.TIMES, (100 + 20 + 150) / 280),
    ("cash_ratio", "2025-12-31", MetricUnit.TIMES, 150 / 350),
    ("cash_ratio", "2024-12-31", MetricUnit.TIMES, 100 / 280),
    ("total_debt", "2025-12-31", MetricUnit.CURRENCY, 100 + 50 + 350 + 20 + 80),  # 600
    ("total_debt", "2024-12-31", MetricUnit.CURRENCY, 80 + 40 + 300 + 10 + 60),  # 490
    ("debt_to_equity", "2025-12-31", MetricUnit.TIMES, 600 / 1000),
    ("debt_to_equity", "2024-12-31", MetricUnit.TIMES, 490 / 750),
    ("net_debt", "2025-12-31", MetricUnit.CURRENCY, 600 - 150 - 50),
    ("net_debt", "2024-12-31", MetricUnit.CURRENCY, 490 - 100 - 20),
    ("net_debt_to_ebitda", "FY2025", MetricUnit.TIMES, 400 / 250),
    ("net_debt_to_ebitda", "FY2024", MetricUnit.TIMES, 370 / 160),
    ("interest_coverage", "FY2025", MetricUnit.TIMES, 200 / 40),
    ("interest_coverage", "FY2024", MetricUnit.TIMES, 120 / 30),
    ("asset_turnover", "FY2025", MetricUnit.TIMES, 1000 / 1800),
    ("dso", "FY2025", MetricUnit.DAYS, ((250 + 150) / 2) / 1000 * 365),  # 73
    ("dio", "FY2025", MetricUnit.DAYS, ((300 + 200) / 2) / 600 * 365),  # 250 / 600 x 365
    ("dpo", "FY2025", MetricUnit.DAYS, ((180 + 140) / 2) / 600 * 365),
    # DSO 73 + DIO 250 / 600 x 365 (152.08) less DPO 160 / 600 x 365 (97.33) = 127.75
    ("cash_conversion_cycle", "FY2025", MetricUnit.DAYS, 73 + 250 / 600 * 365 - 160 / 600 * 365),
    ("revenue_growth", "FY2025", MetricUnit.RATIO, 1000 / 800 - 1),  # 0.25
    ("net_income_growth", "FY2025", MetricUnit.RATIO, 120 / 60 - 1),  # 1.0
]


@pytest.mark.parametrize(("metric_id", "period", "unit", "value"), ACME_VALUES)
def test_metric_on_acme(metric_id: str, period: str, unit: MetricUnit, value: float) -> None:
    metric = metrics(acme())[(metric_id, period)]
    assert metric.unit is unit
    assert metric.value == approx(value)
    assert metric.flags == []
    assert metric.formula_version == "1"


def test_hand_values_that_are_exact_are_exact() -> None:
    found = metrics(acme())
    assert found[("dso", "FY2025")].value == 73.0
    assert found[("revenue_growth", "FY2025")].value == 0.25
    assert found[("net_income_growth", "FY2025")].value == 1.0
    assert found[("roe", "FY2025")].value == 0.125
    assert found[("interest_coverage", "FY2025")].value == 5.0


def test_a_metric_states_its_formula() -> None:
    found = metrics(acme())
    assert found[("net_margin", "FY2025")].formula == "net_income / revenue"
    assert found[("roa", "FY2025")].formula == "net_income / avg(total_assets)"


def test_the_registry_uses_only_taxonomy_items_and_unique_ids() -> None:
    taxonomy = load_taxonomy()
    ids = [spec.id for spec in REGISTRY]
    assert len(ids) == len(set(ids))
    for spec in REGISTRY:
        for item in spec.inputs:
            assert taxonomy.by_id(item) is not None, f"{spec.id} reads unknown item {item}"


# D3 ----------------------------------------------------------------------------------------


def test_d3_leases_count_in_debt_by_default_and_leave_it_when_the_policy_says() -> None:
    without = Policy(include_lease_liabilities=False, day_count_basis=365)
    found = metrics(acme(), without)
    assert found[("total_debt", "2025-12-31")].value == 100 + 50 + 350  # 500
    assert found[("debt_to_equity", "2025-12-31")].value == 0.5
    assert found[("net_debt", "2025-12-31")].value == 500 - 150 - 50  # 300
    assert found[("net_debt_to_ebitda", "FY2025")].value == 300 / 250
    used = found[("total_debt", "2025-12-31")].inputs
    assert "lease_liabilities_current" not in used
    assert "lease_liabilities_non_current" in metrics(acme())[("total_debt", "2025-12-31")].inputs


# D2 ----------------------------------------------------------------------------------------


def test_d2_an_average_takes_opening_and_closing_balance() -> None:
    roa = metrics(acme())[("roa", "FY2025")]
    cells = roa.inputs["total_assets"]
    assert [(c.period_key, c.reported) for c in cells] == [
        ("2024-12-31", Decimal(1600)),
        ("2025-12-31", Decimal(2000)),
    ]


def test_d2_a_missing_opening_balance_falls_back_to_closing_with_a_flag() -> None:
    found = metrics(acme())
    roa = found[("roa", "FY2024")]  # no 2023 balance sheet
    assert roa.value == approx(60 / 1600)
    assert roa.flags == ["average_fallback_to_closing"]
    assert [c.period_key for c in roa.inputs["total_assets"]] == ["2024-12-31"]
    assert found[("roe", "FY2024")].value == approx(50 / 700)
    assert found[("asset_turnover", "FY2024")].value == approx(800 / 1600)
    assert found[("dso", "FY2024")].flags == ["average_fallback_to_closing"]


# D1 ----------------------------------------------------------------------------------------


def with_revenue(figure: int) -> Metrics:
    rows = replace_row(INCOME_ROWS, "revenue", row("revenue", {"FY2025": figure, "FY2024": 800}))
    return metrics(acme_with(income=rows))


def test_d1_margins_on_a_negative_revenue_are_computed_and_flagged() -> None:
    found = with_revenue(-1000)
    assert found[("net_margin", "FY2025")].value == approx(120 / -1000)
    assert found[("net_margin", "FY2025")].flags == ["negative_base"]
    assert found[("gross_margin", "FY2025")].flags == ["negative_base"]
    assert found[("operating_margin", "FY2025")].flags == ["negative_base"]


def test_d1_a_zero_denominator_is_null_with_a_flag_never_infinity() -> None:
    found = with_revenue(0)
    for metric_id in ("gross_margin", "operating_margin", "net_margin"):
        assert found[(metric_id, "FY2025")].value is None, metric_id
    assert found[("net_margin", "FY2025")].flags == ["zero_denominator"]
    assert found[("dso", "FY2025")].flags == ["zero_denominator"]
    assert found[("revenue_growth", "FY2025")].value == approx(0 / 800 - 1)  # a 100% fall


def test_d1_roe_on_negative_equity_is_null_with_undefined_negative_denominator() -> None:
    rows = replace_row(
        BALANCE_ROWS,
        "equity_attributable_parent",
        row("equity_attributable_parent", {"2025-12-31": -50, "2024-12-31": -100}),
    )
    found = metrics(acme_with(balance=rows))
    roe = found[("roe", "FY2025")]  # average equity is (-100 - 50) / 2 = -75
    assert roe.value is None
    assert roe.flags == ["undefined_negative_denominator"]
    assert [c.reported for c in roe.inputs["equity_attributable_parent"]] == [-100, -50]


def test_d1_every_ratio_other_than_a_margin_is_null_on_a_negative_denominator() -> None:
    # D1 names ROE and net debt to EBITDA; for the others the same rule applies: only margins
    # are computed on a negative base.
    def balance_with(item: str, now: int, before: int) -> Metrics:
        rows = replace_row(BALANCE_ROWS, item, row(item, {"2025-12-31": now, "2024-12-31": before}))
        return metrics(acme_with(balance=rows))

    equity = balance_with("total_equity", -500, 750)
    assert equity[("debt_to_equity", "2025-12-31")].value is None
    assert equity[("debt_to_equity", "2025-12-31")].flags == ["undefined_negative_denominator"]
    liabilities = balance_with("total_current_liabilities", -350, 280)
    for metric_id in ("current_ratio", "quick_ratio", "cash_ratio"):
        assert liabilities[(metric_id, "2025-12-31")].value is None, metric_id
        assert liabilities[(metric_id, "2025-12-31")].flags == ["undefined_negative_denominator"]
    assets = balance_with("total_assets", -2000, -1600)  # the average is -1800
    for metric_id in ("roa", "asset_turnover"):
        assert assets[(metric_id, "FY2025")].value is None, metric_id
        assert assets[(metric_id, "FY2025")].flags == ["undefined_negative_denominator"]
    costs = replace_row(
        INCOME_ROWS, "finance_costs", row("finance_costs", {"FY2025": 0, "FY2024": -30})
    )
    assert metrics(acme_with(income=costs))[("interest_coverage", "FY2025")].flags == [
        "zero_denominator"
    ]
    revenue = with_revenue(-1000)
    # DIO and DPO divide by cost of revenue, which stays positive: only DSO has the negative.
    assert revenue[("dso", "FY2025")].value is None
    assert revenue[("dso", "FY2025")].flags == ["undefined_negative_denominator"]
    assert revenue[("cash_conversion_cycle", "FY2025")].value is None


def test_d1_net_debt_to_ebitda_on_negative_or_zero_ebitda_is_null() -> None:
    def with_operating_income(figure: int) -> Metrics:
        rows = replace_row(
            INCOME_ROWS,
            "operating_income",
            row("operating_income", {"FY2025": figure, "FY2024": 120}),
        )
        return metrics(acme_with(income=rows))

    negative = with_operating_income(-100)  # ebitda = -100 + 50 = -50
    assert negative[("ebitda", "FY2025")].value == -50
    assert negative[("net_debt_to_ebitda", "FY2025")].value is None
    assert negative[("net_debt_to_ebitda", "FY2025")].flags == ["undefined_negative_denominator"]
    zero = with_operating_income(-50)  # ebitda = 0
    assert zero[("net_debt_to_ebitda", "FY2025")].flags == ["zero_denominator"]


def test_d1_growth_from_a_zero_or_a_negative_base_is_null_and_says_which() -> None:
    rows = [
        row("revenue", {"FY2025": 1000, "FY2024": 800}),
        row("net_income", {"FY2025": 120, "FY2024": -60}),
    ]
    found = metrics([build("i", StatementType.INCOME, [annual(2025), annual(2024)], rows)])
    assert found[("net_income_growth", "FY2025")].value is None
    assert found[("net_income_growth", "FY2025")].flags == ["undefined_negative_base"]
    rows[1] = row("net_income", {"FY2025": 120, "FY2024": 0})
    found = metrics([build("i", StatementType.INCOME, [annual(2025), annual(2024)], rows)])
    assert found[("net_income_growth", "FY2025")].flags == ["zero_denominator"]


def test_the_first_year_has_no_growth_and_says_which_input_is_missing() -> None:
    growth = metrics(acme())[("revenue_growth", "FY2024")]
    assert growth.value is None
    assert growth.flags == ["missing_input:revenue@prior_period"]


# D4 and D5 ---------------------------------------------------------------------------------

H1 = duration("H1-2025", date(2025, 6, 30), 6)
Q1 = duration("Q1-2025", date(2025, 3, 31), 3)


def interim_statements(
    period: Period, end: date, income: int, receivables: tuple[int, int]
) -> list[Statement]:
    opening_date = date(2024, 12, 31)
    income_rows = [
        row("revenue", {period.key: income}),
        row("net_income", {period.key: 20}),
        row("operating_income", {period.key: 40}),
    ]
    balance_rows = [
        row("trade_receivables", {end.isoformat(): receivables[1], "2024-12-31": receivables[0]}),
        row("total_assets", {end.isoformat(): 1000, "2024-12-31": 900}),
        row("equity_attributable_parent", {end.isoformat(): 400, "2024-12-31": 380}),
    ]
    return [
        build("i", StatementType.INCOME, [period], income_rows),
        build("b", StatementType.BALANCE, [instant(end), instant(opening_date)], balance_rows),
    ]


def test_d4_a_six_month_period_uses_its_actual_181_days() -> None:
    statements = interim_statements(H1, date(2025, 6, 30), 500, (250, 260))
    dso = metrics(statements)[("dso", "H1-2025")]
    assert dso.value == approx(((250 + 260) / 2) / 500 * 181)  # 0.51 x 181 = 92.31
    assert dso.flags == []


def test_d4_a_three_month_period_uses_its_actual_90_days() -> None:
    statements = interim_statements(Q1, date(2025, 3, 31), 300, (150, 180))
    assert metrics(statements)[("dso", "Q1-2025")].value == approx(((150 + 180) / 2) / 300 * 90)


def test_d4_the_policy_sets_the_annual_day_count() -> None:
    p360 = Policy(include_lease_liabilities=True, day_count_basis=360)
    assert metrics(acme(), p360)[("dso", "FY2025")].value == 200 / 1000 * 360


def test_d5_flow_to_stock_ratios_are_not_annualized_for_an_interim_period() -> None:
    found = metrics(interim_statements(H1, date(2025, 6, 30), 500, (250, 260)))
    for metric_id in ("roa", "roe", "asset_turnover", "net_debt_to_ebitda"):
        metric = found[(metric_id, "H1-2025")]
        assert metric.value is None, metric_id
        assert metric.flags == ["interim_not_annualized"], metric_id
    # Flow to flow ratios and the margins are unaffected.
    assert found[("net_margin", "H1-2025")].value == approx(20 / 500)
    assert found[("operating_margin", "H1-2025")].value == approx(40 / 500)


def test_d5_an_eighteen_month_period_is_not_an_interim_and_has_no_annual_only_ratio() -> None:
    long_period = duration("FY2025-18m", date(2025, 12, 31), 18)
    income = build(
        "i",
        StatementType.INCOME,
        [long_period],
        [row("revenue", {"FY2025-18m": 1500}), row("net_income", {"FY2025-18m": 150})],
    )
    balance = build(
        "b",
        StatementType.BALANCE,
        [closing(2025)],
        [row("total_assets", {"2025-12-31": 2000})],
    )
    found = metrics([income, balance])
    for metric_id in ("roa", "roe", "asset_turnover", "net_debt_to_ebitda"):
        assert found[(metric_id, "FY2025-18m")].value is None, metric_id
        assert found[(metric_id, "FY2025-18m")].flags == ["period_longer_than_year"], metric_id
    assert found[("net_margin", "FY2025-18m")].value == approx(0.1)


def test_an_instant_period_on_an_income_statement_gives_null_flags_and_no_exception() -> None:
    # The schema allows an instant period on any statement; a flow metric over it has no length.
    income = build(
        "i",
        StatementType.INCOME,
        [closing(2025)],
        [row("revenue", {"2025-12-31": 1000}), row("net_income", {"2025-12-31": 100})],
    )
    balance = build(
        "b",
        StatementType.BALANCE,
        [closing(2025), closing(2024)],
        [
            row("total_assets", {"2025-12-31": 2000, "2024-12-31": 1600}),
            row("trade_receivables", {"2025-12-31": 250, "2024-12-31": 150}),
        ],
    )
    found = metrics([income, balance])
    assert found[("roa", "2025-12-31")].value is None
    assert found[("roa", "2025-12-31")].flags == ["period_not_a_duration"]
    assert found[("dso", "2025-12-31")].value is None
    assert found[("dso", "2025-12-31")].flags == ["period_not_a_duration"]
    assert found[("net_margin", "2025-12-31")].value == approx(0.1)


def test_growth_compares_periods_of_equal_length_only() -> None:
    rows = [row("revenue", {"H1-2025": 500, "FY2024": 800})]
    statement = build("i", StatementType.INCOME, [H1, annual(2024)], rows)
    growth = metrics([statement])[("revenue_growth", "H1-2025")]
    assert growth.value is None  # no 6-month period ended 30 June 2024
    assert growth.flags == ["missing_input:revenue@prior_period"]
    like = [row("revenue", {"H1-2025": 500, "H1-2024": 400})]
    prior = duration("H1-2024", date(2024, 6, 30), 6)
    found = metrics([build("i", StatementType.INCOME, [H1, prior], like)])
    assert found[("revenue_growth", "H1-2025")].value == 0.25


# Missing and unmapped inputs ---------------------------------------------------------------


def test_a_missing_input_gives_null_and_a_flag_naming_it_never_zero() -> None:
    rows = replace_row(BALANCE_ROWS, "trade_receivables", None)
    found = metrics(acme_with(balance=rows))
    quick = found[("quick_ratio", "2025-12-31")]
    assert quick.value is None
    assert quick.flags == ["missing_input:trade_receivables"]
    assert found[("dso", "FY2025")].flags == ["missing_input:trade_receivables"]
    assert found[("current_ratio", "2025-12-31")].value == approx(800 / 350)  # unaffected


def test_every_missing_input_is_named() -> None:
    rows = replace_row(BALANCE_ROWS, "trade_receivables", None)
    rows = replace_row(rows, "short_term_investments", None)
    quick = metrics(acme_with(balance=rows))[("quick_ratio", "2025-12-31")]
    assert quick.flags == [
        "missing_input:short_term_investments",
        "missing_input:trade_receivables",
    ]


def test_an_input_that_may_sit_among_unmapped_rows_says_so_without_claiming_it_does() -> None:
    rows = replace_row(
        BALANCE_ROWS,
        "trade_receivables",
        row(None, {"2025-12-31": 250, "2024-12-31": 150}, flag="unmapped"),
    )
    quick = metrics(acme_with(balance=rows))[("quick_ratio", "2025-12-31")]
    assert quick.value is None
    assert quick.flags == [
        "missing_input:trade_receivables",
        "input_may_be_unmapped:trade_receivables",
    ]


def test_a_statement_with_unmapped_rows_flags_only_the_inputs_it_lacks() -> None:
    rows = [*BALANCE_ROWS, row(None, {"2025-12-31": 7, "2024-12-31": 8}, flag="unmapped")]
    found = metrics(acme_with(balance=rows))
    assert found[("quick_ratio", "2025-12-31")].flags == []  # every input is present
    assert found[("current_ratio", "2025-12-31")].flags == []


def test_two_rows_on_one_item_make_it_ambiguous_not_a_guess() -> None:
    rows = [*INCOME_ROWS, row("revenue", {"FY2025": 999, "FY2024": 1})]
    found = metrics(acme_with(income=rows))
    margin = found[("net_margin", "FY2025")]
    assert margin.value is None
    assert margin.flags == ["ambiguous_input:revenue"]


def test_a_missing_statement_type_gives_no_metrics_of_that_basis() -> None:
    income_only = acme()[:1]
    found = metrics(income_only)
    assert ("net_margin", "FY2025") in found
    assert not [k for k in found if k[0] == "current_ratio"]
    assert found[("roa", "FY2025")].value is None
    assert found[("roa", "FY2025")].flags == ["missing_input:total_assets"]


def test_an_expense_is_a_magnitude_whichever_sign_it_is_printed_with() -> None:
    # Many issuers print costs positive. The magnitude is used either way, so the sign changes
    # nothing and there is nothing to flag.
    rows = replace_row(
        INCOME_ROWS, "finance_costs", row("finance_costs", {"FY2025": 40, "FY2024": -30})
    )
    found = metrics(acme_with(income=rows))
    assert found[("interest_coverage", "FY2025")].value == 5.0
    assert found[("interest_coverage", "FY2025")].flags == []
    assert found[("interest_coverage", "FY2024")].value == 4.0
    assert found[("interest_coverage", "FY2024")].flags == []


def test_the_sign_a_statement_prints_does_not_change_a_cost_based_metric() -> None:
    positive = [
        row("cost_of_revenue", {"FY2025": 600, "FY2024": 520})
        if r.canonical_id == "cost_of_revenue"
        else r
        for r in INCOME_ROWS
    ]
    printed_negative, printed_positive = metrics(acme()), metrics(acme_with(income=positive))
    for key in (("dio", "FY2025"), ("dpo", "FY2025"), ("cash_conversion_cycle", "FY2025")):
        assert printed_positive[key].value == printed_negative[key].value
        assert printed_positive[key].flags == []


def test_row_order_does_not_change_any_result() -> None:
    forward = metrics(acme())
    reverse = metrics(
        [
            build(
                "acme-income", StatementType.INCOME, [annual(2025), annual(2024)], INCOME_ROWS[::-1]
            ),
            build(
                "acme-balance",
                StatementType.BALANCE,
                [closing(2025), closing(2024)],
                BALANCE_ROWS[::-1],
            ),
        ]
    )
    assert forward.keys() == reverse.keys()
    for key, metric in forward.items():
        assert reverse[key].value == metric.value
        assert reverse[key].flags == metric.flags


# Provenance --------------------------------------------------------------------------------


def test_every_computed_value_names_the_cells_it_came_from() -> None:
    statements = acme()
    cells = {
        (s.id, i.id, c.period_key): c for s in statements for i in s.line_items for c in i.cells
    }
    produced = compute(to_frame(statements), POLICY)
    assert produced
    for metric in produced:
        if metric.value is None:
            continue
        assert metric.inputs, metric.metric_id
        for role_cells in metric.inputs.values():
            assert role_cells
            for used in role_cells:
                cell = cells[(used.statement_id, used.line_item_id, used.period_key)]
                assert used.provenance == cell.provenance
                assert used.reported == cell.reported
                assert (used.scale, used.currency) == (1, "SAR")


def test_net_margin_traces_to_the_two_printed_cells() -> None:
    income = acme()[0]
    margin = metrics(acme())[("net_margin", "FY2025")]
    revenue_item = next(i for i in income.line_items if i.canonical_id == "revenue")
    [revenue] = margin.inputs["revenue"]
    assert revenue.line_item_id == revenue_item.id
    assert revenue.provenance == revenue_item.cells[0].provenance
    assert list(margin.inputs) == ["net_income", "revenue"]


def test_a_composite_metric_traces_through_to_every_component_cell() -> None:
    ratio = metrics(acme())[("net_debt_to_ebitda", "FY2025")]
    assert set(ratio.inputs) == {
        "short_term_borrowings",
        "current_portion_long_term_debt",
        "long_term_borrowings",
        "lease_liabilities_current",
        "lease_liabilities_non_current",
        "cash_and_equivalents",
        "short_term_investments",
        "operating_income",
        "depreciation_amortization",
    }


# A missing component -----------------------------------------------------------------------


def test_a_missing_debt_or_cash_component_makes_the_totals_null_and_names_it() -> None:
    # PENDING OWNER DECISION: an issuer that prints only long-term borrowings, or no short-term
    # investments line, may really have none of the other component, but a statement cannot say
    # "none" apart from "not mapped". Until the owner rules, a missing component is never read as
    # zero: every figure built from it is null, and the flags name the component that is absent.
    rows = replace_row(BALANCE_ROWS, "short_term_borrowings", None)
    rows = replace_row(rows, "current_portion_long_term_debt", None)
    rows = replace_row(rows, "short_term_investments", None)
    found = metrics(acme_with(balance=rows))
    absent = [
        "missing_input:short_term_borrowings",
        "missing_input:current_portion_long_term_debt",
    ]
    for metric_id in ("total_debt", "debt_to_equity"):
        assert found[(metric_id, "2025-12-31")].value is None, metric_id
        assert found[(metric_id, "2025-12-31")].flags == absent, metric_id
    net_debt = found[("net_debt", "2025-12-31")]
    assert net_debt.value is None
    assert net_debt.flags == [*absent, "missing_input:short_term_investments"]
    leverage = found[("net_debt_to_ebitda", "FY2025")]
    assert leverage.value is None
    assert leverage.flags == net_debt.flags
    quick = found[("quick_ratio", "2025-12-31")]
    assert quick.value is None
    assert quick.flags == ["missing_input:short_term_investments"]
    assert found[("cash_ratio", "2025-12-31")].value == approx(150 / 350)  # needs neither


# Opening balances and repeated statements --------------------------------------------------


def test_an_ambiguous_opening_balance_is_null_and_named_not_averaged() -> None:
    rows = [*BALANCE_ROWS, row("total_assets", {"2025-12-31": 2000, "2024-12-31": 999})]
    found = metrics(acme_with(balance=rows))
    # Two total_assets rows make the closing balance ambiguous too, so look at the opening alone.
    assert found[("roa", "FY2025")].flags == ["ambiguous_input:total_assets"]
    only_opening = [
        *replace_row(BALANCE_ROWS, "total_assets", row("total_assets", {"2025-12-31": 2000})),
        row("total_assets", {"2024-12-31": 1600}),
        row("total_assets", {"2024-12-31": 1700}),
    ]
    roa = metrics(acme_with(balance=only_opening))[("roa", "FY2025")]
    assert roa.value is None
    assert roa.flags == ["ambiguous_input:total_assets@opening"]


def test_two_statements_of_one_type_make_every_input_ambiguous_until_one_is_chosen() -> None:
    parent_only = build(
        "parent-balance", StatementType.BALANCE, [closing(2025), closing(2024)], BALANCE_ROWS
    )
    both = [*acme(), parent_only]
    found = metrics(both)
    assert found[("current_ratio", "2025-12-31")].value is None
    assert found[("current_ratio", "2025-12-31")].flags == [
        "ambiguous_input:total_current_assets",
        "ambiguous_input:total_current_liabilities",
    ]
    chosen = metrics(primary_statements(both))
    assert chosen[("current_ratio", "2025-12-31")].value == approx(800 / 350)
    assert chosen[("current_ratio", "2025-12-31")].flags == []


def test_the_primary_statement_of_each_type_is_the_first_in_document_order() -> None:
    income, balance = acme()
    later = build("later-balance", StatementType.BALANCE, [closing(2025)], BALANCE_ROWS)
    chosen = primary_statements([income, balance, later])
    assert [s.id for s in chosen] == ["acme-income", "acme-balance"]
    assert [s.id for s in primary_statements([later, balance, income])] == [
        "later-balance",
        "acme-income",
    ]
