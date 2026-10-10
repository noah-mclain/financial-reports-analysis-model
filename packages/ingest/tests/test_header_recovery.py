"""Recorded positive crop reads against the original missing-header grids.

The fixture contains source cells and OCR lines, never a PDF or page image. Recognition
is injected at the engine boundary; crop coordinates are transformed independently.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image
from pydantic import BaseModel

from fra_core.schemas import BBox, StatementType
from fra_ingest import header_recovery
from fra_ingest.config import IngestConfig
from fra_ingest.docling_json import DlDocument, DlPage, DlTable, DlText
from fra_ingest.header import parse_header
from fra_ingest.ocr import OcrLine
from fra_ingest.table_grid import Grid, build_grid

FIXTURE = Path(__file__).parent / "fixtures/header_recovery.json"


class RecordedCrop(BaseModel):
    page_points: tuple[float, float]
    crop_pixels: tuple[int, int]
    crop_origin_points: tuple[float, float]
    crop_height_points: float
    lines: list[OcrLine]


class Observation(BaseModel):
    docling_path: str
    table: DlTable
    page: DlPage
    texts: list[DlText]
    ocr: RecordedCrop

    @property
    def grid(self) -> Grid:
        return build_grid(self.table, self.document(), self.docling_path)

    def document(self) -> DlDocument:
        return DlDocument(
            tables=[self.table], texts=self.texts, pages={str(self.page.page_no): self.page}
        )


OBSERVED = [Observation.model_validate(x) for x in json.loads(FIXTURE.read_text())]


class RecordedEngine:
    name = "vision"

    def __init__(self, observation: Observation) -> None:
        self.observation = observation
        self.lines = observation.ocr.lines
        self.calls: list[tuple[str, ...]] = []

    def recognize(self, image: Image.Image, languages: Sequence[str]) -> list[OcrLine]:
        self.calls.append(tuple(languages))
        crop = BBox.model_validate(image.info["crop"])
        recorded = self.observation.ocr
        width, _height = recorded.page_points
        height = recorded.crop_height_points
        result = []
        for line in self.lines:
            left, top = line.left * width, line.top * height
            right, bottom = (line.left + line.width) * width, line.bottom * height
            if not (
                crop.left <= left < right <= crop.right and crop.top <= top < bottom <= crop.bottom
            ):
                continue
            result.append(
                replace(
                    line,
                    left=(left - crop.left) / (crop.right - crop.left),
                    top=(top - crop.top) / (crop.bottom - crop.top),
                    width=(right - left) / (crop.right - crop.left),
                    height=(bottom - top) / (crop.bottom - crop.top),
                )
            )
        return result


def inject_render(monkeypatch: pytest.MonkeyPatch, observation: Observation) -> list[BBox]:
    requests: list[BBox] = []

    def render(_pdf: Path, page_no: int, bbox: BBox, scale: float) -> header_recovery.RasterCrop:
        assert page_no == observation.grid.page_no
        requests.append(bbox)
        image = Image.new(
            "RGB",
            (round((bbox.right - bbox.left) * scale), round((bbox.bottom - bbox.top) * scale)),
        )
        image.info["crop"] = bbox.model_dump()
        return header_recovery.RasterCrop(image, bbox, *observation.ocr.page_points)

    monkeypatch.setattr(header_recovery, "render_crop", render)
    return requests


@pytest.mark.parametrize("observation", OBSERVED, ids=["page5", "page6"])
def test_recorded_years_bind_original_columns_without_changing_any_source_cell(
    observation: Observation, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = observation.grid
    assert parse_header(before, StatementType.BALANCE, None).unbound_cols == [0, 1]
    requests = inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    recovered = header_recovery.recover_header(
        before, observation.document(), Path("unused.pdf"), IngestConfig(), engine
    )
    layout = parse_header(recovered, StatementType.BALANCE, None)
    assert {col: p.key for col, p in layout.value_cols.items()} == {
        0: "2023-12-31",
        1: "2024-12-31",
    }
    assert recovered.cells == before.cells
    assert recovered.num_rows == before.num_rows
    assert layout.unbound_cols == []
    assert "header_recovered" in recovered.flags
    assert engine.calls == [("ar-SA", "en-US")]
    assert len(requests) == 1
    assert requests[0].top > 0 and requests[0].bottom < 180


def recover(
    observation: Observation, monkeypatch: pytest.MonkeyPatch, lines: list[OcrLine]
) -> Grid:
    inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    engine.lines = lines
    return header_recovery.recover_header(
        observation.grid, observation.document(), Path("unused.pdf"), IngestConfig(), engine
    )


def years(observation: Observation) -> list[OcrLine]:
    return [line for line in observation.ocr.lines if line.text in ("٢٠٢٣", "٢٠٢٤")]


@pytest.mark.parametrize(
    "case",
    [
        "duplicate",
        "conflict",
        "same_period",
        "caption",
        "body",
        "note",
        "span",
        "wrong_column",
        "different_rows",
        "missing",
    ],
)
def test_uncertain_labels_never_bind(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    observation = OBSERVED[1]
    first, second = years(observation)
    lines = list(observation.ocr.lines)
    if case == "duplicate":
        lines.append(first)
    elif case == "conflict":
        lines.append(replace(first, text="2022"))
    elif case == "same_period":
        lines[lines.index(second)] = replace(second, text=first.text)
    elif case == "caption":
        lines = [replace(line, top=0.46) if line in (first, second) else line for line in lines]
    elif case == "body":
        lines = [replace(line, top=0.93) if line in (first, second) else line for line in lines]
    elif case == "note":
        lines[lines.index(first)] = replace(first, text="(23)")
    elif case == "span":
        lines[lines.index(first)] = replace(first, width=0.2)
    elif case == "wrong_column":
        lines[lines.index(first)] = replace(first, left=0.44)
    elif case == "different_rows":
        lines[lines.index(first)] = replace(first, top=0.60)
    else:
        lines.remove(first)
    result = recover(observation, monkeypatch, lines)
    assert not parse_header(result, StatementType.INCOME, None).value_cols
    assert result.cells == observation.grid.cells
    assert any(flag.startswith("header_recovery_") for flag in result.flags)


@pytest.mark.parametrize("digits", [("2023", "2024"), ("۲۰۲۳", "۲۰۲۴")])
def test_digits_and_reading_order_do_not_control_the_binding(
    digits: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    observation = OBSERVED[1]
    a, b = years(observation)
    lines = [
        replace(line, text=digits[0])
        if line == a
        else replace(line, text=digits[1])
        if line == b
        else line
        for line in reversed(observation.ocr.lines)
    ]
    result = recover(observation, monkeypatch, lines)
    layout = parse_header(result, StatementType.INCOME, None)
    assert {col: p.key for col, p in layout.value_cols.items()} == {0: "FY2023", 1: "FY2024"}


@pytest.mark.parametrize(
    "caption,months,end",
    [
        ("For the three months ended 30 June", 3, "06-30"),
        ("For the six months ended 30 June", 6, "06-30"),
        ("عن تسعة أشهر المنتهية في ٣٠ سبتمبر", 9, "09-30"),
    ],
)
def test_years_keep_the_printed_interim_date_and_length(
    caption: str, months: int, end: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation = OBSERVED[1]
    lines = [
        replace(line, text=caption) if "عن السنة" in line.text else line
        for line in observation.ocr.lines
    ]
    result = recover(observation, monkeypatch, lines)
    layout = parse_header(result, StatementType.INCOME, None)
    assert {col: p.key for col, p in layout.value_cols.items()} == {
        0: f"{months}M-2023-{end}",
        1: f"{months}M-2024-{end}",
    }
    assert all(p.months == months for p in layout.value_cols.values())


@pytest.mark.parametrize(
    "caption",
    [
        "For the two months ended 30 June",
        "For the period ended 30 June",
        "For the quarter ended",
        "Report for 2024",
        "31 unknown 2024",
    ],
)
def test_unreadable_or_unsupported_caption_cannot_create_an_annual_default(
    caption: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation = OBSERVED[1]
    lines = [
        replace(line, text=caption) if "عن السنة" in line.text else line
        for line in observation.ocr.lines
    ]
    result = recover(observation, monkeypatch, lines)
    assert not parse_header(result, StatementType.INCOME, None).value_cols


@pytest.mark.parametrize(
    "field,value",
    [
        ("left", -0.01),
        ("top", float("nan")),
        ("width", 0),
        ("height", -0.1),
        ("left", 0.99),
        ("confidence", 1.5),
        ("confidence", 0.1),
    ],
)
def test_malformed_or_uncertain_date_boxes_are_rejected(
    field: str, value: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Inject malformed engine output directly so a test renderer cannot discard it first.
    observation = OBSERVED[1]
    inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    recognize = engine.recognize

    def malformed(image: Image.Image, languages: Sequence[str]) -> list[OcrLine]:
        output = recognize(image, languages)
        index = next(i for i, line in enumerate(output) if line.text == "٢٠٢٣")
        line = output[index]
        output[index] = OcrLine(
            text=line.text,
            confidence=value if field == "confidence" else line.confidence,
            left=value if field == "left" else line.left,
            top=value if field == "top" else line.top,
            width=value if field == "width" else line.width,
            height=value if field == "height" else line.height,
        )
        return output

    monkeypatch.setattr(engine, "recognize", malformed)
    result = header_recovery.recover_header(
        observation.grid, observation.document(), Path("unused.pdf"), IngestConfig(), engine
    )
    assert not result.recovered_headers
    assert result.cells == observation.grid.cells


def test_valid_header_skips_rendering_and_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    observation = OBSERVED[1]
    recovered = recover(observation, monkeypatch, observation.ocr.lines)
    requests = inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    assert (
        header_recovery.recover_header(
            recovered, observation.document(), Path("unused.pdf"), IngestConfig(), engine
        )
        is recovered
    )
    assert not requests and not engine.calls


def test_two_overlapping_tables_do_not_share_a_crop(monkeypatch: pytest.MonkeyPatch) -> None:
    observation = OBSERVED[1]
    document = observation.document()
    document = document.model_copy(
        update={
            "tables": [
                *document.tables,
                document.tables[0].model_copy(update={"self_ref": "#/tables/99"}),
            ]
        }
    )
    requests = inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    result = header_recovery.recover_header(
        observation.grid, document, Path("unused.pdf"), IngestConfig(), engine
    )
    assert "header_recovery_ambiguous_table" in result.flags
    assert not requests and not engine.calls


def test_source_boxes_and_page_identity_must_be_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    observation = OBSERVED[1]
    cell = next(c for c in observation.grid.cells if c.row == 3 and c.col == 0)
    grid = observation.grid.model_copy(
        update={
            "cells": tuple(
                c.model_copy(update={"page_no": 5}) if c == cell else c
                for c in observation.grid.cells
            )
        }
    )
    requests = inject_render(monkeypatch, observation)
    result = header_recovery.recover_header(
        grid,
        observation.document(),
        Path("unused.pdf"),
        IngestConfig(),
        RecordedEngine(observation),
    )
    assert "header_recovery_geometry_invalid" in result.flags
    assert not requests


def test_engine_failure_is_visible_and_never_changes_source_cells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fra_ingest.ocr import OcrUnavailableError

    observation = OBSERVED[1]
    inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)

    def unavailable(_image: Image.Image, _languages: Sequence[str]) -> list[OcrLine]:
        raise OcrUnavailableError("configured engine unavailable")

    monkeypatch.setattr(engine, "recognize", unavailable)
    result = header_recovery.recover_header(
        observation.grid, observation.document(), Path("unused.pdf"), IngestConfig(), engine
    )
    assert result.flags == (
        "header_recovery_failed:OcrUnavailableError:configured engine unavailable",
    )
    assert result.cells == observation.grid.cells


def test_nonzero_crop_origin_is_applied_in_points() -> None:
    image = Image.new("RGB", (900, 600))
    crop = header_recovery.RasterCrop(
        image, BBox(left=100, top=200, right=250, bottom=300), 600, 800
    )
    assert crop.line_box(OcrLine("2024", 1, 0.2, 0.3, 0.1, 0.1)) == BBox(
        left=130, top=230, right=145, bottom=240
    )


@pytest.mark.parametrize("date", ["31 December", "٣١ ديسمبر", "31/12/", "2023-12-31"])
def test_complete_printed_dates_are_accepted(date: str, monkeypatch: pytest.MonkeyPatch) -> None:
    observation = OBSERVED[1]
    a, b = years(observation)
    if date == "2023-12-31":
        texts = ("2023-12-31", "2024-12-31")
    elif date.endswith("/"):
        texts = (date + "2023", date + "2024")
    else:
        texts = (date + " 2023", date + " 2024")
    lines = [
        replace(line, text=texts[0])
        if line == a
        else replace(line, text=texts[1])
        if line == b
        else line
        for line in observation.ocr.lines
    ]
    result = recover(observation, monkeypatch, lines)
    assert {
        col: p.key for col, p in parse_header(result, StatementType.INCOME, None).value_cols.items()
    } == {0: "FY2023", 1: "FY2024"}


def test_complete_date_conflicting_with_caption_is_held(monkeypatch: pytest.MonkeyPatch) -> None:
    observation = OBSERVED[1]
    a, b = years(observation)
    lines = [
        replace(line, text="30 June 2023")
        if line == a
        else replace(line, text="30 June 2024")
        if line == b
        else line
        for line in observation.ocr.lines
    ]
    result = recover(observation, monkeypatch, lines)
    assert "header_recovery_conflicting_periods" in result.flags
    assert not result.recovered_headers


def test_mirrored_english_scan_uses_boxes_not_column_index(monkeypatch: pytest.MonkeyPatch) -> None:
    observation = OBSERVED[1]
    width = observation.page.size.width

    def mirror_box(box: BBox) -> BBox:
        return box.model_copy(update={"left": width - box.right, "right": width - box.left})

    table = observation.table.model_copy(
        update={
            "prov": [
                p.model_copy(
                    update={
                        "bbox": p.bbox.model_copy(
                            update={"left": width - p.bbox.right, "right": width - p.bbox.left}
                        )
                    }
                )
                for p in observation.table.prov
            ],
            "data": observation.table.data.model_copy(
                update={
                    "table_cells": [
                        c.model_copy(
                            update={
                                "bbox": c.bbox.model_copy(
                                    update={
                                        "left": width - c.bbox.right,
                                        "right": width - c.bbox.left,
                                    }
                                )
                            }
                        )
                        if c.bbox
                        else c
                        for c in observation.table.data.table_cells
                    ]
                }
            ),
        }
    )
    texts = [
        t.model_copy(
            update={
                "prov": [
                    p.model_copy(
                        update={
                            "bbox": p.bbox.model_copy(
                                update={"left": width - p.bbox.right, "right": width - p.bbox.left}
                            )
                        }
                    )
                    for p in t.prov
                ]
            }
        )
        for t in observation.texts
    ]
    mirrored = observation.model_copy(update={"table": table, "texts": texts})
    lines = [
        replace(
            line,
            left=1 - line.left - line.width,
            text="For the year ended 31 December" if "عن السنة" in line.text else line.text,
        )
        for line in observation.ocr.lines
    ]
    result = recover(mirrored, monkeypatch, lines)
    layout = parse_header(result, StatementType.INCOME, None)
    assert {col: p.key for col, p in layout.value_cols.items()} == {0: "FY2023", 1: "FY2024"}
    assert result.cells == mirrored.grid.cells
    left, right = result.recovered_headers
    assert left.bbox is not None and right.bbox is not None
    assert left.bbox.left > right.bbox.left
    assert mirror_box(left.bbox).left < mirror_box(right.bbox).left


def test_an_independent_table_on_another_page_does_not_supply_years(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = OBSERVED[1]
    other = OBSERVED[0]
    document = observation.document().model_copy(
        update={
            "tables": [observation.table, other.table],
            "texts": [*observation.texts, *other.texts],
            "pages": {"5": other.page, "6": observation.page},
        }
    )
    inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    engine.lines = [line for line in engine.lines if line not in years(observation)]
    result = header_recovery.recover_header(
        observation.grid, document, Path("unused.pdf"), IngestConfig(), engine
    )
    assert not result.recovered_headers
    assert result.cells == observation.grid.cells


@pytest.mark.parametrize("rotation", [0, 90])
def test_pdfium_crop_rounding_uses_the_displayed_page_space(tmp_path: Path, rotation: int) -> None:
    import math

    import pypdfium2 as pdfium

    path = tmp_path / "crop.pdf"
    with pdfium.PdfDocument.new() as pdf:
        page = pdf.new_page(595.4, 842.4)
        page.set_rotation(rotation)
        width, height = page.get_size()
        page.close()
        pdf.save(path)
    box = BBox(left=100.13, top=45.17, right=320.28, bottom=165.29)
    scale = 5.5
    crop = header_recovery.render_crop(path, 1, box, scale)
    try:
        xscale, yscale = math.ceil(width * scale) / width, math.ceil(height * scale) / height
        assert crop.bbox.left == pytest.approx(math.ceil(box.left * scale) / xscale)
        assert crop.bbox.top == pytest.approx(math.ceil(box.top * scale) / yscale)
        assert crop.bbox.right - crop.bbox.left == pytest.approx(crop.image.width / xscale)
        assert crop.bbox.bottom - crop.bbox.top == pytest.approx(crop.image.height / yscale)
        assert crop.line_box(OcrLine("2024", 1, 0, 0, 1, 1)) == crop.bbox
    finally:
        crop.image.close()


def test_original_valid_english_and_arabic_headers_keep_identical_layouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_structure import document

    english_doc = document()
    english = build_grid(english_doc.tables[0], english_doc, "docling/p1-2.json")
    observation = OBSERVED[1]
    recovered = recover(observation, monkeypatch, observation.ocr.lines)
    arabic = observation.grid.model_copy(
        update={
            "cells": tuple(
                c.model_copy(update={"row": 0, "text": "٣١ ديسمبر " + c.text, "flags": ()})
                for c in recovered.recovered_headers
            )
            + observation.grid.cells
        }
    )
    requests = inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    for grid, doc in ((english, english_doc), (arabic, observation.document())):
        before = parse_header(grid, StatementType.BALANCE, None)
        assert before.value_cols
        after = header_recovery.recover_header(
            grid, doc, Path("unused.pdf"), IngestConfig(), engine
        )
        assert after is grid
        assert parse_header(after, StatementType.BALANCE, None) == before
    assert not requests and not engine.calls


def test_no_engine_retains_an_explicit_unavailable_reason() -> None:
    observation = OBSERVED[1]
    result = header_recovery.recover_header(
        observation.grid, observation.document(), Path("unused.pdf"), IngestConfig(), None
    )
    assert result.flags == ("header_recovery_unavailable",)
    assert not result.recovered_headers


def test_two_independent_tables_on_same_page_keep_their_own_header_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = OBSERVED[1]
    own = observation.document()
    prov = observation.table.prov[0]
    other = observation.table.model_copy(
        update={
            "self_ref": "#/tables/98",
            "prov": [
                prov.model_copy(
                    update={
                        "bbox": prov.bbox.model_copy(
                            update={"top": 650.0, "bottom": 750.0, "coord_origin": "TOPLEFT"}
                        )
                    }
                )
            ],
        }
    )
    document = own.model_copy(update={"tables": [*own.tables, other]})
    inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    result = header_recovery.recover_header(
        observation.grid, document, Path("unused.pdf"), IngestConfig(), engine
    )
    assert {
        col: p.key for col, p in parse_header(result, StatementType.INCOME, None).value_cols.items()
    } == {0: "FY2023", 1: "FY2024"}
    assert result.cells == observation.grid.cells


def safety_lines(caption: str, headers: tuple[str, str]) -> list[OcrLine]:
    observation = OBSERVED[1]
    first, second = years(observation)
    return [
        replace(line, text=headers[0])
        if line == first
        else replace(line, text=headers[1])
        if line == second
        else replace(line, text=caption)
        if "عن السنة" in line.text
        else line
        for line in observation.ocr.lines
    ]


@pytest.mark.parametrize(
    "caption",
    [
        "For the year ended 31 April",
        "For the year ended 32 December",
        "For the year ended 31 unknown",
        "For the year ended 31 money 2024",
        "عن السنة المنتهية في ٣١ أبريل",
        "For the year ended 29 February",
        "For the year ended 2024-02-29",
        "For the year ended 31 April.",
        "For the year ended 32 December;",
        "For the year ended 31 unknown.",
        "For the year ended 29 February,",
        "عن السنة المنتهية في ٣١ أبريل.",
        "عن السنة المنتهية في ٣٢ ديسمبر،",
        "عن السنة المنتهية في ٣١ مجهول؛",
        "عن السنة المنتهية في ٢٩ فبراير؛",
        "For the year ended 31 April:",
        "For the year ended 31 unknown…",
        "For the year ended 32 December...",
        "For the year ended 29 February:…;",
        "عن السنة المنتهية في ٣١ أبريل:",
        "عن السنة المنتهية في ٣١ مجهول…",
        "عن السنة المنتهية في ٣٢ ديسمبر؛؛",
        "عن السنة المنتهية في ٢٩ فبراير:…؛",
    ],
)
@pytest.mark.parametrize("headers", [("2023", "2024"), ("31 December 2023", "31 December 2024")])
def test_invalid_annual_caption_dates_never_recover(
    caption: str, headers: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = recover(OBSERVED[1], monkeypatch, safety_lines(caption, headers))
    assert not result.recovered_headers
    assert not parse_header(result, StatementType.INCOME, None).value_cols
    assert any(flag.startswith("header_recovery_") for flag in result.flags)
    assert result.cells == OBSERVED[1].grid.cells


@pytest.mark.parametrize(
    "caption",
    [
        "For the year ended 30 September.",
        "For the year ended 30 September,",
        "For the year ended 30 September;",
        "عن السنة المنتهية في ٣٠ سبتمبر.",
        "عن السنة المنتهية في ٣٠ سبتمبر،",
        "عن السنة المنتهية في ٣٠ سبتمبر؛",
        "For the year ended 30 September:",
        "For the year ended 30 September…",
        "For the year ended 30 September:…;",
        "عن السنة المنتهية في ٣٠ سبتمبر:",
        "عن السنة المنتهية في ٣٠ سبتمبر…",
        "عن السنة المنتهية في ٣٠ سبتمبر:…؛",
    ],
)
@pytest.mark.parametrize("headers", [("2023", "2024"), ("30 September 2023", "30 September 2024")])
def test_punctuated_annual_caption_preserves_comparative_dates_and_source_cells(
    caption: str, headers: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = recover(OBSERVED[1], monkeypatch, safety_lines(caption, headers))
    layout = parse_header(result, StatementType.INCOME, None)
    assert {
        col: (p.key, p.end_date.isoformat(), p.months) for col, p in layout.value_cols.items()
    } == {
        0: ("FY2023", "2023-09-30", 12),
        1: ("FY2024", "2024-09-30", 12),
    }
    assert result.recovered_context
    assert result.cells == OBSERVED[1].grid.cells


@pytest.mark.parametrize(
    "caption,months,end",
    [
        ("For the quarter ended", 3, "09-30"),
        ("For the quarter ended", 3, "12-31"),
        ("For the three months ended", 3, "12-31"),
        ("For the six months ended", 6, "06-30"),
        ("For the nine months ended", 9, "09-30"),
        ("عن ستة أشهر المنتهية", 6, "06-30"),
        ("عن تسعة أشهر المنتهية", 9, "09-30"),
        ("For the twelve months ended", 12, "12-31"),
        ("For the 12 months ended", 12, "12-31"),
        ("For the year ended", 12, "12-31"),
        ("عن السنة المنتهية", 12, "12-31"),
    ],
)
def test_date_free_caption_preserves_duration_and_complete_end_dates(
    caption: str, months: int, end: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = (f"2023-{end}", f"2024-{end}")
    result = recover(OBSERVED[1], monkeypatch, safety_lines(caption, headers))
    layout = parse_header(result, StatementType.INCOME, None)
    assert {col: (p.end_date.isoformat(), p.months) for col, p in layout.value_cols.items()} == {
        0: (headers[0], months),
        1: (headers[1], months),
    }
    assert result.recovered_context
    assert result.cells == OBSERVED[1].grid.cells


@pytest.mark.parametrize(
    "caption",
    [
        "For the two months ended",
        "For the period ended",
        "عن الفترة المنتهية",
        "عن شهرين المنتهية",
        "For the unknown months ended",
    ],
)
def test_date_free_unknown_duration_never_becomes_annual(
    caption: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = recover(OBSERVED[1], monkeypatch, safety_lines(caption, ("2023-12-31", "2024-12-31")))
    assert not result.recovered_headers
    assert "header_recovery_period_uncertain" in result.flags
    assert result.cells == OBSERVED[1].grid.cells


@pytest.mark.parametrize(
    "headers",
    [
        ("31 April 2023", "31 April 2024"),
        ("31 money 2023", "31 money 2024"),
        ("2023-02-29", "2024-02-29"),
    ],
)
def test_invalid_printed_dates_never_recover(
    headers: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = recover(OBSERVED[1], monkeypatch, safety_lines("For the year ended", headers))
    assert not result.recovered_headers
    assert result.cells == OBSERVED[1].grid.cells


@pytest.mark.parametrize("headers", [("2024", "2020"), ("2024-02-29", "2020-02-29")])
def test_valid_leap_dates_bind_only_when_each_selected_year_is_valid(
    headers: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = recover(
        OBSERVED[1], monkeypatch, safety_lines("For the quarter ended 29 February", headers)
    )
    layout = parse_header(result, StatementType.INCOME, None)
    assert {col: (p.end_date.isoformat(), p.months) for col, p in layout.value_cols.items()} == {
        0: ("2024-02-29", 3),
        1: ("2020-02-29", 3),
    }


def test_mixed_complete_and_year_only_headers_require_consistent_caption_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = recover(
        OBSERVED[1],
        monkeypatch,
        safety_lines("For the quarter ended 30 September", ("2023-12-31", "2024")),
    )
    assert not result.recovered_headers
    assert "header_recovery_conflicting_periods" in result.flags


@pytest.mark.parametrize("duration_below_date", [False, True])
def test_caption_duration_on_a_separate_line_survives_date_recognition(
    duration_below_date: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = safety_lines("30 September", ("2023-09-30", "2024-09-30"))
    date = next(line for line in lines if line.text == "30 September")
    lines.append(
        replace(
            date,
            text="For the six months ended",
            top=date.top + (0.06 if duration_below_date else -0.06),
        )
    )
    result = recover(OBSERVED[1], monkeypatch, lines)
    layout = parse_header(result, StatementType.INCOME, None)
    assert {col: (p.end_date.isoformat(), p.months) for col, p in layout.value_cols.items()} == {
        0: ("2023-09-30", 6),
        1: ("2024-09-30", 6),
    }


@pytest.mark.parametrize("interim_below_date", [False, True])
@pytest.mark.parametrize("headers", [("2023", "2024"), ("2023-06-30", "2024-06-30")])
@pytest.mark.parametrize("length", [None, 3, 6, 9])
def test_separate_interim_caption_is_retained_and_requires_a_printed_length(
    interim_below_date: bool,
    headers: tuple[str, str],
    length: int | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines = safety_lines("30 June", headers)
    date_line = next(line for line in lines if line.text == "30 June")
    interim = "Interim" if length is None else f"Interim for the {length} months ended"
    lines.append(
        replace(
            date_line, text=interim, top=date_line.top + (0.06 if interim_below_date else -0.06)
        )
    )
    result = recover(OBSERVED[1], monkeypatch, lines)
    assert {c.text for c in result.recovered_context} == {"30 June", interim}
    assert result.cells == OBSERVED[1].grid.cells
    layout = parse_header(result, StatementType.INCOME, None)
    if length is None:
        assert not result.recovered_headers
        assert not layout.value_cols
        assert "header_recovery_period_uncertain" in result.flags
    else:
        assert {
            col: (p.end_date.isoformat(), p.months) for col, p in layout.value_cols.items()
        } == {
            0: ("2023-06-30", length),
            1: ("2024-06-30", length),
        }
