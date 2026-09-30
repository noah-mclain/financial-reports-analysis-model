"""The outcome of one arithmetic check on a statement: a subtotal or an accounting identity."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    statement_id: str
    kind: Literal["subtotal", "balance_identity"]
    period_key: str
    status: Literal["pass", "fail", "skipped"]
    expected: Decimal | None = None
    actual: Decimal | None = None
    difference: Decimal | None = None
    tolerance: Decimal | None = None
    line_item_ids: list[str] = Field(default_factory=list)
    detail: str = ""
