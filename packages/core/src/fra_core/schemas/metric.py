"""Computed metric values.

Each value keeps the cells it came from, with their provenance, so the UI source view and the
grounding checker can walk from a displayed number back to a page region.
"""

from __future__ import annotations

import math
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fra_core.schemas.statement import Provenance


class MetricUnit(StrEnum):
    RATIO = "ratio"
    """A proportion, stored as a fraction. Rendered as a percentage."""

    CURRENCY = "currency"
    DAYS = "days"
    TIMES = "times"
    PER_SHARE = "per_share"


class MetricInput(BaseModel):
    """One printed cell a metric was computed from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    statement_id: str
    line_item_id: str
    canonical_id: str
    period_key: str
    reported: Decimal = Field(description="As printed, before the statement scale")
    scale: int
    currency: str
    provenance: Provenance


class MetricValue(BaseModel):
    """One metric for one period."""

    model_config = ConfigDict(extra="forbid")

    metric_id: str
    period_key: str
    value: float | None = Field(
        default=None,
        description="None when the metric is undefined for this period, with the reason in "
        "flags. Never a sentinel, never an infinity. A value has at least one input cell.",
    )
    unit: MetricUnit
    formula: str = Field(description="The formula as text, for the source view")
    inputs: dict[str, list[MetricInput]] = Field(
        default_factory=dict,
        description="Role to the cells used, for example {'numerator': [...]}. A role whose "
        "input is missing is absent and named in flags.",
    )
    formula_version: str
    flags: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(
        default_factory=list,
        description="Caveat ids of the statements behind a currency or per-share value. "
        "Ratios, times and days carry none.",
    )

    @model_validator(mode="after")
    def _value_or_reason(self) -> MetricValue:
        where = f"metric {self.metric_id!r} for {self.period_key!r}"
        if self.value is None and not self.flags:
            msg = f"{where} has no value and no flag saying why"
            raise ValueError(msg)
        if self.value is not None and not math.isfinite(self.value):
            msg = f"{where} has a value that is not finite: {self.value}"
            raise ValueError(msg)
        if self.value is not None and not any(self.inputs.values()):
            msg = f"{where} has a value and no input cells to trace it to"
            raise ValueError(msg)
        return self

    @property
    def is_defined(self) -> bool:
        return self.value is not None
