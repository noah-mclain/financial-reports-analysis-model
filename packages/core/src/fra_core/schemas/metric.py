"""Computed metric values.

Each value keeps the line item ids it came from, for the UI source view and the grounding
checker.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class MetricUnit(StrEnum):
    RATIO = "ratio"
    """A proportion, stored as a fraction. Rendered as a percentage."""

    CURRENCY = "currency"
    DAYS = "days"
    TIMES = "times"
    PER_SHARE = "per_share"


class MetricValue(BaseModel):
    """One metric for one period."""

    model_config = ConfigDict(extra="forbid")

    metric_id: str
    period_key: str
    value: float | None = Field(
        default=None,
        description="None when the metric is undefined for this period, with the reason in "
        "flags. Never a sentinel, never an infinity.",
    )
    unit: MetricUnit
    inputs: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Role to line item ids, for example {'numerator': ['li_42']}",
    )
    formula_version: str
    flags: list[str] = Field(default_factory=list)

    @property
    def is_defined(self) -> bool:
        return self.value is not None
