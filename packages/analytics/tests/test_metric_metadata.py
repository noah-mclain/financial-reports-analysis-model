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


def test_descriptor_statements_come_from_the_taxonomy_inputs() -> None:
    def reads_balance(key: str) -> bool:
        descriptor = metric_descriptor(key)
        assert descriptor is not None
        return StatementType.BALANCE in descriptor.statements

    assert {k for k in METRIC_IDS if reads_balance(k)} >= {
        "current_ratio",
        "total_debt",
        "net_debt_to_ebitda",
        "dso",
    }
    assert not any(reads_balance(k) for k in ("net_margin", "ebitda"))
    margin = metric_descriptor("net_margin")
    assert margin is not None and margin.statements == {StatementType.INCOME}
