"""Signed margins from exact source values, with explicit undefined results.

Input IDs are scoped to the source Statement. Keep that Statement alongside these results
to resolve each role and period back to its source cells and page regions. Supplied canonical
mappings are authoritative here; these primitives neither map labels nor approve documents.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    Underflow,
)
from math import isfinite

from fra_analytics.policy import Policy
from fra_core.schemas.metric import MetricUnit, MetricValue
from fra_core.schemas.statement import Cell, LineItem, PeriodKind, Statement, StatementType
from fra_core.taxonomy import load_taxonomy

_MARGINS = (
    ("gross_margin", "gross_profit"),
    ("operating_margin", "operating_income"),
    ("net_margin", "net_income"),
)
_REVENUE = "revenue"


@dataclass(frozen=True)
class _Input:
    value: Decimal | None
    ids: list[str]
    flags: list[str]


def compute_margins(statement: Statement, *, policy: Policy) -> list[MetricValue]:
    """Return three fractional margins per duration column, without changing the source.

    Same-statement scale and currency cancel. No annualization, sign correction, parent-profit
    substitution, mapping confidence threshold or currency-amount caveat is introduced.
    """
    if not isinstance(policy, Policy):
        raise ValueError("policy must be a Policy")
    rows, cells = _index(statement)
    results = []
    for period in statement.periods:
        denominator = _resolve(_REVENUE, period.key, rows, cells)
        for metric_id, canonical_id in _MARGINS:
            numerator = _resolve(canonical_id, period.key, rows, cells)
            flags = [*statement.flags, *numerator.flags, *denominator.flags]
            value = _divide(numerator.value, denominator.value, policy, flags)
            results.append(
                MetricValue(
                    metric_id=metric_id,
                    period_key=period.key,
                    value=value,
                    unit=MetricUnit.RATIO,
                    inputs={"numerator": numerator.ids, "denominator": denominator.ids},
                    formula_version="1",
                    flags=list(dict.fromkeys(flags)),
                )
            )
    return results


def _index(
    statement: Statement,
) -> tuple[dict[str, list[LineItem]], dict[tuple[str, str], list[Cell]]]:
    if not isinstance(statement, Statement):
        raise ValueError("statement must be a Statement")
    if statement.type is not StatementType.INCOME:
        raise ValueError(f"statement {statement.id!r} must be income, got {statement.type!r}")
    periods: set[str] = set()
    if not statement.periods:
        raise ValueError(f"statement {statement.id!r} has no periods")
    for period in statement.periods:
        if period.kind is not PeriodKind.DURATION or period.months is None:
            raise ValueError(f"period {period.key!r} must be a duration with months")
        if period.key in periods:
            raise ValueError(f"duplicate period key {period.key!r}")
        periods.add(period.key)
    ids: set[str] = set()
    rows: dict[str, list[LineItem]] = {}
    cells: dict[tuple[str, str], list[Cell]] = {}
    for row in statement.line_items:
        if row.id in ids:
            raise ValueError(f"duplicate row id {row.id!r}")
        ids.add(row.id)
        if row.canonical_id is not None:
            rows.setdefault(row.canonical_id, []).append(row)
        for cell in row.cells:
            if cell.period_key not in periods:
                raise ValueError(f"row {row.id!r} references unknown period {cell.period_key!r}")
            if cell.reported is not None and not cell.reported.is_finite():
                raise ValueError(f"nonfinite input in row {row.id!r}, period {cell.period_key!r}")
            cells.setdefault((row.id, cell.period_key), []).append(cell)
    return rows, cells


def _resolve(
    canonical_id: str,
    period_key: str,
    rows: dict[str, list[LineItem]],
    cells: dict[tuple[str, str], list[Cell]],
) -> _Input:
    candidates = rows.get(canonical_id, [])
    flags: list[str] = []
    if not candidates:
        return _Input(None, [], [f"missing_input:{canonical_id}"])
    if len(candidates) > 1:
        flags.append(f"duplicate_input:{canonical_id}")
    item = load_taxonomy().by_id(canonical_id)
    if item is None:
        raise ValueError(f"margin input {canonical_id!r} is absent from the taxonomy")
    value = None
    for row in candidates:
        selected = cells.get((row.id, period_key), [])
        for cell in selected:
            flags.extend(cell.flags)
            amount = cell.reported
            if (
                amount is not None
                and not amount.is_zero()
                and amount.is_signed() != (item.natural_sign == "-")
            ):
                flags.append("sign_unexpected")
        if len(selected) > 1:
            flags.append(f"duplicate_cell:{row.id}:{period_key}")
        elif not selected or selected[0].reported is None:
            flags.append(f"missing_input:{canonical_id}")
        elif len(candidates) == 1:
            value = selected[0].reported
    return _Input(value, [row.id for row in candidates], flags)


def _divide(
    numerator: Decimal | None,
    denominator: Decimal | None,
    policy: Policy,
    flags: list[str],
) -> float | None:
    if denominator is not None and denominator.is_zero():
        flags.append("undefined_zero_denominator")
        return None
    if numerator is None or denominator is None:
        return None
    if denominator.is_signed():
        if policy.negative_margin_denominator == "null":
            flags.append("undefined_negative_denominator")
            return None
        flags.append("negative_base")
    # Specify all context settings and traps, rather than copying the caller's context.
    context = Context(
        prec=34,
        rounding=ROUND_HALF_EVEN,
        Emin=-999999,
        Emax=999999,
        capitals=1,
        clamp=0,
        traps=[InvalidOperation, DivisionByZero, Overflow, Underflow],
    )
    try:
        ratio = context.divide(numerator, denominator)
    except DecimalException:
        flags.append("arithmetic_result")
        return None
    value = float(ratio)
    if not isfinite(value) or (value == 0.0 and not numerator.is_zero()):
        flags.append("unrepresentable_result")
        return None
    return value
