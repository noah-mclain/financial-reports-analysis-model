"""Statements continued across pages (spec 11, Data flow step 8).

Parts of one type on the same or consecutive pages merge when their periods match and their value
columns line up, unless the earlier part is a closed balance sheet. A continuation page whose
header leaves a value column without a date (Almarai AR, p160) takes that column's period from
the previous page, by position.
"""

from __future__ import annotations

from collections.abc import Sequence

from fra_core.schemas import StatementType
from fra_ingest.header import HeaderLayout
from fra_ingest.label_match import CLOSING_TOTAL_ID, LabelIndex
from fra_ingest.parts import PartialStatement, column_centre
from fra_ingest.table_grid import Grid

COLUMN_TOLERANCE = 0.05


def inherit_periods(layout: HeaderLayout, grid: Grid, previous: PartialStatement) -> HeaderLayout:
    if not layout.unbound_cols:
        return layout
    tolerance = COLUMN_TOLERANCE * grid.page_width
    bound_keys = {p.key for p in layout.value_cols.values()}
    value_cols = dict(layout.value_cols)
    unbound: list[int] = []
    evidence = list(layout.evidence)
    rows = layout.data_rows(grid)
    for col in layout.unbound_cols:
        centre = column_centre(grid, col, rows)
        match = None
        if centre is not None:
            candidates = [
                p
                for p in previous.periods
                if p.key not in bound_keys
                and p.key in previous.column_centres
                and abs(previous.column_centres[p.key] - centre) <= tolerance
            ]
            match = min(
                candidates,
                key=lambda p: abs(previous.column_centres[p.key] - centre),
                default=None,
            )
        if match is None:
            unbound.append(col)
            continue
        value_cols[col] = match
        bound_keys.add(match.key)
        evidence = [e for e in evidence if e != f"period_unbound:{col}"]
        evidence.append(f"inherited:{col}:{match.key}")
    return layout.model_copy(
        update={
            "value_cols": dict(sorted(value_cols.items())),
            "unbound_cols": unbound,
            "evidence": evidence,
        }
    )


def is_closed(part: PartialStatement, index: LabelIndex) -> bool:
    """Whether ``part`` is a balance sheet whose last row carrying values is its closing total
    (total equity and liabilities). Rows without values after it, such as a footer, do not
    reopen it. Nothing continues a closed statement; other statement types are never closed."""
    if part.type is not StatementType.BALANCE:
        return False
    valued = [i for i in part.line_items if i.cells]
    if not valued:
        return False
    match = index.match(valued[-1].raw_label, StatementType.BALANCE)
    return match is not None and match.id == CLOSING_TOTAL_ID


def continues_part(layout: HeaderLayout, grid: Grid, previous: PartialStatement) -> bool:
    """Whether a grid that classification found below confidence continues ``previous``: the
    part ends on the grid's page or the page before, the grid binds exactly the part's period
    keys, and each bound column lines up with the part's column for that period within
    COLUMN_TOLERANCE of the page width. Whether ``previous`` is closed is for the caller."""
    if previous.last_page not in (grid.page_no, grid.page_no - 1):
        return False
    keys = {p.key for p in layout.value_cols.values()}
    if not keys or keys != {p.key for p in previous.periods}:
        return False
    tolerance = COLUMN_TOLERANCE * grid.page_width
    rows = layout.data_rows(grid)
    for col, period in layout.value_cols.items():
        centre = column_centre(grid, col, rows)
        expected = previous.column_centres.get(period.key)
        if centre is None or expected is None or abs(expected - centre) > tolerance:
            return False
    return True


def _continues(first: PartialStatement, second: PartialStatement, index: LabelIndex) -> bool:
    if (
        is_closed(first, index)
        or second.type is not first.type
        or second.first_page not in (first.last_page, first.last_page + 1)
    ):
        return False
    keys = {p.key for p in second.periods}
    if keys != {p.key for p in first.periods}:
        return False
    tolerance = COLUMN_TOLERANCE * first.page_width
    for key in keys:
        if key not in first.column_centres or key not in second.column_centres:
            return False
        if abs(first.column_centres[key] - second.column_centres[key]) > tolerance:
            return False
    return True


def _table_index(part: PartialStatement) -> int:
    """N of the first table's "...#/tables/N"; -1 when it has none."""
    ref = part.table_refs[0] if part.table_refs else ""
    tail = ref.rpartition("/tables/")[2]
    return int(tail) if tail.isdigit() else -1


def merge_continuations(
    parts: Sequence[PartialStatement], index: LabelIndex
) -> list[PartialStatement]:
    """Parts of one type in page order, then table order on a page, each merged into the one
    before it when it starts on that part's last page or the next, its columns line up and
    that part is not closed."""
    ordered = sorted(parts, key=lambda p: (p.type.value, p.first_page, _table_index(p)))
    merged: list[PartialStatement] = []
    for part in ordered:
        if merged and _continues(merged[-1], part, index):
            head = merged[-1]
            merged[-1] = head.model_copy(
                update={
                    "last_page": part.last_page,
                    "line_items": [*head.line_items, *part.line_items],
                    "indents": {**head.indents, **part.indents},
                    "table_refs": [*head.table_refs, *part.table_refs],
                    "flags": [*head.flags, *part.flags],
                }
            )
        else:
            merged.append(part)
    return merged
