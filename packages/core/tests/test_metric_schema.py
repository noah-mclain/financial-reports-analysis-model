"""Metric values: every number names the cells it came from, and a null says why."""

import json
from decimal import Decimal
from typing import get_args

import pytest
from pydantic import ValidationError

from fra_core.schemas import BBox, MetricInput, MetricUnit, MetricValue, Provenance
from fra_core.schemas.caveat import CaveatId


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


def metric_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "metric_id": "revenue",
        "period_key": "FY2025",
        "value": 1234.5,
        "unit": MetricUnit.CURRENCY,
        "formula": "revenue",
        "inputs": {"value": [_input().model_dump(mode="json")]},
        "formula_version": "1",
        "flags": ["source_verified"],
    }
    payload.update(overrides)
    return payload


def test_caveated_currency_metric_round_trips_as_json() -> None:
    payload = metric_payload(caveats=["scale_assumed_units"])

    loaded = MetricValue.model_validate_json(json.dumps(payload))

    assert loaded.model_dump(mode="json") == payload


def test_legacy_metric_json_defaults_to_no_caveats() -> None:
    loaded = MetricValue.model_validate_json(json.dumps(metric_payload()))

    assert loaded.caveats == []


@pytest.mark.parametrize("caveat_id", get_args(CaveatId))
def test_each_defined_caveat_id_round_trips(caveat_id: CaveatId) -> None:
    payload = metric_payload(caveats=[caveat_id])

    loaded = MetricValue.model_validate_json(json.dumps(payload))

    assert loaded.caveats == [caveat_id]
    assert loaded.model_dump(mode="json")["caveats"] == [caveat_id]


def test_unknown_caveat_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MetricValue.model_validate({**metric_payload(), "caveats": ["unknown_caveat"]})


def test_default_caveat_lists_are_independent() -> None:
    first = MetricValue.model_validate(metric_payload())
    second = MetricValue.model_validate(metric_payload())

    first.caveats.append("currency_inferred")

    assert second.caveats == []


@pytest.mark.parametrize("value", [1234.5, None])
def test_defined_and_undefined_metrics_preserve_fields_and_inputs(value: float | None) -> None:
    cell = _input().model_dump(mode="json")
    payload = metric_payload(
        value=value,
        caveats=["currency_from_domicile"],
        inputs={"numerator": [cell], "denominator": [cell]},
    )
    original_payload = json.loads(json.dumps(payload))

    metric = MetricValue.model_validate_json(json.dumps(payload))
    reloaded = MetricValue.model_validate_json(metric.model_dump_json())

    assert metric.model_dump(mode="json") == original_payload
    assert reloaded == metric
    assert payload == original_payload
    assert metric.is_defined is (value is not None)
