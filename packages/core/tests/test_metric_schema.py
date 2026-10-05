"""Metric metadata shared by analytics and display."""

import json
from typing import get_args

import pytest
from pydantic import ValidationError

from fra_core.schemas.caveat import CaveatId
from fra_core.schemas.metric import MetricUnit, MetricValue


def metric_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "metric_id": "revenue",
        "period_key": "2025-12-31",
        "value": 1234.5,
        "unit": MetricUnit.CURRENCY,
        "inputs": {"value": ["li_revenue"]},
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
    payload = metric_payload(
        value=value,
        caveats=["currency_from_domicile"],
        inputs={"numerator": ["li_revenue"], "denominator": ["li_shares"]},
    )
    original_payload = json.loads(json.dumps(payload))

    metric = MetricValue.model_validate_json(json.dumps(payload))
    reloaded = MetricValue.model_validate_json(metric.model_dump_json())

    assert metric.model_dump(mode="json") == original_payload
    assert reloaded == metric
    assert metric.inputs == original_payload["inputs"]
    assert payload == original_payload
    assert metric.is_defined is (value is not None)
