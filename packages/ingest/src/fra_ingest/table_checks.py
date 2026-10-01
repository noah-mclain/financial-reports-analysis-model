"""Subtotal checks and the balance sheet identity (spec 11, Data flow step 10).

Tolerance is rounding-aware (D6): n x 0.5 reported units, n the number of addends. A subtotal
is checked only when it directly closes a run of two or more plain rows; it passes against
their sum, or against their sum plus the previous subtotal (a running total such as operating
profit after gross profit). A heading row starts a new run but keeps the previous subtotal; a
subtotal whose run began at a heading that cut rows off the run before it, and that misses both
sums, is skipped as ``subtotal_scope_uncertain``, since those rows may belong to it. A plain row
equal to the sum of the last two or more rows of the run in every period is an implicit
subtotal: it passes and replaces those rows in the run as one addend. At least two of those
rows must hold a value other than zero. Per-share rows are left out of every sum. Other
subtotals are skipped for Part 3b's sum-based hierarchy.
"""

from __future__ import annotations

from decimal import Decimal

from fra_core.schemas import CheckResult, LineItem, Statement, StatementType
from fra_ingest.label_match import LabelIndex

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


def _suffix_sum(
    statement: Statement, item: LineItem, addends: list[LineItem]
) -> list[CheckResult] | None:
    """Pass results when ``item`` equals the sum of ``addends`` in every period, else None."""
    tolerance = _HALF * len(addends)
    results = []
    for period in statement.periods:
        key = period.key
        actual = _value(item, key)
        values = [_value(i, key) for i in addends]
        if actual is None or any(v is None for v in values):
            return None
        expected = sum((v for v in values if v is not None), Decimal(0))
        if abs(actual - expected) > tolerance:
            return None
        results.append(
            _result(
                statement,
                "subtotal",
                key,
                item.id,
                "pass",
                expected=expected,
                actual=actual,
                difference=actual - expected,
                tolerance=tolerance,
                line_item_ids=[*(i.id for i in addends), item.id],
                detail="implicit_subtotal",
            )
        )
    return results


def _implicit_subtotal(
    statement: Statement, item: LineItem, run: list[LineItem]
) -> tuple[int, list[CheckResult]] | None:
    """The length of the shortest suffix of ``run`` that ``item`` equals in every period, with
    its pass results, else None. The suffix needs two or more rows that are not zero
    throughout: a row equal to one row plus dashes is not a sum."""
    for length in range(2, len(run) + 1):
        addends = run[-length:]
        if sum(1 for i in addends if any(_value(i, p.key) for p in statement.periods)) < 2:
            continue
        results = _suffix_sum(statement, item, addends)
        if results is not None:
            return length, results
    return None


def check_subtotals(statement: Statement) -> list[CheckResult]:
    results: list[CheckResult] = []
    run: list[LineItem] = []
    previous: LineItem | None = None
    after_heading = False
    for item in statement.line_items:
        if item.cells and all("per_share" in c.flags for c in item.cells):
            # Earnings per share is not an amount of the statement's unit: it joins no sum.
            continue
        if not item.cells:
            # A heading starts a new run; a running total carries across it. Only a heading
            # that cut rows off the run makes the next subtotal's scope uncertain.
            after_heading = after_heading or bool(run)
            run = []
            continue
        if not item.is_subtotal:
            implicit = _implicit_subtotal(statement, item, run)
            if implicit is not None:
                # The implicit subtotal stands in for the rows it sums.
                length, found = implicit
                results.extend(found)
                del run[-length:]
            run.append(item)
            continue
        for period in statement.periods:
            key = period.key
            if len(run) < 2:
                results.append(
                    _result(
                        statement,
                        "subtotal",
                        key,
                        item.id,
                        "skipped",
                        line_item_ids=[item.id],
                        detail="subtotal_scope_unknown",
                    )
                )
                continue
            values = [_value(i, key) for i in run]
            actual = _value(item, key)
            if actual is None or any(v is None for v in values):
                results.append(
                    _result(
                        statement,
                        "subtotal",
                        key,
                        item.id,
                        "skipped",
                        line_item_ids=[item.id],
                        detail="missing_values",
                    )
                )
                continue
            plain = sum((v for v in values if v is not None), Decimal(0))
            candidates = [(plain, len(run), [i.id for i in run])]
            prior = _value(previous, key) if previous is not None else None
            if previous is not None and prior is not None:
                candidates.append(
                    (plain + prior, len(run) + 1, [previous.id, *(i.id for i in run)])
                )
            expected, addends, ids = min(candidates, key=lambda c: abs(actual - c[0]))
            tolerance = _HALF * addends
            difference = actual - expected
            status, detail = ("pass", "") if abs(difference) <= tolerance else ("fail", "")
            if status == "fail" and after_heading:
                # The heading may have cut rows the subtotal covers (items above it).
                status, detail = "skipped", "subtotal_scope_uncertain"
            results.append(
                _result(
                    statement,
                    "subtotal",
                    key,
                    item.id,
                    status,
                    expected=expected,
                    actual=actual,
                    difference=difference,
                    tolerance=tolerance,
                    line_item_ids=[*ids, item.id],
                    detail=detail,
                )
            )
        run, previous, after_heading = [], item, False
    return results


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
    results = check_subtotals(statement) + check_identity(statement, index)
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
