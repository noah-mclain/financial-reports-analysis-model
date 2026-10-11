"""Conservative assignment of two comma-grouped amounts merged by table OCR."""

from __future__ import annotations

import math
import re
from statistics import median

from fra_core.numbers import parse_number
from fra_core.schemas import BBox
from fra_ingest.header import HeaderLayout
from fra_ingest.table_grid import Grid, GridCell

_TOKEN = re.compile(r"[0-9]{1,3}(?:,[0-9]{3})+")
_FLAG = "numeric_columns_split"


def _box(cell: GridCell) -> BBox | None:
    box = cell.bbox
    if box is None or not all(math.isfinite(v) for v in (box.left, box.right, box.top, box.bottom)):
        return None
    return box if box.left < box.right and box.top < box.bottom else None


def _covers(grid: Grid, row: int, col: int) -> list[GridCell]:
    return [
        c
        for c in grid.cells
        if c.row <= row < c.row + c.row_span and c.col <= col < c.col + c.col_span
    ]


def _unit(cell: GridCell, row: int, col: int, page: int) -> bool:
    return (
        (cell.row, cell.col, cell.row_span, cell.col_span, cell.page_no) == (row, col, 1, 1, page)
        and not cell.is_column_header
        and not cell.is_row_section
    )


def _unresolved(grid: Grid, row: int) -> bool:
    return any("row_misaligned" in c.flags for c in grid.cells if c.row <= row < c.row + c.row_span)


def _section(grid: Grid, row: int) -> int | None:
    sections = [cell.row for cell in grid.cells if cell.is_row_section and cell.row < row]
    return max(sections, default=None)


def _columns(
    grid: Grid, layout: HeaderLayout, left: int, right: int, target_section: int | None
) -> tuple[float, float] | None:
    pairs: list[tuple[BBox, BBox]] = []
    for row in layout.data_rows(grid):
        if _section(grid, row) != target_section:
            continue
        if _unresolved(grid, row):
            continue
        a, b = _covers(grid, row, left), _covers(grid, row, right)
        if len(a) != 1 or len(b) != 1:
            continue
        first, second = a[0], b[0]
        if not (_unit(first, row, left, grid.page_no) and _unit(second, row, right, grid.page_no)):
            continue
        # Derived assignments are never independent witnesses for a subsequent repair.
        if _FLAG in first.flags or _FLAG in second.flags:
            continue
        if parse_number(first.text).value is None or parse_number(second.text).value is None:
            continue
        first_box, second_box = _box(first), _box(second)
        if first_box is None or second_box is None or first_box.right >= second_box.left:
            return None
        pairs.append((first_box, second_box))
    if len(pairs) < 2:
        return None
    # Separate rectangles in one row must also establish separate physical column bands.
    if max(a.right for a, _ in pairs) >= min(b.left for _, b in pairs):
        return None
    return median((a.left + a.right) / 2 for a, _ in pairs), median(
        (b.left + b.right) / 2 for _, b in pairs
    )


def split_numeric_columns(grid: Grid, layout: HeaderLayout) -> Grid:
    """Split only two strictly grouped tokens with independent physical column evidence.

    The caller admits logical OCR grids only. Periods are already bound by the header;
    geometry assigns tokens left to right without interpreting dates or comparing values.
    Witness rows may be realigned or label-less because only their boxes establish geometry;
    each page and table section must provide two independent witness rows.
    """
    cols = sorted(layout.value_cols)
    if (
        len(cols) != 2
        or cols[1] != cols[0] + 1
        or layout.label_col is None
        or layout.label_col in cols
        or layout.label_col >= cols[0]
        or layout.unbound_cols
        or layout.period_conflicts
        or layout.value_cols[cols[0]].key == layout.value_cols[cols[1]].key
        or any(f.split(":")[0] in {"period_conflict", "period_unbound"} for f in layout.evidence)
    ):
        return grid
    left, right = cols
    target_rows = [
        row
        for row in layout.data_rows(grid)
        if layout.label_col is not None
        and len(_covers(grid, row, layout.label_col)) == 1
        and _covers(grid, row, layout.label_col)[0].text.strip()
        and len(_covers(grid, row, left)) == 1
        and len(_covers(grid, row, left)[0].text.split()) == 2
        and parse_number(_covers(grid, row, left)[0].text).value is None
    ]
    if not target_rows:
        return grid
    target_sections = {_section(grid, row) for row in target_rows}
    centres_by_section = {
        section: _columns(grid, layout, left, right, section) for section in target_sections
    }
    replacements: dict[tuple[int, int], GridCell] = {}
    additions: list[GridCell] = []
    for row in layout.data_rows(grid):
        centres = centres_by_section.get(_section(grid, row))
        if centres is None:
            continue
        if _unresolved(grid, row):
            continue
        labels = _covers(grid, row, layout.label_col)
        sources = _covers(grid, row, left)
        destinations = _covers(grid, row, right)
        if len(labels) != 1 or len(sources) != 1 or len(destinations) > 1:
            continue
        label, source = labels[0], sources[0]
        if (
            not label.text.strip()
            or not _unit(label, row, layout.label_col, grid.page_no)
            or not _unit(source, row, left, grid.page_no)
        ):
            continue
        if destinations and (
            destinations[0].text.strip() or not _unit(destinations[0], row, right, grid.page_no)
        ):
            continue
        if parse_number(source.text).value is not None:
            continue
        tokens = source.text.split()
        if len(tokens) != 2 or not all(
            _TOKEN.fullmatch(t) and parse_number(t).value is not None for t in tokens
        ):
            continue
        box, label_box = _box(source), _box(label)
        if box is None or label_box is None:
            continue
        if not (
            box.left <= centres[0] < centres[1] <= box.right
            and box.top <= (label_box.top + label_box.bottom) / 2 <= box.bottom
        ):
            continue
        for col, token in zip(cols, tokens, strict=True):
            cell = source.model_copy(
                update={
                    "col": col,
                    "text": token,
                    "source_row": source.source_row
                    if source.source_row is not None
                    else source.row,
                    "source_col": source.source_col
                    if source.source_col is not None
                    else source.col,
                    "flags": (*source.flags, _FLAG),
                }
            )
            if col == left or destinations:
                replacements[(row, col)] = cell
            else:
                additions.append(cell)
    if not replacements:
        return grid
    return grid.model_copy(
        update={
            "cells": tuple(replacements.get((c.row, c.col), c) for c in grid.cells)
            + tuple(additions)
        }
    )
