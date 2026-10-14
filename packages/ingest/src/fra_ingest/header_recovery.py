"""Read missing printed period labels without rewriting a table's source cells.

A recovery requires a unique printed label for each missing slot above the first data row. The crop
is bounded by the table and its nearest caption, and uses the configured engine only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from statistics import median

import pypdfium2 as pdfium
from PIL import Image

from fra_core.periods import interpret_period, parse_period
from fra_core.schemas import BBox, PeriodKind, Provenance, StatementType, TextSource
from fra_core.schemas.statement import PeriodEvidence, PeriodObservation
from fra_core.units import is_glyph_placeholder, unit_marker_spans
from fra_ingest.config import IngestConfig
from fra_ingest.docling_json import DlDocument, DlTable
from fra_ingest.header import (
    _is_sub_header_text,
    has_period_context,
    is_amount,
    is_printed_period_label,
    parse_header,
    period_with_context,
    printed_date,
)
from fra_ingest.ocr import OcrEngine, OcrLine
from fra_ingest.period_evidence import contained, source_cell
from fra_ingest.table_grid import Grid, GridCell

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
    covered = {e.col for e in grid.period_evidence}
    flags = [*grid.flags, f"header_recovery_{reason}"]
    for col in parse_header(grid, StatementType.BALANCE, None).value_cols:
        if col not in covered and not any(
            f.startswith(f"period_evidence_unavailable:{col}:") for f in flags
        ):
            flags.append(f"period_evidence_unavailable:{col}:{reason}")
    return grid.model_copy(update={"flags": tuple(flags)})


def _unavailable(grid: Grid, cols: list[int], reason: str) -> Grid:
    flags = (f"period_evidence_unavailable:{col}:{reason}" for col in cols)
    return grid.model_copy(update={"flags": (*grid.flags, *flags)})


def _only_units(text: str) -> bool:
    """Unit text is marker spans and punctuation, with a glyph placeholder beside a marker."""
    covered = {i for start, end in unit_marker_spans(text) for i in range(start, end)}
    rest = "".join(" " if i in covered else c for i, c in enumerate(text))
    return bool(covered) and all(
        is_glyph_placeholder(token) or not token.strip("()[]'\".,:") for token in rest.split()
    )


def _primary_observation(
    grid: Grid,
    table: DlTable,
    anchor: GridCell,
    height: float,
    scope: str,
    header_rows: list[int],
) -> PeriodObservation | None:
    """A unique, unit-span original Docling cell, never the repaired grid literal."""
    source = source_cell(table, anchor.row, anchor.col, grid.page_no)
    box = anchor.bbox
    if source is None or box is None or anchor.row < 0 or anchor.col < 0:
        return None
    if any(
        c.row in header_rows
        and c.row != anchor.row
        and c.col <= anchor.col < c.col + c.col_span
        and c.text
        and not _only_units(c.text)
        for c in grid.cells
    ):
        return None
    index, cell = source
    if (
        cell.bbox is None
        or not cell.text.strip()
        or cell.bbox.to_bbox(height) != box
        or not all(
            math.isfinite(v)
            for v in (cell.bbox.left, cell.bbox.top, cell.bbox.right, cell.bbox.bottom)
        )
    ):
        return None
    return PeriodObservation(
        source_id=f"docling-cell:{scope}:{index}",
        raw_text=cell.text,
        docling_path=grid.docling_path,
        # This path runs only for image-page Docling OCR, whose token confidence and
        # token identity are not present in DlCell. The ID names the cell stage only.
        provenance=Provenance(
            page_no=grid.page_no,
            table_ref=grid.table_ref,
            row=anchor.row,
            col=anchor.col,
            source=TextSource.OCR,
            bbox=box,
        ),
    )


def _collect_evidence(
    grid: Grid,
    primaries: dict[int, PeriodObservation],
    crop: RasterCrop,
    lines: list[OcrLine],
    header_rows: list[int],
    scope: str,
) -> Grid:
    """Capture before missing-slot validation; confidence does not choose a reading."""
    groups: list[PeriodEvidence] = []
    flags = list(grid.flags)
    for col, primary in primaries.items():
        anchor = primary.provenance.bbox
        observations = [primary]
        reasons: set[str] = set()
        for index, line in enumerate(lines):
            box = crop.line_box(line)
            if box is None:
                reasons.add("invalid_line_geometry")
                continue
            if not _overlap(box, anchor):
                continue
            if not (_valid_box(box, crop.page_width, crop.page_height) and contained(box, anchor)):
                reasons.add("outside_source_rectangle")
                continue
            if any(
                c.bbox is not None
                and c.row in header_rows
                and (c.row, c.col) != (primary.provenance.row, col)
                and _overlap(box, c.bbox)
                for c in grid.cells
            ):
                reasons.add("ambiguous_source_geometry")
                continue
            if not line.text:
                reasons.add("empty_reading")
                continue
            observations.append(
                PeriodObservation(
                    source_id=f"header-crop-line:{scope}:{index}",
                    raw_text=line.text,
                    confidence=line.confidence,
                    docling_path=grid.docling_path,
                    provenance=primary.provenance.model_copy(update={"bbox": box}),
                )
            )
        if len(observations) > 1:
            groups.append(PeriodEvidence(observations=tuple(observations)))
        elif not reasons:
            reasons.add("no_attributable_reading")
        flags.extend(f"period_evidence_unavailable:{col}:{r}" for r in sorted(reasons))
    return grid.model_copy(update={"period_evidence": tuple(groups), "flags": tuple(flags)})


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
    if not layout.unbound_cols or grid.recovered_headers:
        return grid
    # Missing calendar facts can be read; malformed, duplicate and explicit duration
    # facts cannot be replaced by a more convenient crop reading.
    targets = [
        col
        for col in layout.unbound_cols
        if (facts := interpret_period(layout.header_texts.get(col, ""))).calendar == "missing"
        and facts.duration == "none"
        and (
            not layout.value_cols
            or not layout.header_texts.get(col)
            or _is_sub_header_text(layout.header_texts[col])
        )
    ]
    if not targets or (not layout.value_cols and len(targets) < 2):
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
        if c.col in (*layout.unbound_cols, *layout.value_cols)
        and c.row not in layout.header_rows
        and is_amount(c.text)
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
    for col in (*layout.unbound_cols, *layout.value_cols):
        boxes = [c.bbox for c in amount_cells if c.col == col and c.bbox is not None]
        if len(boxes) < 2 and col in targets:
            return _flag(grid, "geometry_invalid")
        if boxes:
            bands[col] = median(b.left for b in boxes), median(b.right for b in boxes)
    bound_boxes: list[BBox] = []
    primaries: dict[int, PeriodObservation] = {}
    unanchored: list[int] = []
    scope = sha256(f"{grid.docling_path}\0{grid.page_no}\0{grid.table_ref}".encode()).hexdigest()
    for col in layout.value_cols:
        anchors = [
            c
            for c in grid.cells
            if c.col <= col < c.col + c.col_span
            and c.row in layout.header_rows
            and is_printed_period_label(c.text)
        ]
        # A partial read needs a unique original row anchor in every bound column.
        # Alternate OCR over that column cannot supply missing source geometry.
        if len(anchors) != 1 or col not in bands:
            return _flag(
                _unavailable(grid, unanchored, "source_anchor_invalid"), "geometry_invalid"
            )
        anchor = anchors[0]
        b = anchor.bbox
        left, right = bands[col]
        if (
            anchor.row_span != 1
            or anchor.col_span != 1
            or b is None
            or not _valid_box(b, size.width, size.height)
            or not (left <= b.left < b.right <= right)
            or not (header_band.top <= b.top < b.bottom <= header_band.bottom)
        ):
            return _flag(
                _unavailable(grid, unanchored, "source_anchor_invalid"), "geometry_invalid"
            )
        bound_boxes.append(b)
        primary = _primary_observation(grid, table, anchor, size.height, scope, layout.header_rows)
        if primary is None:
            unanchored.append(col)
        else:
            primaries[col] = primary
    # No comparison for a column without a usable original cell; the missing slots can
    # still be read, as they were before bound columns were observed.
    grid = _unavailable(grid, unanchored, "source_anchor_invalid")
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
    # Pixel rounding can move the rendered extent a fraction of a point past the requested
    # one. That never stopped a missing slot being read; it does stop a bound column's
    # alternate reading being attributed to this table's own cells.
    if _valid_box(crop.bbox, size.width, size.height) and not any(
        b is not None and _overlap(b, crop.bbox) for b in sibling_boxes
    ):
        grid = _collect_evidence(grid, primaries, crop, lines, layout.header_rows, scope)
    else:
        grid = _unavailable(grid, list(primaries), "geometry_invalid")
    observed: list[tuple[OcrLine, BBox]] = []
    labels: dict[int, list[tuple[OcrLine, BBox]]] = {col: [] for col in targets}
    for line in lines:
        b = crop.line_box(line)
        facts = interpret_period(line.text)
        date = is_printed_period_label(line.text) or facts.calendar != "missing"
        if b is None:
            if date:
                return _flag(grid, "geometry_invalid")
            continue
        # Reads wholly over an existing column are not candidate labels for a missing
        # column. Cross-column and unlocatable readings remain ambiguous.
        cols = [col for col, (left, right) in bands.items() if left <= b.left < b.right <= right]
        if (
            date
            and header_band.top <= b.top < b.bottom <= header_band.bottom
            and len(cols) == 1
            and cols[0] not in targets
        ):
            continue
        if not _valid_box(b, size.width, size.height) or line.confidence < config.min_confidence:
            if date:
                return _flag(grid, "uncertain")
            continue
        observed.append((line, b))
        if not date or not (header_band.top <= b.top < b.bottom <= header_band.bottom):
            continue
        if len(cols) != 1:
            return _flag(grid, "ambiguous_columns")
        if not is_printed_period_label(line.text):
            return _flag(grid, "period_uncertain")
        labels[cols[0]].append((line, b))
    if any(len(value) != 1 for value in labels.values()):
        return _flag(grid, "labels_missing_or_conflicting")
    selected = [value[0] for value in labels.values()]
    row_boxes = [b for _, b in selected] + bound_boxes
    if max(b.top for b in row_boxes) >= min(b.bottom for b in row_boxes):
        return _flag(grid, "ambiguous_rows")
    for col, values in labels.items():
        original = layout.header_texts.get(col, "")
        if original:
            # The header consumer parses the recovered label alone. Use its printed
            # date fragment and the core parser to detect original status facts;
            # these cannot be discarded or copied into an invented OCR observation.
            date_text = interpret_period(values[0][0].text).date_text
            status = parse_period(f"{date_text} {original}")
            if status is None or status.restated or status.audited is not None:
                return _flag(grid, "status_unpreserved")
    periods = [parse_period(line.text) for line, _ in selected]
    if any(p is None for p in periods) or len({p.key for p in periods if p is not None}) != len(
        periods
    ):
        return _flag(grid, "conflicting_periods")
    caption = _caption(observed, min(b.top for _, b in selected))
    context = " ".join(line.text for line, _ in caption)
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
    # Preserve the printed uncertainty even when it prevents recovering any binding.
    grid = grid.model_copy(update={"recovered_context": context_cells})
    if (
        any(interpret_period(line.text).calendar == "year_only" for line, _ in selected)
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
    recovered = grid.model_copy(
        update={
            "recovered_headers": headers,
            "recovered_context": context_cells,
            "flags": (*grid.flags, "header_recovered"),
        }
    )
    # Recovered context is shared by recovered labels only. Check both statement kinds
    # so a new earlier column cannot steal an existing period through duplicate binding.
    for kind in (StatementType.BALANCE, StatementType.INCOME):
        before = parse_header(grid, kind, None)
        after = parse_header(recovered, kind, None)
        # A year-only original can take a printed date hint downstream. Do not add a
        # same-year target which could displace that original when the hint is applied.
        if any(
            interpret_period(before.header_texts[col]).calendar == "year_only"
            and any(
                after.value_cols[target].end_date.year == period.end_date.year
                for target in targets
                if target in after.value_cols
            )
            for col, period in before.value_cols.items()
        ):
            return _flag(grid, "conflicting_periods")
        if any(
            after.value_cols.get(col) != period for col, period in before.value_cols.items()
        ) or any(col not in after.value_cols for col in targets):
            return _flag(grid, "conflicting_periods")
    return recovered
