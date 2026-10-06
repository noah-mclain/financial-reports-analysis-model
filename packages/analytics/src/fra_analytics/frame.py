"""Statements to a tidy frame: one row per mapped printed cell.

The blueprint (04, 1B.1) sketches a pandas DataFrame. The workspace has no pandas and nothing
here needs it, so the frame is a tuple of typed rows with the lookups the metrics make. Money is
``Decimal`` throughout: a row keeps the figure as printed and the same figure times the
statement scale.

A row exists only for a line item the mapping resolved (``canonical_id`` set) and a cell that
holds a figure. Rows the mapping flagged are not in the frame; each statement counts them, so a
metric whose input is missing can say the statement holds rows nobody could place.

One statement of each type. A filing can hold two of a type (consolidated and parent-only
balance sheets, say), and a metric reading an item that both print cannot tell which to use:
it is null, flagged ``ambiguous_input``, never a guess. ``primary_statements`` is the rule for
choosing: the first of each type in document order, because a filing opens with its
consolidated statements, which are the ones the metrics describe. The caller applies it before
``to_frame``; ``to_frame`` itself keeps every statement it is given.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from fra_analytics.metrics.division import arithmetic_context
from fra_core.schemas import Period, Provenance, Statement, StatementType
from fra_core.schemas.caveat import CaveatId


@dataclass(frozen=True)
class FrameStatement:
    """What the unit-caveat stage and the metrics need to know about a statement."""

    id: str
    type: StatementType
    scale: int
    currency: str
    caveats: tuple[CaveatId, ...]
    flags: tuple[str, ...]
    periods: tuple[Period, ...]
    unmapped_rows: int


@dataclass(frozen=True)
class FrameRow:
    statement_id: str
    statement_type: StatementType
    line_item_id: str
    canonical_id: str
    period: Period
    reported: Decimal
    scale: int
    value: Decimal
    currency: str
    provenance: Provenance
    flags: tuple[str, ...] = ()


def _period_order(period: Period) -> tuple[date, str, int]:
    return period.end_date, period.kind.value, period.months or 0


@dataclass(frozen=True)
class Frame:
    statements: tuple[FrameStatement, ...]
    rows: tuple[FrameRow, ...]

    def statement(self, statement_id: str) -> FrameStatement:
        for statement in self.statements:
            if statement.id == statement_id:
                return statement
        msg = f"no statement {statement_id!r} in the frame"
        raise KeyError(msg)

    def periods(self, statement_type: StatementType) -> list[Period]:
        """The distinct periods of the statements of one type, oldest first. Two columns that
        end on one date and cover one length are one period, whatever each statement calls it."""
        found = {
            (p.end_date, p.kind, p.months): p
            for s in self.statements
            if s.type is statement_type
            for p in s.periods
        }
        return sorted(found.values(), key=_period_order)

    def lookup(
        self,
        canonical_id: str,
        statement_type: StatementType,
        *,
        end_date: date,
        months: int | None,
    ) -> list[FrameRow]:
        """Every row for one item in the statements of one type, for the period that ends on
        ``end_date`` and spans ``months`` (None for a balance at that date)."""
        return [
            r
            for r in self.rows
            if r.canonical_id == canonical_id
            and r.statement_type is statement_type
            and r.period.end_date == end_date
            and r.period.months == months
        ]

    def unmapped_in(self, statement_type: StatementType) -> int:
        return sum(s.unmapped_rows for s in self.statements if s.type is statement_type)


def primary_statements(statements: Sequence[Statement]) -> list[Statement]:
    """The first statement of each type in document order, in the order the types first
    appear."""
    found: dict[StatementType, Statement] = {}
    for statement in statements:
        found.setdefault(statement.type, statement)
    return list(found.values())


@arithmetic_context()
def to_frame(statements: Sequence[Statement]) -> Frame:
    ids = [s.id for s in statements]
    repeated = sorted({i for i in ids if ids.count(i) > 1})
    if repeated:
        msg = f"statement ids must be unique, repeated: {repeated}"
        raise ValueError(msg)
    frame_statements: list[FrameStatement] = []
    rows: list[FrameRow] = []
    for statement in statements:
        periods = {p.key: p for p in statement.periods}
        frame_statements.append(
            FrameStatement(
                id=statement.id,
                type=statement.type,
                scale=statement.scale,
                currency=statement.currency,
                caveats=tuple(c.id for c in statement.caveats),
                flags=tuple(statement.flags),
                periods=tuple(statement.periods),
                unmapped_rows=sum(1 for i in statement.line_items if i.mapping_flag is not None),
            )
        )
        for item in statement.line_items:
            if item.canonical_id is None:
                continue
            rows.extend(
                FrameRow(
                    statement_id=statement.id,
                    statement_type=statement.type,
                    line_item_id=item.id,
                    canonical_id=item.canonical_id,
                    period=periods[cell.period_key],
                    reported=cell.reported,
                    scale=statement.scale,
                    value=cell.reported * statement.scale,
                    currency=statement.currency,
                    provenance=cell.provenance,
                    flags=tuple(cell.flags),
                )
                for cell in item.cells
                if cell.reported is not None
            )
    return Frame(statements=tuple(frame_statements), rows=tuple(rows))
