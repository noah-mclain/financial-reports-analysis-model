"""Display stored metrics without recomputing them or inheriting caller rounding."""

from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from fra_core.presentation import Language
from fra_core.schemas import MetricUnit, MetricValue


def format_amount(value: Decimal) -> str:
    """Preserve every printed decimal digit, grouped, with negative parentheses."""
    if not value.is_finite():
        raise ValueError("amount must be finite")
    text = f"{value.copy_abs():,f}"
    return f"({text})" if value < 0 else text


def format_metric(metric: MetricValue, language: Language = "en") -> str | None:
    if language not in ("en", "ar"):
        raise ValueError(f"unsupported display language: {language}")
    if metric.value is None:
        return None
    value = Decimal(str(metric.value))
    if not value.is_finite():
        raise ValueError(f"nonfinite metric: {metric.metric_id}")
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        if metric.unit is MetricUnit.RATIO:
            return f"{value * 100:.1f}%"
        if metric.unit is MetricUnit.TIMES:
            return f"{value:.2f}×"
        if metric.unit is MetricUnit.DAYS:
            return f"{value:.0f} {'يوم' if language == 'ar' else 'days'}"
        currencies = sorted({i.currency for cells in metric.inputs.values() for i in cells})
        currency = "/".join(currencies)
        if metric.unit is MetricUnit.PER_SHARE:
            suffix = "للسهم" if language == "ar" else "per share"
            return f"{format_amount(value)} {currency} {suffix}".strip()
        return f"{value:,.0f} {currency}".strip()
