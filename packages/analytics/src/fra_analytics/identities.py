"""Accounting identities and subtotal ties over mapped canonical items (D6).

Each check reads the frame, never labels: a statement whose rows the mapping resolved can be
checked, and one whose rows it did not cannot. The tolerance is the one rule in
``fra_core.tolerance``: n x 0.5 reported units for n addends, which structure uses too. Figures
are compared as printed, since rounding happens at the printed unit.

A check that cannot be made is ``skipped`` with the items it lacked, never a pass.

Two rows on one item in one statement and period make a check ``skipped`` with the reason
``ambiguous_input``. The structure stage's own identity check takes the last matching row
instead, so the two can differ on a statement that repeats a total; analytics does not pick a
row it cannot justify, and the metrics treat the same case the same way. Of two statements of
one type, the caller chooses one with ``fra_analytics.frame.primary_statements``: each
statement is checked on its own rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from fra_analytics.frame import Frame, FrameRow, FrameStatement
from fra_core.schemas import CheckResult, StatementType
from fra_core.taxonomy.loader import CanonicalItem, load_taxonomy
from fra_core.tolerance import rounding_tolerance, within_rounding

_ASSETS = "total_assets"
_LIABILITIES_AND_EQUITY = "total_liabilities_and_equity"


@dataclass(frozen=True)
class Tie:
    """``total`` equals the sum of ``parts``. An expense item (natural sign minus) is added as
    a magnitude, whichever sign it is printed with."""

    statement_type: StatementType
    total: str
    parts: tuple[str, ...]
    subtractions: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return f"{self.total} = {' + '.join(self.parts)}" + "".join(
            f" - {s}" for s in self.subtractions
        )


TIES: tuple[Tie, ...] = (
    Tie(
        StatementType.BALANCE, "total_assets", ("total_current_assets", "total_non_current_assets")
    ),
    Tie(
        StatementType.BALANCE,
        "total_liabilities",
        ("total_current_liabilities", "total_non_current_liabilities"),
    ),
    Tie(
        StatementType.BALANCE,
        "total_equity",
        ("equity_attributable_parent", "non_controlling_interests"),
    ),
    Tie(StatementType.INCOME, "gross_profit", ("revenue",), ("cost_of_revenue",)),
    Tie(
        StatementType.INCOME,
        "net_income",
        ("net_income_attributable_parent", "net_income_attributable_nci"),
    ),
)

_Kind = Literal["subtotal", "balance_identity"]


def _canonical(canonical_id: str) -> CanonicalItem:
    item = load_taxonomy().by_id(canonical_id)
    if item is None:
        msg = f"a tie names {canonical_id!r}, which is not a canonical item"
        raise ValueError(msg)
    return item


def _signed(canonical_id: str, row: FrameRow, *, subtract: bool) -> Decimal:
    if _canonical(canonical_id).natural_sign == "-":
        return -abs(row.reported) if subtract else abs(row.reported)
    return -row.reported if subtract else row.reported


def _result(
    statement: FrameStatement,
    kind: _Kind,
    target: str,
    period_key: str,
    status: Literal["pass", "fail", "skipped"],
    **fields: object,
) -> CheckResult:
    return CheckResult.model_validate(
        {
            "id": f"{statement.id}:{kind}:{target}:{period_key}",
            "statement_id": statement.id,
            "kind": kind,
            "period_key": period_key,
            "status": status,
            **fields,
        }
    )


def _check(
    frame: Frame,
    statement: FrameStatement,
    period_key: str,
    kind: _Kind,
    target: str,
    total: str,
    adds: Sequence[str],
    subtracts: Sequence[str],
    text: str,
) -> CheckResult | None:
    """One total against its addends in one statement and period; None when the total is not
    printed and the check is a tie (an identity is always reported)."""
    period = next(p for p in statement.periods if p.key == period_key)
    for item in (total, *adds, *subtracts):
        _canonical(item)  # a typo in a tie fails here, whatever the statement holds

    def rows(item: str) -> list[FrameRow]:
        return [
            r
            for r in frame.lookup(
                item, statement.type, end_date=period.end_date, months=period.months
            )
            if r.statement_id == statement.id
        ]

    found = {item: rows(item) for item in (total, *adds, *subtracts)}
    used = [r.line_item_id for item in found for r in found[item]]
    ambiguous = [item for item, r in found.items() if len(r) > 1]
    absent = [item for item, r in found.items() if not r]
    if kind == "subtotal" and total in absent:
        return None
    if ambiguous or absent:
        reasons = [f"ambiguous_input:{i}" for i in ambiguous] + [
            f"missing_input:{i}" for i in absent
        ]
        return _result(
            statement,
            kind,
            target,
            period_key,
            "skipped",
            line_item_ids=used,
            detail=";".join(reasons),
        )
    actual = found[total][0].reported
    expected = sum(
        [_signed(i, found[i][0], subtract=False) for i in adds]
        + [_signed(i, found[i][0], subtract=True) for i in subtracts],
        Decimal(0),
    )
    addends = len(adds) + len(subtracts)
    difference = actual - expected
    return _result(
        statement,
        kind,
        target,
        period_key,
        "pass" if within_rounding(difference, addends) else "fail",
        expected=expected,
        actual=actual,
        difference=difference,
        tolerance=rounding_tolerance(addends),
        line_item_ids=used,
        detail=text,
    )


def check_identities(frame: Frame) -> list[CheckResult]:
    """Assets against liabilities plus equity on every balance sheet, and every subtotal tie
    whose total is printed, for every period."""
    results: list[CheckResult] = []
    for statement in frame.statements:
        for period in statement.periods:
            if statement.type is StatementType.BALANCE:
                results.append(_identity(frame, statement, period.key))
            for tie in TIES:
                if tie.statement_type is not statement.type:
                    continue
                result = _check(
                    frame,
                    statement,
                    period.key,
                    "subtotal",
                    tie.total,
                    tie.total,
                    tie.parts,
                    tie.subtractions,
                    tie.text,
                )
                if result is not None:
                    results.append(result)
    return results


def _identity(frame: Frame, statement: FrameStatement, period_key: str) -> CheckResult:
    """Total assets against the printed total of liabilities and equity when there is one, else
    against total liabilities plus total equity, as the structure stage does."""
    period = next(p for p in statement.periods if p.key == period_key)
    printed = [
        r
        for r in frame.lookup(
            _LIABILITIES_AND_EQUITY, statement.type, end_date=period.end_date, months=period.months
        )
        if r.statement_id == statement.id
    ]
    parts = (_LIABILITIES_AND_EQUITY,) if printed else ("total_liabilities", "total_equity")
    result = _check(
        frame,
        statement,
        period_key,
        "balance_identity",
        "balance",
        _ASSETS,
        parts,
        (),
        f"{_ASSETS} = {' + '.join(parts)}",
    )
    assert result is not None  # an identity is always reported
    return result
