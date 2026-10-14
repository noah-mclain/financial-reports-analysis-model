"""Conservative assignment of two comma-grouped amounts merged by table OCR."""

from __future__ import annotations

import math
import re
from bisect import bisect_left
from statistics import median

from fra_core.numbers import parse_number
from fra_core.schemas import BBox
from fra_ingest.header import HeaderLayout
from fra_ingest.table_grid import Grid, GridCell

_TOKEN = re.compile(r"[0-9]{1,3}(?:,[0-9]{3})+")
SPLIT_FLAG = "numeric_columns_split"


def _box(cell: GridCell) -> BBox | None:
    box = cell.bbox
    if box is None or not all(math.isfinite(v) for v in (box.left, box.right, box.top, box.bottom)):
        return None
    return box if box.left < box.right and box.top < box.bottom else None


class _Index:
    """One pass over the grid: row sections, cells over queried columns, misaligned rows."""

    def __init__(self, grid: Grid, cols: tuple[int, ...]) -> None:
        wanted = set(cols)
        self._sections = sorted(c.row for c in grid.cells if c.is_row_section)
        self._covering: dict[tuple[int, int], list[GridCell]] = {}
        self._unresolved: set[int] = set()
        for c in grid.cells:
            rows = range(max(c.row, 0), min(c.row + c.row_span, grid.num_rows))
            if "row_misaligned" in c.flags:
                self._unresolved.update(rows)
            for col in wanted.intersection(range(c.col, c.col + c.col_span)):
                for row in rows:
                    self._covering.setdefault((row, col), []).append(c)

    def covers(self, row: int, col: int) -> list[GridCell]:
        return self._covering.get((row, col), [])

    def unresolved(self, row: int) -> bool:
        return row in self._unresolved

    def section(self, row: int) -> int | None:
        before = bisect_left(self._sections, row)
        return self._sections[before - 1] if before else None


def _unit(cell: GridCell, row: int, col: int, page: int) -> bool:
    return (
        (cell.row, cell.col, cell.row_span, cell.col_span, cell.page_no) == (row, col, 1, 1, page)
        and not cell.is_column_header
        and not cell.is_row_section
    )


def _columns(
    grid: Grid,
    rows: list[int],
    index: _Index,
    label: int,
    left: int,
    right: int,
    target_section: int | None,
) -> tuple[float, float] | None:
    pairs: list[tuple[BBox, BBox]] = []
    for row in rows:
        if index.section(row) != target_section:
            continue
        if index.unresolved(row):
            continue
        a, b = index.covers(row, left), index.covers(row, right)
        if len(a) != 1 or len(b) != 1:
            continue
        first, second = a[0], b[0]
        if not (_unit(first, row, left, grid.page_no) and _unit(second, row, right, grid.page_no)):
            continue
        # Derived assignments are never independent witnesses for a subsequent repair.
        if SPLIT_FLAG in first.flags or SPLIT_FLAG in second.flags:
            continue
        if parse_number(first.text).value is None or parse_number(second.text).value is None:
            continue
        first_box, second_box = _box(first), _box(second)
        if first_box is None or second_box is None or first_box.right >= second_box.left:
            return None
        # A witness row counts only when its label lies physically left of its amounts.
        labels = index.covers(row, label)
        if len(labels) != 1 or not _unit(labels[0], row, label, grid.page_no):
            continue
        label_box = _box(labels[0])
        if label_box is None or label_box.right > first_box.left:
            continue
        pairs.append((first_box, second_box))
    # Two logical rows over one physical row (a duplicated OCR row) are a single observation.
    distinct: list[tuple[BBox, BBox]] = []
    bands: list[tuple[float, float]] = []
    for pair in sorted(pairs, key=lambda p: p[0].top):
        band = (min(pair[0].top, pair[1].top), max(pair[0].bottom, pair[1].bottom))
        if any(band[0] <= end and start <= band[1] for start, end in bands):
            continue
        distinct.append(pair)
        bands.append(band)
    pairs = distinct
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
    The label must lie left of the amounts both by logical column and by box position.
    Witness rows may be realigned, but each needs a boxed label left of its amounts; each page
    and table section must provide two independent witness rows.
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
    rows = layout.data_rows(grid)
    index = _Index(grid, (layout.label_col, left, right))
    target_rows = [
        row
        for row in rows
        if len(index.covers(row, layout.label_col)) == 1
        and index.covers(row, layout.label_col)[0].text.strip()
        and len(index.covers(row, left)) == 1
        and len(index.covers(row, left)[0].text.split()) == 2
        and parse_number(index.covers(row, left)[0].text).value is None
    ]
    if not target_rows:
        return grid
    target_sections = {index.section(row) for row in target_rows}
    centres_by_section = {
        section: _columns(grid, rows, index, layout.label_col, left, right, section)
        for section in target_sections
    }
    replacements: dict[tuple[int, int], GridCell] = {}
    additions: list[GridCell] = []
    for row in rows:
        centres = centres_by_section.get(index.section(row))
        if centres is None:
            continue
        if index.unresolved(row):
            continue
        labels = index.covers(row, layout.label_col)
        sources = index.covers(row, left)
        destinations = index.covers(row, right)
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
        if box is None or label_box is None or label_box.right > box.left:
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
                    "flags": (*source.flags, SPLIT_FLAG),
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
