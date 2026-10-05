"""The unit-caveat stage: scale and currency assumptions reach the metrics they affect."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from statement_builders import POLICY, acme, annual, build

from fra_analytics.frame import to_frame
from fra_analytics.metrics.registry import compute
from fra_analytics.unit_caveats import unit_notes
from fra_core.schemas import MetricUnit, MetricValue, Statement, StatementType


def metrics(statements: Sequence[Statement]) -> dict[tuple[str, str], MetricValue]:
    return {(m.metric_id, m.period_key): m for m in compute(to_frame(statements), POLICY)}


def test_a_caveat_on_the_statement_reaches_a_currency_metric() -> None:
    found = metrics(acme(caveats=["scale_assumed_units"], flags=["scale_missing"]))
    ebitda = found[("ebitda", "FY2025")]
    assert ebitda.value == 250
    assert ebitda.caveats == ["scale_assumed_units"]
    assert ebitda.flags == ["scale_missing:acme-income"]
    net_debt = found[("net_debt", "2025-12-31")]
    assert net_debt.caveats == ["scale_assumed_units"]
    assert net_debt.flags == ["scale_missing:acme-balance"]


def test_a_ratio_from_one_statement_takes_no_caveat_because_the_unit_cancels() -> None:
    found = metrics(acme(caveats=["scale_assumed_units"], flags=["scale_missing"]))
    for metric_id, period in [
        ("net_margin", "FY2025"),
        ("interest_coverage", "FY2025"),
        ("current_ratio", "2025-12-31"),
        ("debt_to_equity", "2025-12-31"),
    ]:
        assert found[(metric_id, period)].caveats == []
        assert found[(metric_id, period)].flags == []
    assert found[("net_margin", "FY2025")].value == pytest.approx(0.12)


def test_a_ratio_across_statements_carries_the_unit_flags_of_each() -> None:
    # The unit cancels only inside one statement. Across two, an assumed scale on one of them
    # decides the ratio: income assumed in units, balance printed in units, would be wrong by the
    # missing multiplier.
    found = metrics(acme(caveats=["scale_assumed_units"], flags=["scale_missing"]))
    for metric_id in ("dso", "net_debt_to_ebitda", "roa", "asset_turnover"):
        metric = found[(metric_id, "FY2025")]
        assert metric.caveats == ["scale_assumed_units"], metric_id
        assert metric.flags == [
            "scale_missing:acme-balance",
            "scale_missing:acme-income",
        ], metric_id
    assert found[("dso", "FY2025")].value == 73.0  # nothing is changed, only said


def test_a_flag_on_one_statement_reaches_the_ratios_that_read_it_across_statements() -> None:
    income, balance = acme()
    income = income.model_copy(update={"flags": ["scale_missing"]})
    found = metrics([income, balance])
    for metric_id in ("dso", "net_debt_to_ebitda"):
        assert found[(metric_id, "FY2025")].flags == ["scale_missing:acme-income"], metric_id
    # A ratio read from the balance sheet alone is untouched.
    assert found[("current_ratio", "2025-12-31")].flags == []


def test_a_caveat_reaches_only_the_metrics_built_from_that_statement() -> None:
    income, balance = acme()
    income = income.model_copy(update={"flags": ["currency_inferred"]})
    found = metrics([income, balance])
    assert found[("ebitda", "FY2025")].flags == ["currency_inferred:acme-income"]
    assert found[("net_debt", "2025-12-31")].flags == []


def test_the_scale_and_currency_flags_travel_with_a_currency_metric() -> None:
    found = metrics(acme(flags=["scale_conflict", "currency_missing"]))
    assert found[("ebitda", "FY2025")].flags == [
        "scale_conflict:acme-income",
        "currency_missing:acme-income",
    ]


def test_a_ratio_across_two_scales_is_computed_and_flagged() -> None:
    income = acme(scale=1000)[0]  # printed in thousands
    balance = acme(scale=1)[1]  # printed in single units
    ratio = metrics([income, balance])[("net_debt_to_ebitda", "FY2025")]
    assert ratio.flags == ["scale_mismatch"]
    assert ratio.value == pytest.approx(400 / 250_000)  # net debt 400 units, EBITDA 250 thousand
    assert ratio.caveats == []


def test_a_ratio_across_two_currencies_is_null_with_a_flag() -> None:
    income = acme(currency="SAR")[0]
    balance = acme(currency="USD")[1]
    ratio = metrics([income, balance])[("net_debt_to_ebitda", "FY2025")]
    assert ratio.value is None
    assert ratio.flags == ["currency_mismatch"]
    assert ratio.inputs  # the cells are still named, so a reviewer can see the clash
    assert ratio.formula == "net_debt / ebitda"


def test_a_currency_amount_across_two_scales_or_currencies_is_null_with_a_flag() -> None:
    a = to_frame(acme(scale=1000)).statement("acme-income")
    b = to_frame(acme(scale=1)).statement("acme-balance")
    scale_clash = unit_notes(MetricUnit.CURRENCY, [a, b])
    assert scale_clash.blocked
    assert scale_clash.flags == ("scale_mismatch",)
    per_share = unit_notes(MetricUnit.PER_SHARE, [a, b])
    assert per_share.blocked
    c = to_frame(acme(currency="USD")).statement("acme-income")
    currency_clash = unit_notes(MetricUnit.CURRENCY, [b, c])
    assert currency_clash.blocked
    assert currency_clash.flags == ("currency_mismatch",)


def test_statements_that_agree_block_nothing_and_a_ratio_carries_nothing() -> None:
    [income] = [s for s in to_frame(acme(caveats=["scale_assumed_units"])).statements][:1]
    same = unit_notes(MetricUnit.CURRENCY, [income, income])
    assert not same.blocked
    assert same.caveats == ("scale_assumed_units",)
    ratio = unit_notes(MetricUnit.RATIO, [income])
    assert (ratio.caveats, ratio.flags, ratio.blocked) == ((), (), False)


def test_two_statements_of_one_caveat_list_it_once_and_in_order() -> None:
    frame = to_frame(
        acme(caveats=["scale_assumed_units", "currency_inferred"], flags=["scale_missing"])
    )
    notes = unit_notes(MetricUnit.CURRENCY, list(frame.statements))
    assert notes.caveats == ("currency_inferred", "scale_assumed_units")
    assert notes.flags == ("scale_missing:acme-balance", "scale_missing:acme-income")


def test_a_statement_with_no_figures_gives_null_metrics_that_name_what_is_missing() -> None:
    empty = build("e", StatementType.INCOME, [annual(2025)], [])
    margin = metrics([empty])[("net_margin", "FY2025")]
    assert margin.value is None
    assert margin.flags == ["missing_input:net_income", "missing_input:revenue"]
    assert margin.inputs == {}
