"""Passed, or held for a person to look at (spec 12, Data flow step 10a).

A statement passes when its checks hold, something vouches for its figures and nothing on it
is flagged as unsure. Passing is not proof: ``checked_cells`` says how many figures a passing
check covers, and the rest were read and nothing more.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from fra_core.schemas import Cell, CheckResult, Statement, StatementType

# In the order the reasons are listed.
CRITICAL_CELL_FLAGS = (
    "unparsed",
    "implausible_magnitude",
    "digit_suspect",
    "period_outlier",
    "row_misaligned",
    "row_realigned",
    "ambiguous_separator",
)
_HOLD_FLAGS = (
    "period_unbound",
    "scale_conflict",
    "currency_conflict",
    "currency_missing",
    "row_alignment_unresolved",
)
_WARNING_FLAGS = ("scale_missing", "currency_from_domicile", "currency_inferred")
_WARNING_CELL_FLAGS = ("label_merged", "blank_confirmed")
_FAILED = (
    ("subtotal", "subtotal_failed"),
    ("balance_identity", "identity_failed"),
    ("net_profit_tie", "tie_failed"),
)


class StatementReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    statement_id: str
    status: Literal["passed", "needs_review"]
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    numeric_cells: int = Field(ge=0)
    checked_cells: int = Field(ge=0)
    flagged_cells: int = Field(ge=0)


def _lost(cell: Cell) -> bool:
    return "numbers_missing" in cell.flags and "blank_confirmed" not in cell.flags


def is_critical(cell: Cell) -> bool:
    return _lost(cell) or any(f in CRITICAL_CELL_FLAGS for f in cell.flags)


def review_statement(
    statement: Statement, checks: Sequence[CheckResult], *, primary: bool
) -> StatementReview:
    cells = [(item.id, c) for item in statement.line_items for c in item.cells]
    reasons = [
        reason
        for kind, reason in _FAILED
        if any(c.kind == kind and c.status == "fail" for c in checks)
    ]
    identity = [c for c in checks if c.kind == "balance_identity"]
    if statement.type is StatementType.BALANCE and not any(c.status == "fail" for c in identity):
        skipped = sorted({c.detail for c in identity if c.status == "skipped" and c.detail})
        if not identity or skipped or any(c.status != "pass" for c in identity):
            reasons.append(":".join(["identity_not_checked", *skipped[:1]]))
    if not any(c.status == "pass" for c in checks):
        reasons.append("unchecked")
    lost = sum(1 for _, c in cells if _lost(c))
    if lost:
        reasons.append(f"numbers_missing:{lost}")
    counts = Counter(f for _, c in cells for f in c.flags)
    reasons += [f"{flag}:{counts[flag]}" for flag in CRITICAL_CELL_FLAGS if counts[flag]]
    reasons += [f for f in statement.flags if f.split(":")[0] in _HOLD_FLAGS]
    if not primary:
        reasons = ["duplicate_statement"]
    warnings = [f for f in statement.flags if f in _WARNING_FLAGS]
    warnings += [f"{flag}:{counts[flag]}" for flag in _WARNING_CELL_FLAGS if counts[flag]]
    vouched = {(i, c.period_key) for c in checks if c.status == "pass" for i in c.line_item_ids}
    numeric = [(i, c) for i, c in cells if c.reported is not None]
    return StatementReview(
        statement_id=statement.id,
        status="needs_review" if reasons else "passed",
        reasons=reasons,
        warnings=warnings,
        numeric_cells=len(numeric),
        checked_cells=sum(1 for i, c in numeric if (i, c.period_key) in vouched),
        flagged_cells=sum(1 for _, c in cells if is_critical(c)),
    )
