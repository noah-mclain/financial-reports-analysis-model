"""Validate caller-collected header observations; hold unresolved interpretations."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date

from fra_core.schemas import BBox, Period, PeriodKind
from fra_core.schemas.statement import PeriodCandidate, PeriodConflict, PeriodEvidence
from fra_ingest.table_grid import Grid


def validate_evidence(
    grid: Grid, evidence: Sequence[PeriodEvidence], header_rows: Sequence[int]
) -> None:
    seen: set[int] = set()
    for group in evidence:
        if group.col in seen:
            raise ValueError(f"period evidence repeats column {group.col}")
        seen.add(group.col)
        for observation in group.observations:
            prov = observation.provenance
            if (observation.docling_path, prov.page_no, prov.table_ref) != (
                grid.docling_path,
                grid.page_no,
                grid.table_ref,
            ):
                raise ValueError(
                    f"period evidence {observation.source_id!r}: path/page/table mismatch"
                )
            anchor = grid.cell(prov.row, prov.col)
            if (
                anchor is None
                or anchor.row != prov.row
                or anchor.col != prov.col
                or anchor.page_no != grid.page_no
                or prov.row not in header_rows
                or anchor.col_span != 1
                or anchor.bbox is None
            ):
                raise ValueError(
                    f"period evidence {observation.source_id!r}: invalid header column/row"
                )
            box = prov.bbox
            if not (
                all(math.isfinite(v) for v in (box.left, box.top, box.right, box.bottom))
                and 0 <= box.left < box.right <= grid.page_width
                and 0 <= box.top < box.bottom
                and _overlap(box, anchor.bbox)
                and anchor.bbox.left <= (box.left + box.right) / 2 <= anchor.bbox.right
                and anchor.bbox.top <= (box.top + box.bottom) / 2 <= anchor.bbox.bottom
            ):
                raise ValueError(
                    f"period evidence {observation.source_id!r}: invalid header geometry"
                )
            if any(
                c.col != prov.col
                and c.row in header_rows
                and c.bbox is not None
                and _overlap(box, c.bbox)
                for c in grid.cells
            ):
                raise ValueError(
                    f"period evidence {observation.source_id!r}: ambiguous column geometry"
                )


def _overlap(a: BBox, b: BBox) -> bool:
    return a.left < b.right and b.left < a.right and a.top < b.bottom and b.top < a.bottom


def period_identity(period: Period) -> tuple[str, date, PeriodKind, int | None]:
    return period.key, period.end_date, period.kind, period.months


def unresolved(candidates: tuple[PeriodCandidate, ...]) -> PeriodConflict | None:
    if any(c.period is None for c in candidates):
        return PeriodConflict(reason="unparseable_observation", candidates=candidates)
    if len({period_identity(c.period) for c in candidates if c.period is not None}) > 1:
        return PeriodConflict(reason="differing_periods", candidates=candidates)
    return None
