"""Which rows each total covers, found by the sums themselves (spec 12, Data flow step 10).

Rows are read top to bottom into a list of open rows. A row with values closes the shortest
suffix of that list that sums to it, and takes its place. A heading marks where a run starts
and removes nothing, so a total over section totals needs no special case. A total that no
suffix explains is judged against the run above it, as Part 3a did.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from fra_core.schemas import LineItem, Statement

HALF = Decimal("0.5")
_Fit = Literal["fit", "blank", "clash", "open"]


class SumOutcome(BaseModel):
    """How one total came out in one period."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["pass", "fail", "skipped"]
    addend_ids: tuple[str, ...] = ()
    expected: Decimal | None = None
    actual: Decimal | None = None
    blank_ids: tuple[str, ...] = ()
    detail: str = ""


class SumGroup(BaseModel):
    """A total and the rows it covers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_id: str
    addend_ids: tuple[str, ...]
    basis: Literal["sums", "run"]
    implicit: bool
    outcomes: dict[str, SumOutcome]

    @property
    def confirmed(self) -> bool:
        return any(o.status == "pass" for o in self.outcomes.values())


def _is_blank(item: LineItem, key: str) -> bool:
    return any(
        c.period_key == key and c.reported is None and "numbers_missing" in c.flags
        for c in item.cells
    )


def _fit(item: LineItem, addends: Sequence[LineItem], key: str) -> tuple[_Fit, SumOutcome]:
    actual = item.value_for(key)
    values = [a.value_for(key) for a in addends]
    blanks = tuple(
        a.id for a, v in zip(addends, values, strict=True) if v is None and _is_blank(a, key)
    )
    lost = sum(1 for v in values if v is None) - len(blanks)
    missing = SumOutcome(status="skipped", detail="missing_values")
    if actual is None or lost:
        return "open", missing
    present = [v for v in values if v is not None]
    expected = sum(present, Decimal(0))
    within = abs(actual - expected) <= HALF * len(present)
    if blanks and not within:
        return "open", missing
    outcome = SumOutcome(
        status="pass" if within else "fail",
        addend_ids=tuple(a.id for a in addends),
        expected=expected,
        actual=actual,
        blank_ids=blanks,
    )
    return ("blank" if blanks else "fit") if within else "clash", outcome


def _non_zero(addends: Sequence[LineItem], keys: Sequence[str]) -> int:
    return sum(1 for a in addends if any(a.value_for(k) for k in keys))


def _closing_suffix(
    item: LineItem, open_rows: Sequence[LineItem], keys: Sequence[str]
) -> tuple[int, dict[str, SumOutcome]] | None:
    """The length of the suffix of ``open_rows`` that ``item`` closes, with its outcomes.

    Shortest first. Every candidate fits in at least one period with every value present and
    not all of them zero. A
    row with a total cue takes a suffix that clashes in no period, else the one that fits the
    most periods. A row without one must fit in every period."""
    best: tuple[int, int, dict[str, SumOutcome]] | None = None
    for length in range(2, len(open_rows) + 1):
        addends = open_rows[-length:]
        if _non_zero(addends, keys) < 2:
            continue
        fits = {key: _fit(item, addends, key) for key in keys}
        kinds = [kind for kind, _ in fits.values()]
        # A period where every figure is zero fits any rows at all, so it confirms none.
        complete = sum(
            1
            for kind, outcome in fits.values()
            if kind == "fit" and (outcome.actual or outcome.expected)
        )
        if not complete:
            continue
        outcomes = {key: outcome for key, (_, outcome) in fits.items()}
        if "clash" not in kinds and (item.is_subtotal or "open" not in kinds):
            return length, outcomes
        if item.is_subtotal and "clash" in kinds and (best is None or complete > best[0]):
            best = (complete, length, outcomes)
    return (best[1], best[2]) if best is not None else None


def _run_outcomes(
    item: LineItem,
    run: Sequence[LineItem],
    previous: LineItem | None,
    after_heading: bool,
    keys: Sequence[str],
) -> dict[str, SumOutcome]:
    """Part 3a's reading: the run since the last heading or total, alone or with the total
    before it."""
    outcomes: dict[str, SumOutcome] = {}
    for key in keys:
        if len(run) == 1:
            # One row above the total. The only checks there are: that the two are equal, or
            # that the total is that row plus the total before it. One addend rounds nothing,
            # so the equality is exact.
            actual, only = item.value_for(key), run[0].value_for(key)
            prior = previous.value_for(key) if previous is not None else None
            if actual is not None and only is not None and actual == only:
                outcomes[key] = SumOutcome(
                    status="pass",
                    addend_ids=(run[0].id,),
                    expected=only,
                    actual=actual,
                    detail="single_addend",
                )
                continue
            if (
                actual is not None
                and only is not None
                and previous is not None
                and prior is not None
                and abs(actual - prior - only) <= HALF * 2
            ):
                outcomes[key] = SumOutcome(
                    status="pass",
                    addend_ids=(previous.id, run[0].id),
                    expected=prior + only,
                    actual=actual,
                )
                continue
        if len(run) < 2:
            outcomes[key] = SumOutcome(status="skipped", detail="subtotal_scope_unknown")
            continue
        actual = item.value_for(key)
        values = [r.value_for(key) for r in run]
        if actual is None or any(v is None for v in values):
            outcomes[key] = SumOutcome(status="skipped", detail="missing_values")
            continue
        plain = sum((v for v in values if v is not None), Decimal(0))
        ids = tuple(r.id for r in run)
        candidates = [(plain, ids)]
        prior = previous.value_for(key) if previous is not None else None
        if previous is not None and prior is not None:
            candidates.append((plain + prior, (previous.id, *ids)))
        expected, addend_ids = min(candidates, key=lambda c: abs(actual - c[0]))
        within = abs(actual - expected) <= HALF * len(addend_ids)
        status: Literal["pass", "fail", "skipped"] = "pass" if within else "fail"
        detail = ""
        if not within and after_heading:
            # The heading may have cut rows the total covers.
            status, detail = "skipped", "subtotal_scope_uncertain"
        outcomes[key] = SumOutcome(
            status=status, addend_ids=addend_ids, expected=expected, actual=actual, detail=detail
        )
    return outcomes


def infer_sums(statement: Statement) -> list[SumGroup]:
    keys = [p.key for p in statement.periods]
    groups: list[SumGroup] = []
    open_rows: list[LineItem] = []
    run_start = 0
    previous: LineItem | None = None
    after_heading = False
    for item in statement.line_items:
        if item.cells and all("per_share" in c.flags for c in item.cells):
            continue
        if not item.cells:
            after_heading = after_heading or len(open_rows) > run_start
            run_start = len(open_rows)
            continue
        run = open_rows[run_start:]
        closing = _closing_suffix(item, open_rows, keys)
        if closing is None and not item.is_subtotal:
            open_rows.append(item)
            continue
        if closing is not None:
            length, outcomes = closing
            addends = open_rows[-length:]
            ids = tuple(a.id for a in addends)
            run_ids = tuple(r.id for r in run)
            with_previous = (previous.id, *run_ids) if previous is not None else None
            basis: Literal["sums", "run"] = "run" if ids in (run_ids, with_previous) else "sums"
            if not item.is_subtotal and length <= len(run):
                basis = "run"
            groups.append(
                SumGroup(
                    total_id=item.id,
                    addend_ids=ids,
                    basis=basis,
                    implicit=not item.is_subtotal,
                    outcomes=outcomes,
                )
            )
            del open_rows[-length:]
        else:
            groups.append(
                SumGroup(
                    total_id=item.id,
                    addend_ids=tuple(r.id for r in run),
                    basis="run",
                    implicit=False,
                    outcomes=_run_outcomes(item, run, previous, after_heading, keys),
                )
            )
            del open_rows[run_start:]
        open_rows.append(item)
        if item.is_subtotal:
            run_start, previous, after_heading = len(open_rows), item, False
        else:
            # An implicit total stands in the run for the rows it sums.
            run_start = min(run_start, len(open_rows) - 1)
    return groups
