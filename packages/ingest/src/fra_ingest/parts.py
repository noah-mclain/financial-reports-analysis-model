"""One classified grid as part of a statement: line items with provenance (spec 11, step 7).

Values are kept as printed, sign applied, before scale. An empty value cell in a row that has
values elsewhere is kept with ``numbers_missing`` and a box synthesized from its row and
column, so every gap stays visible and traceable.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from fra_core.numbers import parse_number
from fra_core.schemas import BBox, Cell, LineItem, Period, Provenance, StatementType, TextSource
from fra_ingest.classify import Classification
from fra_ingest.header import HeaderLayout, split_note
from fra_ingest.label_match import squash
from fra_ingest.table_grid import Grid

_PER_SHARE = tuple(squash(w) for w in ("per share", "للسهم", "ربحية السهم"))


class PartialStatement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: StatementType
    confidence: float
    first_page: int
    last_page: int
    page_width: float
    periods: list[Period]
    column_centres: dict[str, float] = Field(default_factory=dict)
    line_items: list[LineItem] = Field(default_factory=list)
    indents: dict[str, float | None] = Field(default_factory=dict)
    header_text: str = ""
    table_refs: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


def column_centre(grid: Grid, col: int, rows: Sequence[int]) -> float | None:
    centres = []
    for row in rows:
        cell = grid.cell(row, col)
        if cell is not None and cell.text and cell.bbox is not None:
            centres.append((cell.bbox.left + cell.bbox.right) / 2)
    return sum(centres) / len(centres) if centres else None


def _band(
    grid: Grid, *, row: int | None = None, col: int | None = None
) -> tuple[float, float] | None:
    boxes = [
        c.bbox
        for c in grid.cells
        if c.bbox is not None and (row is None or c.row == row) and (col is None or c.col == col)
    ]
    if not boxes:
        return None
    if row is not None:
        return min(b.top for b in boxes), max(b.bottom for b in boxes)
    return min(b.left for b in boxes), max(b.right for b in boxes)


def _synthesized_box(grid: Grid, row: int, col: int) -> BBox | None:
    rows, cols = _band(grid, row=row), _band(grid, col=col)
    if rows is None or cols is None:
        return None
    return BBox(left=cols[0], top=rows[0], right=cols[1], bottom=rows[1])


def build_part(
    grid: Grid,
    layout: HeaderLayout,
    classification: Classification,
    *,
    source: TextSource,
) -> PartialStatement:
    if classification.type is None:
        msg = "only a classified grid becomes part of a statement"
        raise ValueError(msg)
    data_rows = layout.data_rows(grid)
    periods = list(layout.value_cols.values())
    right = grid.right_edge()
    label_on_right = layout.label_col is not None and layout.label_col == max(
        (c for c in range(grid.num_cols) if c in layout.value_cols or c == layout.label_col),
        default=-1,
    )
    part = PartialStatement(
        type=classification.type,
        confidence=classification.confidence,
        first_page=grid.page_no,
        last_page=grid.page_no,
        page_width=grid.page_width,
        periods=periods,
        header_text=" ".join(layout.header_texts.values()),
        table_refs=[f"{grid.docling_path}{grid.table_ref}"],
        flags=list(layout.evidence),
    )
    for col, period in layout.value_cols.items():
        centre = column_centre(grid, col, data_rows)
        if centre is not None:
            part.column_centres[period.key] = centre

    for row in data_rows:
        label_cell = grid.cell(row, layout.label_col) if layout.label_col is not None else None
        label = label_cell.text if label_cell is not None else ""
        note = grid.text(row, layout.note_col) if layout.note_col is not None else ""
        if not note:
            label, split = split_note(label)
            note = split or ""
        texts = {col: grid.text(row, col) for col in layout.value_cols}
        if not label and not any(texts.values()):
            continue
        item_id = f"p{grid.page_no}-{grid.table_index}-r{row}"
        per_share = any(cue in squash(label) for cue in _PER_SHARE)
        has_values = any(texts.values())
        cells = []
        for col, period in layout.value_cols.items():
            if not has_values:
                break
            grid_cell = grid.cell(row, col)
            text = texts[col]
            parsed = parse_number(text) if text else None
            flags = list(parsed.flags) if parsed else ["numbers_missing"]
            if grid_cell is not None:
                flags.extend(grid_cell.flags)
            if per_share:
                flags.append("per_share")
            bbox = (
                grid_cell.bbox
                if grid_cell is not None and grid_cell.bbox is not None
                else _synthesized_box(grid, row, col)
            )
            if bbox is None:
                flags.append("no_box")
                continue
            if grid_cell is None or grid_cell.bbox is None:
                flags.append("bbox_synthesized")
            cells.append(
                Cell(
                    period_key=period.key,
                    reported=parsed.value if parsed else None,
                    raw_text=text,
                    provenance=Provenance(
                        page_no=grid.page_no,
                        bbox=bbox,
                        table_ref=grid.table_ref,
                        row=row,
                        col=col,
                        source=source,
                    ),
                    flags=flags,
                )
            )
        part.line_items.append(
            LineItem(id=item_id, raw_label=label, note_ref=note or None, cells=cells)
        )
        indent = None
        if label_cell is not None and label_cell.bbox is not None:
            if label_on_right and right is not None:
                indent = right - label_cell.bbox.right
            else:
                indent = label_cell.bbox.left
        part.indents[item_id] = indent
    return part
