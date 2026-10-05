"""Metric values: every number names the cells it came from, and a null says why."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from fra_core.schemas import BBox, MetricInput, MetricUnit, MetricValue, Provenance


def _input() -> MetricInput:
    return MetricInput(
        statement_id="s1",
        line_item_id="p1-t0-r2",
        canonical_id="revenue",
        period_key="FY2025",
        reported=Decimal("1000"),
        scale=1000,
        currency="SAR",
        provenance=Provenance(
            page_no=1,
            bbox=BBox(left=0, top=0, right=1, bottom=1),
            table_ref="#/tables/0",
            row=2,
            col=1,
        ),
    )


def test_a_metric_round_trips_with_its_input_cells() -> None:
    metric = MetricValue(
        metric_id="net_margin",
        period_key="FY2025",
        value=0.1,
        unit=MetricUnit.RATIO,
        formula="net_income / revenue",
        inputs={"denominator": [_input()]},
        formula_version="1",
    )
    assert MetricValue.model_validate_json(metric.model_dump_json()) == metric
    assert metric.is_defined


def test_a_null_value_without_a_reason_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"net_margin.*FY2025.*no value and no flag"):
        MetricValue(
            metric_id="net_margin",
            period_key="FY2025",
            value=None,
            unit=MetricUnit.RATIO,
            formula="net_income / revenue",
            formula_version="1",
        )


@pytest.mark.parametrize("bad", [float("inf"), float("nan")])
def test_a_non_finite_value_is_rejected(bad: float) -> None:
    with pytest.raises(ValidationError, match="not finite"):
        MetricValue(
            metric_id="net_margin",
            period_key="FY2025",
            value=bad,
            unit=MetricUnit.RATIO,
            formula="net_income / revenue",
            inputs={"denominator": [_input()]},
            formula_version="1",
        )


def test_a_value_with_no_input_cells_is_rejected() -> None:
    # A displayed number must trace to cells (blueprint rule): no inputs, no value.
    with pytest.raises(ValidationError, match=r"net_margin.*FY2025.*no input cells"):
        MetricValue(
            metric_id="net_margin",
            period_key="FY2025",
            value=0.1,
            unit=MetricUnit.RATIO,
            formula="net_income / revenue",
            formula_version="1",
        )
    with pytest.raises(ValidationError, match="no input cells"):
        MetricValue(
            metric_id="net_margin",
            period_key="FY2025",
            value=0.1,
            unit=MetricUnit.RATIO,
            formula="net_income / revenue",
            inputs={"denominator": []},
            formula_version="1",
        )
