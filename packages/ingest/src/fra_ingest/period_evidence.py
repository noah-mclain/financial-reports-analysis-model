"""Validate caller-collected header observations; hold unresolved interpretations."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date

from fra_core.schemas import BBox, Period, PeriodKind
from fra_core.schemas.statement import (
    PeriodCandidate,
    PeriodConflict,
    PeriodEvidence,
    PeriodObservation,
)
from fra_ingest.docling_json import DlCell, DlTable
from fra_ingest.table_grid import Grid


def merge_evidence(*collections: Sequence[PeriodEvidence]) -> tuple[PeriodEvidence, ...]:
    """Keep every distinct observation; a reused identity must name identical facts."""
    columns: dict[int, dict[str, PeriodObservation]] = {}
    for collection in collections:
        for group in collection:
            column = columns.setdefault(group.col, {})
            for observation in group.observations:
                previous = column.get(observation.source_id)
                if previous is not None and previous != observation:
                    raise ValueError(
                        f"period evidence source ID {observation.source_id!r} has different facts"
                    )
                column[observation.source_id] = observation
    return tuple(PeriodEvidence(observations=tuple(c.values())) for c in columns.values())


def source_cell(table: DlTable, row: int, col: int, page_no: int) -> tuple[int, DlCell] | None:
    """The one unit-span original cell at (row, col) of a single-page table, with its index."""
    sources = [
        (i, c)
        for i, c in enumerate(table.data.table_cells)
        if c.start_row_offset_idx <= row < c.start_row_offset_idx + c.row_span
        and c.start_col_offset_idx <= col < c.start_col_offset_idx + c.col_span
    ]
    if len(sources) != 1 or len(table.prov) != 1 or table.prov[0].page_no != page_no:
        return None
    index, cell = sources[0]
    if cell.row_span != 1 or cell.col_span != 1 or cell.bbox is None:
        return None
    return index, cell


def _observation_error(
    grid: Grid,
    observation: PeriodObservation,
    header_rows: Sequence[int],
    source_table: DlTable | None,
    page_height: float | None,
) -> str | None:
    prov = observation.provenance
    if (observation.docling_path, prov.page_no, prov.table_ref) != (
        grid.docling_path,
        grid.page_no,
        grid.table_ref,
    ):
        return "path/page/table mismatch"
    anchor = grid.cell(prov.row, prov.col)
    if (
        anchor is None
        or anchor.row != prov.row
        or anchor.col != prov.col
        or anchor.page_no != grid.page_no
        or prov.row not in header_rows
        or anchor.col_span != 1
        or anchor.row_span != 1
        or anchor.row < 0
        or anchor.col < 0
        or anchor.bbox is None
    ):
        return "invalid header column/row"
    box = prov.bbox
    if source_table is not None:
        source = source_cell(source_table, prov.row, prov.col, grid.page_no)
        if (
            page_height is None
            or source_table.self_ref != grid.table_ref
            or source is None
            or source[1].bbox is None
            or source[1].bbox.to_bbox(page_height) != anchor.bbox
        ):
            return "invalid original source cell"
    if not (
        all(math.isfinite(v) for v in (box.left, box.top, box.right, box.bottom))
        and all(
            math.isfinite(v)
            for v in (
                anchor.bbox.left,
                anchor.bbox.top,
                anchor.bbox.right,
                anchor.bbox.bottom,
            )
        )
        and 0 <= box.left < box.right <= grid.page_width
        and 0 <= box.top < box.bottom
        and 0 <= anchor.bbox.left < anchor.bbox.right <= grid.page_width
        and 0 <= anchor.bbox.top < anchor.bbox.bottom
        and (page_height is None or max(box.bottom, anchor.bbox.bottom) <= page_height)
        and _overlap(box, anchor.bbox)
        and anchor.bbox.left <= (box.left + box.right) / 2 <= anchor.bbox.right
        and anchor.bbox.top <= (box.top + box.bottom) / 2 <= anchor.bbox.bottom
    ):
        return "invalid header geometry"
    if any(
        c != anchor and c.row in header_rows and c.bbox is not None and _overlap(box, c.bbox)
        for c in grid.cells
    ):
        return "ambiguous column geometry"
    return None


def validate_evidence(
    grid: Grid,
    evidence: Sequence[PeriodEvidence],
    header_rows: Sequence[int],
    *,
    source_table: DlTable | None = None,
    page_height: float | None = None,
) -> None:
    seen: set[int] = set()
    for group in evidence:
        if group.col in seen:
            raise ValueError(f"period evidence repeats column {group.col}")
        seen.add(group.col)
        for observation in group.observations:
            error = _observation_error(grid, observation, header_rows, source_table, page_height)
            if error is not None:
                raise ValueError(f"period evidence {observation.source_id!r}: {error}")


def supported_evidence(
    grid: Grid,
    collected: Sequence[PeriodEvidence],
    header_rows: Sequence[int],
    *,
    source_table: DlTable,
    page_height: float,
) -> tuple[tuple[PeriodEvidence, ...], tuple[str, ...]]:
    """Collected groups the original grid supports, and a visible flag for each one it does not.

    The collector reads the repaired grid; the source contract is the original one. A caller's
    invalid group is an input error and stays loud (`validate_evidence`); a collected group the
    original grid cannot carry is only an unavailable comparison."""
    kept: list[PeriodEvidence] = []
    flags: list[str] = []
    for group in collected:
        if any(
            _observation_error(grid, o, header_rows, source_table, page_height) is not None
            for o in group.observations
        ):
            flags.append(f"period_evidence_unavailable:{group.col}:raw_source_mismatch")
        else:
            kept.append(group)
    return tuple(kept), tuple(flags)


def _overlap(a: BBox, b: BBox) -> bool:
    return a.left < b.right and b.left < a.right and a.top < b.bottom and b.top < a.bottom


def period_identity(period: Period) -> tuple[str, date, PeriodKind, int | None, bool, bool | None]:
    # Unknown audit status is a separate fact; no reading supplies another's metadata.
    return period.key, period.end_date, period.kind, period.months, period.restated, period.audited


def unresolved(candidates: tuple[PeriodCandidate, ...]) -> PeriodConflict | None:
    if any(c.period is None for c in candidates):
        return PeriodConflict(reason="unparseable_observation", candidates=candidates)
    if len({period_identity(c.period) for c in candidates if c.period is not None}) > 1:
        return PeriodConflict(reason="differing_periods", candidates=candidates)
    return None
