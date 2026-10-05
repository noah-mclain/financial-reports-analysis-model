"""Signed margins from exact source values, with explicit undefined results.

Each result carries the printed cells it was computed from, with their provenance, so a value
traces to its page regions without the Statement. Supplied canonical mappings are authoritative
here; these primitives neither map labels nor approve documents. This is the only place the
margin formulas are written: the registry takes its margins from ``compute_margins``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fra_analytics.metrics.division import divide
from fra_analytics.policy import Policy
from fra_core.schemas.metric import MetricInput, MetricUnit, MetricValue
from fra_core.schemas.statement import Cell, LineItem, PeriodKind, Statement, StatementType
from fra_core.taxonomy import load_taxonomy

_MARGINS = (
    ("gross_margin", "gross_profit"),
    ("operating_margin", "operating_income"),
    ("net_margin", "net_income"),
)
MARGIN_IDS = tuple(metric_id for metric_id, _ in _MARGINS)
_REVENUE = "revenue"


@dataclass(frozen=True)
class _Input:
    value: Decimal | None
    cells: list[MetricInput]
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
        denominator = _resolve(statement, _REVENUE, period.key, rows, cells)
        for metric_id, canonical_id in _MARGINS:
            numerator = _resolve(statement, canonical_id, period.key, rows, cells)
            flags = [*statement.flags, *numerator.flags, *denominator.flags]
            ratio = divide(
                numerator.value,
                denominator.value,
                negative=policy.negative_margin_denominator,
                flags=flags,
            )
            results.append(
                MetricValue(
                    metric_id=metric_id,
                    period_key=period.key,
                    value=None if ratio is None else float(ratio),
                    unit=MetricUnit.RATIO,
                    formula=f"{canonical_id} / {_REVENUE}",
                    inputs={"numerator": numerator.cells, "denominator": denominator.cells},
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
    statement: Statement,
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
    used: list[MetricInput] = []
    for row in candidates:
        selected = cells.get((row.id, period_key), [])
        for cell in selected:
            flags.extend(cell.flags)
            if cell.reported is not None:
                used.append(
                    MetricInput(
                        statement_id=statement.id,
                        line_item_id=row.id,
                        canonical_id=canonical_id,
                        period_key=period_key,
                        reported=cell.reported,
                        scale=statement.scale,
                        currency=statement.currency,
                        provenance=cell.provenance,
                    )
                )
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
    return _Input(value, used, flags)
