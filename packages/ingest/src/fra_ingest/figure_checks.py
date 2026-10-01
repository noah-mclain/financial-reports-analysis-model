"""Misreads the sums can point at, or that no sum covers (spec 12, Data flow step 10).

Nothing here changes a figure. A cell is flagged, and a check's detail says what would settle
it; the read value stays as it was read.
"""

from __future__ import annotations

from decimal import Decimal

from fra_core.schemas import Cell, CheckResult, LineItem, Statement

OUTLIER_RATIO = 1000
# Zeros that may sit between a lost leading digit and the digits read.
_ZEROS_LOST = 2
# A fraction this large is no per-share figure or ratio the cues missed.
_LARGE_FRACTION = 1000
# Above this share of fractional amounts, the statement is printed with decimals.
FRACTION_SHARE = Decimal("0.2")
_HALF = Decimal("0.5")
_NOT_COMPARED = {"per_share", "percent", "implausible_magnitude"}


def single_digit_place(difference: Decimal) -> int | None:
    """k when the difference is one digit times 10^k, else None."""
    magnitude = abs(difference)
    if magnitude == 0 or magnitude != magnitude.to_integral_value():
        return None
    digits = str(int(magnitude))
    return len(digits) - 1 if len(digits.rstrip("0")) == 1 else None


def one_digit_apart(a: Decimal, b: Decimal) -> bool:
    """Two whole figures of the same sign and length that differ in exactly one digit."""
    if a == b or (a < 0) != (b < 0):
        return False
    if a != a.to_integral_value() or b != b.to_integral_value():
        return False
    x, y = str(abs(int(a))), str(abs(int(b)))
    return len(x) == len(y) and sum(1 for p, q in zip(x, y, strict=True) if p != q) == 1


def leading_digit_lost(read: Decimal, settled: Decimal) -> bool:
    """Whether ``read`` is ``settled`` with its leading digit lost: 302,414,061 read as
    2,414,061. The two differ by one digit that sits just above the digits read, with at most
    two zeros between (3,014,061 read as 14,061). Nothing is "lost" from a zero."""
    if read == 0 or (read < 0) != (settled < 0) or abs(settled) <= abs(read):
        return False
    if read != read.to_integral_value() or settled != settled.to_integral_value():
        return False
    place = single_digit_place(abs(settled) - abs(read))
    digits = len(str(abs(int(read))))
    return place is not None and digits <= place <= digits + _ZEROS_LOST


def _total_id(check: CheckResult) -> str:
    return check.line_item_ids[0] if check.kind == "balance_identity" else check.line_item_ids[-1]


def _with_flag(statement: Statement, cells: set[tuple[str, str]], flag: str) -> Statement:
    if not cells:
        return statement
    items = [
        item.model_copy(
            update={
                "cells": [
                    c.model_copy(update={"flags": [*c.flags, flag]})
                    if (item.id, c.period_key) in cells and flag not in c.flags
                    else c
                    for c in item.cells
                ]
            }
        )
        for item in statement.line_items
    ]
    return statement.model_copy(update={"line_items": items})


def diagnose_digits(
    statement: Statement, checks: list[CheckResult]
) -> tuple[Statement, list[CheckResult]]:
    """Name the cells where one misread digit explains a failed check."""
    by_id = {item.id: item for item in statement.line_items}
    vouched = {(i, c.period_key) for c in checks if c.status == "pass" for i in c.line_item_ids}
    candidates: dict[str, dict[str, Decimal]] = {}
    places: dict[str, int] = {}
    for check in checks:
        if check.status != "fail" or check.difference is None:
            continue
        place = single_digit_place(check.difference)
        if place is None:
            continue
        places[check.id] = place
        total = _total_id(check)
        found: dict[str, Decimal] = {}
        for item_id in check.line_item_ids:
            item = by_id.get(item_id)
            value = item.value_for(check.period_key) if item is not None else None
            if value is None or (item_id, check.period_key) in vouched:
                continue
            settled = value - check.difference if item_id == total else value + check.difference
            if one_digit_apart(value, settled) or leading_digit_lost(value, settled):
                found[item_id] = settled
        candidates[check.id] = found
    period_of = {c.id: c.period_key for c in checks}
    sole = {
        (next(iter(found)), period_of[check_id])
        for check_id, found in candidates.items()
        if len(found) == 1
    }
    suspects: set[tuple[str, str]] = set()
    updated: list[CheckResult] = []
    for check in checks:
        if check.id not in places:
            updated.append(check)
            continue
        found = candidates[check.id]
        named = {i: v for i, v in found.items() if (i, check.period_key) in sole} or found
        suspects.update((i, check.period_key) for i in named)
        details = [check.detail, f"single_digit:10^{places[check.id]}"]
        if len(named) == 1:
            item_id, settled = next(iter(named.items()))
            details.append(f"suspect:{item_id}={settled}")
        updated.append(check.model_copy(update={"detail": "; ".join(d for d in details if d)}))
    return _with_flag(statement, suspects, "digit_suspect"), updated


def _compared(cell: Cell) -> bool:
    return bool(cell.reported) and not _NOT_COMPARED & set(cell.flags)


def flag_period_outliers(statement: Statement) -> Statement:
    """Flag the values of a row whose largest is ``OUTLIER_RATIO`` times its smallest or more."""
    outliers: set[tuple[str, str]] = set()
    for item in statement.line_items:
        cells = [c for c in item.cells if _compared(c)]
        sizes = [abs(c.reported) for c in cells if c.reported is not None]
        if len(sizes) >= 2 and max(sizes) >= OUTLIER_RATIO * min(sizes):
            outliers.update((item.id, c.period_key) for c in cells)
    return _with_flag(statement, outliers, "period_outlier")


def flag_fractions(statement: Statement) -> Statement:
    """Flag a figure with a fractional part in a statement printed in whole amounts: a decimal
    mark OCR put into a number (132,705,608 read as 1327.5608). Per-share and percentage cells
    are not amounts, and a statement printed with decimals throughout is left alone. So is a
    row of small fractions with no whole amount beside them: that is a per-share figure or a
    ratio the label cues did not name, not a misread."""
    fractions: set[tuple[str, str]] = set()
    total = 0
    for item in statement.line_items:
        amounts = [
            c
            for c in item.cells
            if c.reported is not None and not {"per_share", "percent"} & set(c.flags)
        ]
        total += len(amounts)
        parts = [c for c in amounts if c.reported is not None and c.reported % 1 != 0]
        beside_whole = len(parts) < len(amounts)
        for c in parts:
            if beside_whole or (c.reported is not None and abs(c.reported) >= _LARGE_FRACTION):
                fractions.add((item.id, c.period_key))
    if len(fractions) > FRACTION_SHARE * total:
        return statement
    return _with_flag(statement, fractions, "fraction_among_whole")


def _first_valued(statement: Statement) -> LineItem | None:
    return next(
        (i for i in statement.line_items if any(c.reported is not None for c in i.cells)), None
    )


def check_net_profit_tie(income: Statement, comprehensive: Statement) -> list[CheckResult]:
    """The first valued row of comprehensive income against the rows of the income statement:
    one of them must equal it in every period the two statements share."""
    head = _first_valued(comprehensive)
    shared = {p.key for p in income.periods}
    keys = [p.key for p in comprehensive.periods if p.key in shared]
    keys = [k for k in keys if head is not None and head.value_for(k) is not None]
    if head is None or not keys:
        return []

    def equal(item: LineItem) -> bool:
        return all(
            (v := item.value_for(k)) is not None and abs(v - head.value_for(k)) <= _HALF  # type: ignore[operator]
            for k in keys
        )

    match = next((i for i in income.line_items if i.cells and equal(i)), None)
    results = []
    for key in keys:
        actual = head.value_for(key)
        expected = match.value_for(key) if match is not None else None
        results.append(
            CheckResult(
                id=f"{comprehensive.id}:net_profit_tie:{head.id}:{key}",
                statement_id=comprehensive.id,
                kind="net_profit_tie",
                period_key=key,
                status="pass" if match is not None else "fail",
                expected=expected,
                actual=actual,
                difference=actual - expected
                if actual is not None and expected is not None
                else None,
                tolerance=_HALF if match is not None else None,
                line_item_ids=[match.id, head.id] if match is not None else [head.id],
                detail="" if match is not None else "no_income_row_equal",
            )
        )
    return results
