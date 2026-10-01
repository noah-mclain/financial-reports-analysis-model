"""Values put back on their label's line by geometry (spec 12, Data flow step 6a).

OCR tables can come out with a row's values one row away from its label. The grid says which
row a cell is in; the cell's box says where it sits on the page. Where the two disagree, the
box is right: a group of values whose vertical centre lies outside its own label's top and
bottom is moved to the row whose label line contains it. A repair moves cells and never edits
them, and anything that cannot be placed is left where it was and flagged.
"""

from __future__ import annotations

from itertools import pairwise
from statistics import median

from pydantic import BaseModel, ConfigDict, Field

from fra_ingest.header import HeaderLayout
from fra_ingest.table_grid import Grid, GridCell

_SLACK = 0.25  # of the median value-cell height
_TALL = 1.7  # a label cell this many value heights tall holds more than one printed line


class Realignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grid: Grid
    moved: list[tuple[int, int, int]] = Field(default_factory=list)  # (col, from_row, to_row)
    unresolved: list[int] = Field(default_factory=list)
    merged_labels: list[int] = Field(default_factory=list)


def _centre(cell: GridCell) -> float:
    assert cell.bbox is not None
    return (cell.bbox.top + cell.bbox.bottom) / 2


def realign_rows(grid: Grid, layout: HeaderLayout) -> Realignment:
    rows = layout.data_rows(grid)
    groups: dict[int, list[GridCell]] = {}
    for cell in grid.cells:
        if cell.row in rows and cell.col in layout.value_cols and cell.text and cell.bbox:
            groups.setdefault(cell.row, []).append(cell)
    if not groups or layout.label_col is None:
        return Realignment(grid=grid)
    height = median(c.bbox.height for cells in groups.values() for c in cells if c.bbox)
    slack = _SLACK * height
    bands: dict[int, tuple[float, float]] = {}
    for cell in grid.cells:
        if cell.row in rows and cell.col == layout.label_col and cell.text and cell.bbox:
            bands[cell.row] = (cell.bbox.top, cell.bbox.bottom)
    merged = sorted(r for r, (top, bottom) in bands.items() if bottom - top > _TALL * height)
    at = {row: median(_centre(c) for c in cells) for row, cells in groups.items()}

    def inside(y: float, row: int) -> bool:
        top, bottom = bands[row]
        return top - slack <= y <= bottom + slack

    off = sorted(r for r in groups if r in bands and not inside(at[r], r))
    if not off:
        return Realignment(grid=grid, merged_labels=merged)

    # Where each row's group ends up: start from the grid, then vacate the rows that are off.
    holds: dict[int, int | None] = {r: (r if r in groups else None) for r in rows}
    for row in off:
        holds[row] = None
    # An unlabelled group that sits on the label line of a vacated row belongs to that row.
    vacated_unlabelled: list[int] = []
    for row in sorted(r for r in groups if r not in bands):
        target = next((t for t in off if holds[t] is None and inside(at[row], t)), None)
        if target is not None:
            holds[target], holds[row] = row, None
            vacated_unlabelled.append(row)
    unresolved: list[int] = []
    for row in sorted(off, key=lambda r: at[r]):
        targets = sorted(
            (t for t in bands if t != row and inside(at[row], t)),
            key=lambda t: abs(at[row] - sum(bands[t]) / 2),
        )
        free = next((t for t in targets if holds[t] is None), None)
        if free is None and targets:
            # The label line already keeps a group: a cell holding two printed lines. The
            # lower group takes the unlabelled row that was vacated below it.
            free = next(
                (u for u in vacated_unlabelled if u > targets[0] and holds[u] is None), None
            )
        if free is None:
            unresolved.append(row)
            continue
        holds[free] = row
    for row in unresolved:
        if holds[row] is None:
            holds[row] = row
    placed = [(row, at[source]) for row, source in sorted(holds.items()) if source is not None]
    in_order = all(a[1] <= b[1] + slack for a, b in pairwise(placed))
    lost = set(groups) - {s for s in holds.values() if s is not None}
    if not in_order or lost:
        # A repair that would reorder or drop figures is no repair: leave the grid, flag it.
        holds = {r: (r if r in groups else None) for r in rows}
        unresolved = off

    target_of = {source: row for row, source in holds.items() if source is not None}
    moved: list[tuple[int, int, int]] = []
    cells: list[GridCell] = []
    for cell in grid.cells:
        if cell.row in groups and cell in groups[cell.row]:
            target = target_of[cell.row]
            if target != cell.row:
                moved.append((cell.col, cell.row, target))
                cell = cell.model_copy(
                    update={
                        "row": target,
                        "source_row": cell.row,
                        "flags": (*cell.flags, "row_realigned"),
                    }
                )
            elif cell.row in unresolved:
                cell = cell.model_copy(update={"flags": (*cell.flags, "row_misaligned")})
        cells.append(cell)
    return Realignment(
        grid=grid.model_copy(update={"cells": tuple(cells)}),
        moved=sorted(moved),
        unresolved=sorted(unresolved),
        merged_labels=merged,
    )
