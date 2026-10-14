"""One classified grid as part of a statement: line items with provenance (spec 11, step 7).

Values are kept as printed, sign applied, before scale. An empty value cell in a row that has
values elsewhere is kept with ``numbers_missing`` and a box synthesized from its row and
column, so every gap stays visible and traceable. A row with no values at all is a heading,
unless its label opens with a total cue and names a taxonomy item (``Total assets``): such a
row never prints without figures, so its cells are kept as missing and the checks report the
gap. A section heading the taxonomy also knows (``Current assets``) stays a heading.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from statistics import median

from pydantic import BaseModel, ConfigDict, Field

from fra_core.labels import normalize_label
from fra_core.numbers import parse_number
from fra_core.schemas import BBox, Cell, LineItem, Period, Provenance, StatementType, TextSource
from fra_core.schemas.statement import PeriodConflict
from fra_ingest.classify import Classification
from fra_ingest.header import HeaderLayout, split_note
from fra_ingest.label_match import LabelIndex, has_subtotal_cue, squash
from fra_ingest.table_grid import Grid

# Larger than any printed statement figure, before scale: merged digit groups (OCR).
IMPLAUSIBLE = 10**15
# Two merged figures can stay under that limit, so a value is also implausible when it is this
# many times the median of its own column, given enough values to have a median.
IMPLAUSIBLE_RATIO = 10**4
_MIN_COLUMN_VALUES = 5
# Arabic cues are matched with spaces ignored, as these labels often lose theirs. "صيب السهم"
# is "نصيب السهم" with or without its first letter, which OCR drops.
_PER_SHARE = tuple(
    squash(w) for w in ("للسهم", "ربحية السهم", "صيب السهم", "لكل سهم", "على السهم", "حصة السهم")
)
_PER_SHARE_WORDS = frozenset({"eps", "dps"})
# "per share", "per ordinary share": at most one word between.
_PER_SHARE_BETWEEN = frozenset({"ordinary", "common", "basic", "diluted"})
# The rows a per-share heading reaches, and no others.
_UNDER_HEADING = tuple(
    squash(w)
    for w in ("basic", "diluted", "continuing", "discontinued", "أساسي", "مخفض", "المستمرة")
)


class PartialStatement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period_conflicts: tuple[PeriodConflict, ...] = ()

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


def is_per_share(label: str) -> bool:
    """A row that states an amount per share, not an amount of the statement's unit."""
    words = normalize_label(label).split()
    if _PER_SHARE_WORDS & set(words) or any(cue in squash(label) for cue in _PER_SHARE):
        return True
    for position, word in enumerate(words[:-1]):
        if word != "per":
            continue
        after = words[position + 1 : position + 3]
        if after[0] == "share" or (
            len(after) == 2 and after[0] in _PER_SHARE_BETWEEN and after[1] == "share"
        ):
            return True
    return False


def _under_per_share_heading(label: str) -> bool:
    return any(cue in squash(label) for cue in _UNDER_HEADING)


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


def _flag_column_outliers(part: PartialStatement) -> None:
    """Add ``implausible_magnitude`` to values far beyond their column. Per-share values and
    zeros are neither judged nor counted."""
    for period in part.periods:
        sized = [
            abs(c.reported)
            for item in part.line_items
            for c in item.cells
            if c.period_key == period.key and c.reported and "per_share" not in c.flags
        ]
        if len(sized) < _MIN_COLUMN_VALUES:
            continue
        limit = IMPLAUSIBLE_RATIO * median(sized)
        for n, item in enumerate(part.line_items):
            cells = [
                c.model_copy(update={"flags": [*c.flags, "implausible_magnitude"]})
                if c.period_key == period.key
                and c.reported
                and abs(c.reported) > limit
                and not {"per_share", "implausible_magnitude"} & set(c.flags)
                else c
                for c in item.cells
            ]
            part.line_items[n] = item.model_copy(update={"cells": cells})


def build_part(
    grid: Grid,
    layout: HeaderLayout,
    classification: Classification,
    *,
    source: TextSource,
    index: LabelIndex | None = None,
    merged_rows: Collection[int] = (),
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
        period_conflicts=layout.period_conflicts,
        header_text=" ".join(layout.header_texts.values()),
        table_refs=[f"{grid.docling_path}{grid.table_ref}"],
        flags=[*layout.evidence, *grid.flags],
    )
    for col, period in layout.value_cols.items():
        centre = column_centre(grid, col, data_rows)
        if centre is not None:
            part.column_centres[period.key] = centre

    under_per_share = False  # below a per-share heading, until the next heading
    for row in data_rows:
        label_cell = grid.cell(row, layout.label_col) if layout.label_col is not None else None
        label = label_cell.text if label_cell is not None else ""
        note = grid.text(row, layout.note_col) if layout.note_col is not None else ""
        if not note:
            label, split = split_note(label)
            note = split or ""
        covering = {col: grid.cell(row, col) for col in layout.value_cols}
        # A value belongs only to the column it starts in; the rest of a span is empty.
        starts = {
            col: c if c is not None and (c.row, c.col) == (row, col) else None
            for col, c in covering.items()
        }
        texts = {col: c.text if c is not None else "" for col, c in starts.items()}
        if not label and not any(texts.values()):
            continue
        item_id = f"p{grid.page_no}-{grid.table_index}-r{row}"
        named_per_share = is_per_share(label)
        known = index.match(label, classification.type) if index is not None else None
        # A known total with every value lost keeps its cells, each flagged as missing.
        has_values = any(texts.values()) or (known is not None and has_subtotal_cue(label))
        if not has_values:
            under_per_share = named_per_share
        per_share = named_per_share or (under_per_share and _under_per_share_heading(label))
        cells = []
        for col, period in layout.value_cols.items():
            if not has_values:
                break
            grid_cell = starts[col]
            text = texts[col]
            parsed = parse_number(text) if text else None
            flags = list(parsed.flags) if parsed else ["numbers_missing"]
            if parsed and parsed.value is not None and abs(parsed.value) > IMPLAUSIBLE:
                flags.append("implausible_magnitude")
            if grid_cell is not None:
                flags.extend(grid_cell.flags)
            elif covering[col] is not None:
                flags.append("spanned_cell")
            if per_share:
                flags.append("per_share")
            if row in merged_rows:
                flags.append("label_merged")
            bbox = (
                grid_cell.bbox
                if grid_cell is not None and grid_cell.bbox is not None
                else _synthesized_box(grid, row, col)
            )
            if bbox is None:
                if text:
                    part.flags.append(f"value_without_box:r{row}c{col}")
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
                        row=grid_cell.source_row
                        if grid_cell is not None and grid_cell.source_row is not None
                        else row,
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
    _flag_column_outliers(part)
    return part
