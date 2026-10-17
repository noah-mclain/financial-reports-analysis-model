"""Public descriptors share the existing metric registry's unit and period authority."""

from fra_analytics import METRIC_IDS, metric_descriptor
from fra_analytics.metrics.registry import REGISTRY
from fra_core.schemas import MetricUnit, StatementType


def test_descriptors_cover_registry_without_new_formulas() -> None:
    for spec in REGISTRY:
        descriptor = metric_descriptor(spec.id)
        assert descriptor is not None
        assert (descriptor.metric_id, descriptor.unit, descriptor.anchor) == (
            spec.id,
            spec.unit,
            spec.anchor,
        )
    assert all(metric_descriptor(key) is not None for key in METRIC_IDS)
    margin = metric_descriptor("net_margin")
    assert (
        margin is not None
        and margin.unit is MetricUnit.RATIO
        and margin.anchor is StatementType.INCOME
    )
    assert metric_descriptor("unknown") is None
