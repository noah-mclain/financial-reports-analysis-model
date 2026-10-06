"""Read missing printed period labels without rewriting a table's source cells.

A recovery requires a complete, unique row of labels above the first data row. The crop
is bounded by the table and its nearest caption, and uses the configured engine only.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from statistics import median

import pypdfium2 as pdfium
from PIL import Image

from fra_core.numbers import normalize_digits
from fra_core.periods import parse_period
from fra_core.schemas import BBox, PeriodKind, StatementType
from fra_ingest.config import IngestConfig
from fra_ingest.docling_json import DlDocument
from fra_ingest.header import (
    has_period_context,
    is_amount,
    is_printed_period_label,
    parse_header,
    period_with_context,
    printed_date,
)
from fra_ingest.ocr import OcrEngine, OcrLine
from fra_ingest.table_grid import Grid, GridCell

_YEAR = re.compile(r"(?:19|20)\d{2}")
_PROGRAMMING_ERRORS = (TypeError, AttributeError, NameError, IndexError, KeyError)


@dataclass(frozen=True)
class RasterCrop:
    """The actual rendered extent in displayed PDF points, including pixel rounding."""

    image: Image.Image
    bbox: BBox
    page_width: float
    page_height: float

    def line_box(self, line: OcrLine) -> BBox | None:
        values = (line.left, line.top, line.width, line.height, line.confidence)
        if not all(math.isfinite(v) for v in values):
            return None
        if not (
            0 <= line.left < line.left + line.width <= 1
            and 0 <= line.top < line.bottom <= 1
            and 0 <= line.confidence <= 1
        ):
            return None
        width = self.bbox.right - self.bbox.left
        height = self.bbox.bottom - self.bbox.top
        return BBox(
            left=self.bbox.left + line.left * width,
            top=self.bbox.top + line.top * height,
            right=self.bbox.left + (line.left + line.width) * width,
            bottom=self.bbox.top + line.bottom * height,
        )


def render_crop(pdf: Path, page_no: int, bbox: BBox, scale: float) -> RasterCrop:
    """Render only the requested region; PDFium crops in displayed bitmap space."""
    with pdfium.PdfDocument(pdf) as document:
        page = document[page_no - 1]
        try:
            width, height = page.get_size()
            if not _valid_box(bbox, width, height):
                raise ValueError(f"header crop outside page {page_no}: {bbox}")
            margins = (bbox.left, height - bbox.bottom, width - bbox.right, bbox.top)
            bitmap = page.render(scale=scale, crop=margins)
            try:
                image = bitmap.to_pil().copy()
            finally:
                bitmap.close()
            # PDFium maps the displayed page into ceil(width*scale) pixels, then removes
            # ceil(margin*scale) pixels on each side. Neither crop nor page need be integral.
            xscale, yscale = math.ceil(width * scale) / width, math.ceil(height * scale) / height
            left, top = math.ceil(bbox.left * scale) / xscale, math.ceil(bbox.top * scale) / yscale
            actual = BBox(
                left=left,
                top=top,
                right=left + image.width / xscale,
                bottom=top + image.height / yscale,
            )
            return RasterCrop(image, actual, width, height)
        finally:
            page.close()


def _valid_box(box: BBox, width: float, height: float) -> bool:
    return (
        all(math.isfinite(v) for v in (box.left, box.top, box.right, box.bottom))
        and 0 <= box.left < box.right <= width
        and 0 <= box.top < box.bottom <= height
    )


def _overlap(a: BBox, b: BBox) -> bool:
    return a.left < b.right and b.left < a.right and a.top < b.bottom and b.top < a.bottom


def _flag(grid: Grid, reason: str) -> Grid:
    return grid.model_copy(update={"flags": (*grid.flags, f"header_recovery_{reason}")})


def _caption(lines: list[tuple[OcrLine, BBox]], top: float) -> list[tuple[OcrLine, BBox]]:
    above = sorted(
        ((line, box) for line, box in lines if box.bottom <= top),
        key=lambda x: (x[1].top, x[1].left),
    )
    dated = next((i for i in range(len(above) - 1, -1, -1) if printed_date(above[i][0].text)), None)
    # Duration wording remains evidence even when its line carries no date, or an invalid
    # one. Validation must see it rather than replacing missing/uncertain context with FY.
    return [item for i, item in enumerate(above) if i == dated or has_period_context(item[0].text)]


def recover_header(
    grid: Grid, document: DlDocument, pdf: Path, config: IngestConfig, ocr: OcrEngine | None
) -> Grid:
    layout = parse_header(grid, StatementType.BALANCE, None)
    # A partial or already valid binding retains exactly its existing interpretation.
    if layout.value_cols or len(layout.unbound_cols) < 2 or grid.recovered_headers:
        return grid
    size = document.page_size(grid.page_no)
    if any(
        c.page_no != grid.page_no
        or (c.bbox is not None and not _valid_box(c.bbox, size.width, size.height))
        for c in grid.cells
    ):
        return _flag(grid, "geometry_invalid")
    table = next(t for t in document.tables if t.self_ref == grid.table_ref)
    box = table.prov[0].bbox.to_bbox(size.height)
    if box is None or not _valid_box(box, size.width, size.height):
        return _flag(grid, "geometry_invalid")
    amount_cells = [
        c
        for c in grid.cells
        if c.col in layout.unbound_cols and c.row not in layout.header_rows and is_amount(c.text)
    ]
    if not amount_cells or any(
        c.page_no != grid.page_no
        or c.col_span != 1
        or c.bbox is None
        or not _valid_box(c.bbox, size.width, size.height)
        for c in amount_cells
    ):
        return _flag(grid, "geometry_invalid")
    first_row = min(c.row for c in amount_cells)
    first_boxes = [
        c.bbox for c in grid.cells if c.row == first_row and c.text and c.bbox is not None
    ]
    bottom = min(b.top for b in first_boxes)
    if bottom <= box.top:
        return _flag(grid, "geometry_invalid")
    header_band = box.model_copy(update={"bottom": bottom})
    siblings = [
        t
        for t in document.tables
        if t.self_ref != grid.table_ref and t.prov[0].page_no == grid.page_no
    ]
    sibling_boxes = [t.prov[0].bbox.to_bbox(size.height) for t in siblings]
    if any(
        b is None or not _valid_box(b, size.width, size.height) or _overlap(b, header_band)
        for b in sibling_boxes
    ):
        return _flag(grid, "ambiguous_table")
    floor = max(
        (b.bottom for b in sibling_boxes if b is not None and b.bottom <= box.top), default=0.0
    )
    captions = []
    for text in document.texts_on(grid.page_no):
        b = text.prov[0].bbox.to_bbox(size.height)
        if (
            b is not None
            and _valid_box(b, size.width, size.height)
            and floor <= b.top < box.top
            and b.bottom <= bottom
            and b.left < box.right
            and box.left < b.right
        ):
            captions.append(b)
    nearest = max(captions, key=lambda b: b.bottom) if captions else None
    crop_box = header_band.model_copy(
        update={"top": min(box.top, nearest.top) if nearest else box.top}
    )
    bands: dict[int, tuple[float, float]] = {}
    for col in layout.unbound_cols:
        boxes = [c.bbox for c in amount_cells if c.col == col and c.bbox is not None]
        if len(boxes) < 2:
            return _flag(grid, "geometry_invalid")
        bands[col] = median(b.left for b in boxes), median(b.right for b in boxes)
    if ocr is None:
        return _flag(grid, "unavailable")
    try:
        crop = render_crop(pdf, grid.page_no, crop_box, config.header_ocr_scale)
        try:
            lines = ocr.recognize(crop.image, config.ocr_languages)
        finally:
            crop.image.close()
    except _PROGRAMMING_ERRORS:
        raise
    except Exception as exc:
        # Native bridge errors vary by engine. Preserve a visible failure and retry the
        # structure stage next time; never use a different engine or an inferred year.
        return _flag(grid, f"failed:{type(exc).__name__}:{' '.join(str(exc).split())[:160]}")
    if not (
        math.isclose(crop.page_width, size.width, abs_tol=0.01)
        and math.isclose(crop.page_height, size.height, abs_tol=0.01)
    ):
        return _flag(grid, "geometry_invalid")
    observed: list[tuple[OcrLine, BBox]] = []
    labels: dict[int, list[tuple[OcrLine, BBox]]] = {col: [] for col in bands}
    for line in lines:
        b = crop.line_box(line)
        date = is_printed_period_label(line.text)
        if b is None:
            if date:
                return _flag(grid, "geometry_invalid")
            continue
        if not _valid_box(b, size.width, size.height) or line.confidence < config.min_confidence:
            if date:
                return _flag(grid, "uncertain")
            continue
        observed.append((line, b))
        if not date or not (header_band.top <= b.top < b.bottom <= header_band.bottom):
            continue
        cols = [col for col, (left, right) in bands.items() if left <= b.left < b.right <= right]
        if len(cols) != 1:
            return _flag(grid, "ambiguous_columns")
        labels[cols[0]].append((line, b))
    if any(len(value) != 1 for value in labels.values()):
        return _flag(grid, "labels_missing_or_conflicting")
    selected = [value[0] for value in labels.values()]
    if max(b.top for _, b in selected) >= min(b.bottom for _, b in selected):
        return _flag(grid, "ambiguous_rows")
    periods = [parse_period(line.text) for line, _ in selected]
    if any(p is None for p in periods) or len({p.key for p in periods if p is not None}) != len(
        periods
    ):
        return _flag(grid, "conflicting_periods")
    caption = _caption(observed, min(b.top for _, b in selected))
    context = " ".join(line.text for line, _ in caption)
    if (
        any(_YEAR.fullmatch(normalize_digits(line.text)[0].strip()) for line, _ in selected)
        and not caption
    ):
        return _flag(grid, "period_uncertain")
    for line, _ in selected:
        if (
            period_with_context(line.text, context, PeriodKind.DURATION, match_caption_date=True)
            is None
        ):
            conflict = period_with_context(line.text, context, PeriodKind.DURATION) is not None
            return _flag(grid, "conflicting_periods" if conflict else "period_uncertain")
    headers = tuple(
        GridCell(
            text=values[0][0].text,
            row=-1,
            col=col,
            bbox=values[0][1],
            is_column_header=True,
            page_no=grid.page_no,
            flags=("header_recovered",),
        )
        for col, values in labels.items()
    )
    context_cells = tuple(
        GridCell(
            text=line.text,
            row=-1,
            col=-1,
            bbox=b,
            page_no=grid.page_no,
            flags=("header_recovery_context",),
        )
        for line, b in caption
    )
    return grid.model_copy(
        update={
            "recovered_headers": headers,
            "recovered_context": context_cells,
            "flags": (*grid.flags, "header_recovered"),
        }
    )
