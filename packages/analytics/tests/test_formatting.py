"""Display uses the stored metric, with a private decimal rounding policy."""

from decimal import ROUND_UP, Decimal, localcontext

import pytest

from fra_analytics.formatting import format_amount, format_metric
from fra_core.schemas import BBox, MetricInput, MetricUnit, MetricValue, Provenance


def value(unit: MetricUnit, number: float | None) -> MetricValue:
    return MetricValue(
        metric_id="test",
        period_key="p",
        value=number,
        unit=unit,
        formula="test",
        formula_version="1",
        flags=["undefined"] if number is None else [],
        inputs={
            "x": [
                MetricInput(
                    statement_id="s",
                    line_item_id="r",
                    canonical_id="revenue",
                    period_key="p",
                    reported=Decimal("1"),
                    scale=1,
                    currency="EGP",
                    provenance=Provenance(
                        page_no=1,
                        bbox=BBox(left=0, top=0, right=1, bottom=1),
                        table_ref="t",
                        row=0,
                        col=0,
                    ),
                )
            ]
        }
        if number is not None
        else {},
    )


@pytest.mark.parametrize(
    "unit,number,en,ar",
    [
        (MetricUnit.RATIO, 0.1245, "12.4%", "12.4%"),
        (MetricUnit.TIMES, 2.675, "2.68×", "2.68×"),
        (MetricUnit.DAYS, 2.5, "2 days", "2 يوم"),
        (MetricUnit.CURRENCY, -1234.5, "-1,234 EGP", "-1,234 EGP"),
        (MetricUnit.PER_SHARE, -1.257, "(1.257) EGP per share", "(1.257) EGP للسهم"),
        (MetricUnit.PER_SHARE, 0.0000125, "0.0000125 EGP per share", "0.0000125 EGP للسهم"),
    ],
)
def test_units_and_decimal_ties(unit: MetricUnit, number: float, en: str, ar: str) -> None:
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_UP
        assert format_metric(value(unit, number)) == en
        assert format_metric(value(unit, number), "ar") == ar


def test_extremes_null_and_printed_precision() -> None:
    assert format_metric(value(MetricUnit.RATIO, None)) is None
    assert format_metric(value(MetricUnit.RATIO, 1e308)) == "1" + "0" * 310 + ".0%"
    assert format_metric(value(MetricUnit.TIMES, 5e-324)) == "0.00×"
    assert format_amount(Decimal("-1234567.123400")) == "(1,234,567.123400)"
