"""Subtotal checks and the balance sheet identity (spec 11, step 10; spec 12, step 10).

Tolerance is rounding-aware (D6): n x 0.5 reported units, n the number of addends. Which rows
a subtotal covers is settled by ``sum_hierarchy.infer_sums``: the shortest run of open rows
above it that sums to it, across headings, with a blank the sum confirms counted as zero. A
total no sum explains is judged against the run since the last heading or total, alone or with
the total before it, and is skipped as ``subtotal_scope_uncertain`` when a heading cut rows off
that run, or as ``subtotal_scope_unknown`` when the run is shorter than two rows. Per-share
rows are left out of every sum.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from fra_core.schemas import CheckResult, LineItem, Statement, StatementType
from fra_ingest.figure_checks import diagnose_digits, flag_fractions, flag_period_outliers
from fra_ingest.label_match import LabelIndex
from fra_ingest.sum_hierarchy import SumGroup, infer_sums

_HALF = Decimal("0.5")


def _value(item: LineItem, period_key: str) -> Decimal | None:
    return item.value_for(period_key)


def _result(
    statement: Statement, kind: str, key: str, target: str, status: str, **fields: object
) -> CheckResult:
    return CheckResult.model_validate(
        {
            "id": f"{statement.id}:{kind}:{target}:{key}",
            "statement_id": statement.id,
            "kind": kind,
            "period_key": key,
            "status": status,
            **fields,
        }
    )


def _sum_results(statement: Statement, group: SumGroup) -> list[CheckResult]:
    results = []
    for key, outcome in group.outcomes.items():
        expected, actual = outcome.expected, outcome.actual
        judged = expected is not None and actual is not None
        details = [
            outcome.detail,
            "implicit_subtotal" if group.implicit else "",
            "sum_based" if group.basis == "sums" and not group.implicit and judged else "",
            "blank_as_zero" if outcome.blank_ids else "",
        ]
        addends = len(outcome.addend_ids) - len(outcome.blank_ids)
        results.append(
            _result(
                statement,
                "subtotal",
                key,
                group.total_id,
                outcome.status,
                expected=expected,
                actual=actual,
                difference=actual - expected
                if actual is not None and expected is not None
                else None,
                tolerance=_HALF * addends if judged else None,
                line_item_ids=[*outcome.addend_ids, group.total_id],
                detail="; ".join(d for d in details if d),
            )
        )
    return results


def check_subtotals(statement: Statement) -> list[CheckResult]:
    return [r for group in infer_sums(statement) for r in _sum_results(statement, group)]


def _apply_sums(statement: Statement, groups: Sequence[SumGroup]) -> Statement:
    """Parents from the sums, implicit totals marked, blanks the sums confirmed flagged."""
    parents: dict[str, str] = {}
    implicit: set[str] = set()
    blanks: set[tuple[str, str]] = set()
    for group in groups:
        if not group.confirmed:
            continue
        if group.implicit:
            implicit.add(group.total_id)
        for key, outcome in group.outcomes.items():
            if outcome.status != "pass":
                continue
            for addend in outcome.addend_ids:
                parents.setdefault(addend, group.total_id)
            blanks.update((addend, key) for addend in outcome.blank_ids)
    items = []
    for item in statement.line_items:
        cells = [
            c.model_copy(update={"flags": [*c.flags, "blank_confirmed"]})
            if (item.id, c.period_key) in blanks
            else c
            for c in item.cells
        ]
        items.append(
            item.model_copy(
                update={
                    "cells": cells,
                    "parent_id": parents.get(item.id, item.parent_id),
                    "is_subtotal": item.is_subtotal or item.id in implicit,
                }
            )
        )
    return statement.model_copy(update={"line_items": items})


def _find(statement: Statement, index: LabelIndex, canonical_id: str) -> LineItem | None:
    matches = [
        i
        for i in statement.line_items
        if i.cells
        and (m := index.match(i.raw_label, StatementType.BALANCE)) is not None
        and m.id == canonical_id
    ]
    return matches[-1] if matches else None


def check_identity(statement: Statement, index: LabelIndex) -> list[CheckResult]:
    if statement.type is not StatementType.BALANCE:
        return []
    assets = _find(statement, index, "total_assets")
    both = _find(statement, index, "total_liabilities_and_equity")
    liabilities = _find(statement, index, "total_liabilities")
    equity = _find(statement, index, "total_equity")
    results: list[CheckResult] = []
    for period in statement.periods:
        key = period.key
        if assets is None or (both is None and (liabilities is None or equity is None)):
            results.append(
                _result(
                    statement,
                    "balance_identity",
                    key,
                    "balance",
                    "skipped",
                    detail="identity_totals_not_found",
                )
            )
            continue
        actual = _value(assets, key)
        if both is not None:
            parts, ids = [_value(both, key)], [both.id]
        else:
            assert liabilities is not None and equity is not None
            parts, ids = (
                [_value(liabilities, key), _value(equity, key)],
                [liabilities.id, equity.id],
            )
        if actual is None or any(p is None for p in parts):
            results.append(
                _result(
                    statement,
                    "balance_identity",
                    key,
                    "balance",
                    "skipped",
                    detail="missing_values",
                    line_item_ids=[assets.id, *ids],
                )
            )
            continue
        expected = sum((p for p in parts if p is not None), Decimal(0))
        tolerance = _HALF * len(parts)
        difference = actual - expected
        status = "pass" if abs(difference) <= tolerance else "fail"
        results.append(
            _result(
                statement,
                "balance_identity",
                key,
                "balance",
                status,
                expected=expected,
                actual=actual,
                difference=difference,
                tolerance=tolerance,
                line_item_ids=[assets.id, *ids],
                detail="total assets against total liabilities and equity",
            )
        )
    return results


def run_checks(statement: Statement, index: LabelIndex) -> tuple[Statement, list[CheckResult]]:
    groups = infer_sums(statement)
    results = [r for group in groups for r in _sum_results(statement, group)]
    statement = _apply_sums(statement, groups)
    results += check_identity(statement, index)
    statement, results = diagnose_digits(statement, results)
    statement = flag_period_outliers(statement)
    statement = flag_fractions(statement)
    flags = list(statement.flags)
    if any(r.kind == "subtotal" and r.status == "fail" for r in results):
        flags.append("subtotal_failed")
    if any(r.kind == "balance_identity" and r.status == "fail" for r in results):
        flags.append("identity_failed")
    if any(
        r.kind == "balance_identity" and r.detail == "identity_totals_not_found" for r in results
    ):
        flags.append("identity_totals_not_found")
    return statement.model_copy(update={"flags": flags}), results
