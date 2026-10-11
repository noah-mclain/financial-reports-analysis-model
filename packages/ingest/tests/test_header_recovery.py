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
from support import located_titles

from fra_core.schemas import BBox, StatementType
from fra_ingest import header_recovery
from fra_ingest.config import IngestConfig
from fra_ingest.docling_json import DlCell, DlDocument, DlPage, DlTable, DlText
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


def _geometry_invalid(grid: Grid) -> tuple[str, ...]:
    """The recovery flag, then one unavailable comparison for each bound column."""
    bound = parse_header(grid, StatementType.BALANCE, None).value_cols
    return (
        "header_recovery_geometry_invalid",
        *(f"period_evidence_unavailable:{col}:geometry_invalid" for col in bound),
    )


def partial_observation(
    bound_col: int = 0,
    header: str = "31 December 2023 (Restated) (Audited)",
    target_header: str = "",
) -> Observation:
    observation = OBSERVED[1]
    lines = years(observation)
    template = observation.table.data.table_cells[0]
    assert template.bbox is not None
    headers = []
    for col, text in ((bound_col, header), (1 - bound_col, target_header)):
        if not text:
            continue
        line = lines[col]
        width, _ = observation.ocr.page_points
        height = observation.ocr.crop_height_points
        headers.append(
            template.model_copy(
                update={
                    "text": text,
                    "start_col_offset_idx": col,
                    "end_col_offset_idx": col + 1,
                    "col_span": 1,
                    "row_span": 1,
                    "bbox": template.bbox.model_copy(
                        update={
                            "left": line.left * width,
                            "right": (line.left + line.width) * width,
                            "top": line.top * height,
                            "bottom": line.bottom * height,
                            "coord_origin": "TOPLEFT",
                        }
                    ),
                }
            )
        )
    data = observation.table.data.model_copy(
        update={
            "table_cells": [
                *headers,
                *(c for c in observation.table.data.table_cells if c.start_row_offset_idx != 0),
            ]
        }
    )
    return observation.model_copy(
        update={"table": observation.table.model_copy(update={"data": data})}
    )


@pytest.mark.parametrize("bound_col", [0, 1])
@pytest.mark.parametrize(
    "header,caption,target",
    [
        (
            "31 December 2023 (Restated) (Audited)",
            "For the year ended 31 December",
            "31 December 2024",
        ),
        ("2023 (Unaudited)", "For the six months ended 30 June", "30 June 2024"),
        (
            "For the three months ended 30 June 2023",
            "For the six months ended 30 June",
            "30 June 2024",
        ),
    ],
)
def test_partial_recovery_preserves_bound_periods_and_source_context(
    bound_col: int, header: str, caption: str, target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation = partial_observation(bound_col, header)
    labels = (header, target) if bound_col == 0 else (target, header)
    before = observation.grid
    result = recover(observation, monkeypatch, safety_lines(caption, labels))
    assert [c.col for c in result.recovered_headers] == [1 - bound_col]
    assert result.cells == before.cells
    assert result.num_rows == before.num_rows
    assert result.recovered_context
    for kind in StatementType:
        for hint in (None, "For the six months ended 30 June 2023"):
            original = parse_header(before, kind, hint)
            layout = parse_header(result, kind, hint)
            assert (
                layout.value_cols[bound_col].model_dump_json()
                == original.value_cols[bound_col].model_dump_json()
            )
            assert layout.unbound_cols == []
    recovered = result.recovered_headers[0]
    assert recovered.bbox is not None and recovered.page_no == before.page_no
    line = years(observation)[1 - bound_col]
    assert recovered.bbox.left == pytest.approx(line.left * observation.ocr.page_points[0])
    assert recovered.flags == ("header_recovered",)


@pytest.mark.parametrize(
    "case,reason",
    [
        ("low_confidence", "uncertain"),
        ("missing", "labels_missing_or_conflicting"),
        ("duplicate", "labels_missing_or_conflicting"),
        ("contradictory", "labels_missing_or_conflicting"),
        ("collision", "conflicting_periods"),
        ("caption", "period_uncertain"),
        ("cross_column", "ambiguous_columns"),
        ("wrong_row", "ambiguous_rows"),
        ("malformed_alternate", "period_uncertain"),
    ],
)
def test_partial_recovery_holds_uncertain_target(
    case: str, reason: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation = partial_observation()
    lines = safety_lines("For the year ended 31 December", ("31 December 2023", "31 December 2024"))
    target = next(line for line in lines if line.text == "31 December 2024")
    if case == "low_confidence":
        lines[lines.index(target)] = replace(target, confidence=0.1)
    elif case == "missing":
        lines.remove(target)
    elif case == "duplicate":
        lines.append(target)
    elif case == "contradictory":
        lines.append(replace(target, text="31 December 2022"))
    elif case == "collision":
        lines[lines.index(target)] = replace(target, text="31 December 2023")
    elif case == "caption":
        lines = safety_lines("For the period ended", ("31 December 2023", "31 December 2024"))
    elif case == "cross_column":
        lines[lines.index(target)] = replace(target, left=0.26, width=0.10)
    elif case == "wrong_row":
        lines[lines.index(target)] = replace(target, top=0.80)
    else:
        lines.append(replace(target, text="31 Flober 2024"))
    result = recover(observation, monkeypatch, lines)
    assert not result.recovered_headers
    assert f"header_recovery_{reason}" in result.flags
    assert result.cells == observation.grid.cells
    assert (
        parse_header(result, StatementType.INCOME, None).value_cols
        == parse_header(observation.grid, StatementType.INCOME, None).value_cols
    )


@pytest.mark.parametrize(
    "original", ["31 April 2024", "31 unknown 2024", "31 December 2023", "2024-13-31"]
)
def test_partial_recovery_never_replaces_original_printed_header(
    original: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation = partial_observation(target_header=original)
    requests = inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    result = header_recovery.recover_header(
        observation.grid, observation.document(), Path("unused.pdf"), IngestConfig(), engine
    )
    assert not result.recovered_headers
    assert result.cells == observation.grid.cells
    assert not requests and not engine.calls


def test_partial_recovery_engine_failure_preserves_bound_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = partial_observation()
    inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)

    def failed(_image: Image.Image, _languages: Sequence[str]) -> list[OcrLine]:
        raise RuntimeError("partial read failed")

    monkeypatch.setattr(engine, "recognize", failed)
    result = header_recovery.recover_header(
        observation.grid, observation.document(), Path("unused.pdf"), IngestConfig(), engine
    )
    assert result.flags == (
        "header_recovery_failed:RuntimeError:partial read failed",
        "period_evidence_unavailable:0:failed:RuntimeError:partial read failed",
    )
    assert result.cells == observation.grid.cells
    assert (
        parse_header(result, StatementType.BALANCE, None).value_cols
        == parse_header(observation.grid, StatementType.BALANCE, None).value_cols
    )


@pytest.mark.parametrize("bound_read", [None, "31 December 2022", "31 April 2023"])
def test_only_the_missing_slot_requires_an_alternate_read(
    bound_read: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation = partial_observation()
    lines = safety_lines("For the year ended 31 December", ("31 December 2023", "31 December 2024"))
    bound = next(line for line in lines if line.text == "31 December 2023")
    lines.remove(bound)
    if bound_read:
        lines.append(replace(bound, text=bound_read, confidence=0.1))
    result = recover(observation, monkeypatch, lines)
    assert [c.col for c in result.recovered_headers] == [1]
    assert (
        parse_header(result, StatementType.BALANCE, None).value_cols[0]
        == parse_header(observation.grid, StatementType.BALANCE, None).value_cols[0]
    )


@pytest.mark.parametrize("held_col", [0, 1])
def test_real_partial_recovery_cannot_bypass_consumer_period_holds(
    held_col: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_structure import inputs

    from fra_core.schemas import PageMode
    from fra_core.schemas.statement import PeriodEvidence
    from fra_ingest.structure import structure_document

    observation = partial_observation(header="31 December 2023")
    grid = observation.grid
    anchor = grid.cell(0 if held_col == 0 else 1, held_col)
    assert anchor is not None and anchor.bbox is not None
    group = PeriodEvidence.model_validate(
        {
            "observations": [
                {
                    "source_id": f"read-{i}",
                    "raw_text": text,
                    "docling_path": grid.docling_path,
                    "confidence": 1.0,
                    "provenance": {
                        "page_no": grid.page_no,
                        "table_ref": grid.table_ref,
                        "row": anchor.row,
                        "col": held_col,
                        "source": "ocr",
                        "bbox": anchor.bbox.model_dump(),
                    },
                }
                for i, text in enumerate(
                    (
                        "31 December 2023" if held_col == 0 else "31 December 2024",
                        "31 December 2022",
                    )
                )
            ]
        }
    )
    base = inputs([(grid.docling_path, observation.document())]).model_copy(
        update={
            "page_modes": {grid.page_no: PageMode.IMAGE},
            "located": located_titles({grid.page_no: (StatementType.INCOME,)}, {}),
            "period_evidence": (group,),
        }
    )
    inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    engine.lines = safety_lines(
        "For the year ended 31 December", ("31 December 2023", "31 December 2024")
    )
    recovered_grids = []

    def recovery(g: Grid, d: DlDocument) -> Grid:
        result = header_recovery.recover_header(g, d, Path("unused.pdf"), IngestConfig(), engine)
        recovered_grids.append(result)
        return result

    result, _ = structure_document(base, IngestConfig(), header_recover=recovery)
    assert [c.col for c in recovered_grids[0].recovered_headers] == [1]
    statement = result.statements[0]
    assert [p.key for p in statement.periods] == ["FY2024" if held_col == 0 else "FY2023"]
    candidates = tuple(c.observation for c in statement.period_conflicts[0].candidates)
    merged = next(e for e in result.tables[0].period_evidence if e.col == held_col)
    # Every reading of the held column is a candidate: the crop's own, then the caller's.
    assert candidates == merged.observations
    assert candidates[-2:] == group.observations
    assert f"period_conflict:{held_col}" in result.reviews[0].reasons


def test_partial_recovery_cannot_move_a_year_only_bound_date_with_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = partial_observation(bound_col=1, header="2023")
    result = recover(
        observation,
        monkeypatch,
        safety_lines("For the six months ended 30 June", ("30 June 2023", "2023")),
    )
    assert not result.recovered_headers
    assert "header_recovery_conflicting_periods" in result.flags
    for hint in (None, "For the six months ended 30 June 2023"):
        assert (
            parse_header(result, StatementType.INCOME, hint).value_cols
            == parse_header(observation.grid, StatementType.INCOME, hint).value_cols
        )


@pytest.mark.parametrize("target_top", [None, 0.80], ids=["same_row", "disjoint_row"])
def test_partial_recovery_requires_original_bound_row_geometry(
    target_top: float | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation = partial_observation()
    before = observation.grid
    anchor = before.cell(0, 0)
    assert anchor is not None
    before = before.model_copy(
        update={
            "cells": tuple(
                c.model_copy(update={"bbox": None}) if c == anchor else c for c in before.cells
            )
        }
    )
    lines = safety_lines("For the year ended 31 December", ("31 December 2023", "31 December 2024"))
    if target_top is not None:
        lines = [
            replace(line, top=target_top) if line.text == "31 December 2024" else line
            for line in lines
        ]
    requests = inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    engine.lines = lines
    result = header_recovery.recover_header(
        before, observation.document(), Path("unused.pdf"), IngestConfig(), engine
    )
    assert not result.recovered_headers
    assert result.flags == _geometry_invalid(before)
    assert result.cells == before.cells
    assert not result.recovered_context
    for kind in StatementType:
        original = parse_header(before, kind, None)
        layout = parse_header(result, kind, None)
        assert layout.value_cols == original.value_cols
        assert layout.unbound_cols == [1]
    assert not requests and not engine.calls


def test_partial_recovery_requires_bound_column_amount_geometry_from_source_cells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = OBSERVED[1]
    template = observation.table.data.table_cells[0]
    source_box = template.bbox
    assert source_box is not None

    def cell(text: str, row: int, col: int, span: int = 1) -> DlCell:
        return template.model_copy(
            update={
                "text": text,
                "start_row_offset_idx": row,
                "start_col_offset_idx": col,
                "row_span": 1,
                "col_span": span,
                "column_header": row == 0,
                "bbox": source_box.model_copy(
                    update={
                        "left": 20.0 + 130.0 * col,
                        "right": 20.0 + 130.0 * (col + span) - 10.0,
                        "top": 120.0 + 30.0 * row,
                        "bottom": 135.0 + 30.0 * row,
                        "coord_origin": "TOPLEFT",
                    }
                ),
            }
        )

    data = observation.table.data.model_copy(
        update={
            "num_rows": 4,
            "num_cols": 4,
            "table_cells": [
                cell("Assets", 0, 0),
                cell("Notes", 0, 1),
                cell("31 December 2023 (Restated) (Audited)", 0, 2),
                cell("Cash", 1, 0),
                cell("1", 1, 1),
                cell("125,000", 1, 3),
                cell("100,000", 2, 0, span=3),
                cell("150,000", 2, 3),
                cell("200,000", 3, 0, span=3),
                cell("250,000", 3, 3),
            ],
        }
    )
    observation = observation.model_copy(
        update={"table": observation.table.model_copy(update={"data": data})}
    )
    before = build_grid(observation.table, observation.document(), observation.docling_path)
    original = parse_header(before, StatementType.BALANCE, None)
    assert original.label_col == 0 and original.note_col == 1
    assert list(original.value_cols) == [2] and original.unbound_cols == [3]
    assert before.text(2, 2) == "100,000"
    assert not any(c.col == 2 and c.row not in original.header_rows for c in before.cells)
    requests = inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    result = header_recovery.recover_header(
        before, observation.document(), Path("unused.pdf"), IngestConfig(), engine
    )
    assert result.flags == _geometry_invalid(before)
    assert result.cells == before.cells
    assert not result.recovered_headers and not result.recovered_context
    assert not requests and not engine.calls
    for kind in StatementType:
        for hint in (None, "For the six months ended 30 June 2023"):
            layout = parse_header(result, kind, hint)
            assert layout.value_cols == parse_header(before, kind, hint).value_cols
            assert layout.unbound_cols == [3]


@pytest.mark.parametrize(
    "case", ["row_span", "col_span", "outside_header", "outside_column", "zero_area", "nonfinite"]
)
def test_partial_recovery_rejects_untrustworthy_bound_anchor(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    bound_col = 1 if case == "col_span" else 0
    target_col = 1 - bound_col
    observation = partial_observation(bound_col)
    before = observation.grid
    anchor = before.cell(0, bound_col)
    assert anchor is not None and anchor.bbox is not None
    if case == "row_span":
        changed = anchor.model_copy(update={"row_span": 2})
    elif case == "col_span":
        changed = anchor.model_copy(update={"col_span": 2})
    else:
        box = anchor.bbox
        if case == "outside_header":
            box = box.model_copy(update={"top": 200.0, "bottom": 210.0})
        elif case == "outside_column":
            box = box.model_copy(update={"left": 10.0, "right": 20.0})
        elif case == "zero_area":
            box = box.model_copy(update={"bottom": box.top})
        else:
            box = box.model_copy(update={"top": float("nan")})
        changed = anchor.model_copy(update={"bbox": box})
    before = before.model_copy(
        update={"cells": tuple(changed if c == anchor else c for c in before.cells)}
    )
    original = parse_header(before, StatementType.BALANCE, None)
    assert list(original.value_cols) == [bound_col] and original.unbound_cols == [target_col]
    inject_render(monkeypatch, observation)
    engine = RecordedEngine(observation)
    engine.lines = safety_lines(
        "For the year ended 31 December",
        ("31 December 2023", "31 December 2024")
        if bound_col == 0
        else ("31 December 2024", "31 December 2023"),
    )
    result = header_recovery.recover_header(
        before, observation.document(), Path("unused.pdf"), IngestConfig(), engine
    )
    assert not result.recovered_headers
    assert result.flags == _geometry_invalid(before)
    assert result.cells == before.cells
    layout = parse_header(result, StatementType.BALANCE, None)
    assert layout.value_cols == original.value_cols and layout.unbound_cols == [target_col]


@pytest.mark.parametrize("partially_bound", [True, False], ids=["partial", "all_unbound"])
@pytest.mark.parametrize(
    "original",
    [
        "(Restated) (Unaudited)",
        "[Restated]",
        "(Audited)",
        "(Unaudited)",
        "EGP (Re-stated)",
        "معدلة",
        "EGP معدّلة",
        "EGP معاد",
        "غير مدققة",
        "EGP غير مراجعة",
    ],
)
def test_recovery_does_not_discard_original_status_facts(
    original: str, partially_bound: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    observation = partial_observation(
        header="31 December 2023 (Restated) (Audited)" if partially_bound else "",
        target_header=original,
    )
    before = observation.grid
    result = recover(
        observation,
        monkeypatch,
        safety_lines("For the year ended 31 December", ("31 December 2023", "31 December 2024")),
    )
    assert not result.recovered_headers
    assert result.flags == ("header_recovery_status_unpreserved",)
    assert result.cells == before.cells
    assert not result.recovered_context
    for kind in StatementType:
        original_layout = parse_header(before, kind, None)
        layout = parse_header(result, kind, None)
        assert layout.value_cols == original_layout.value_cols
        assert layout.unbound_cols == original_layout.unbound_cols
        assert layout.header_texts == original_layout.header_texts
        assert original in layout.header_texts[1]


@pytest.mark.parametrize("original", ["EGP", "آلاف", "EGP آلاف"])
def test_partial_recovery_retains_plain_currency_and_scale_subheaders(
    original: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fra_ingest.metadata import detect_metadata

    observation = partial_observation(target_header=original)
    before = observation.grid
    result = recover(
        observation,
        monkeypatch,
        safety_lines("For the year ended 31 December", ("31 December 2023", "31 December 2024")),
    )
    assert [c.col for c in result.recovered_headers] == [1]
    assert result.cells == before.cells
    for kind in StatementType:
        layout = parse_header(result, kind, None)
        assert not layout.unbound_cols
        assert layout.value_cols[0] == parse_header(before, kind, None).value_cols[0]
        assert original in layout.header_texts[1]
        before_meta = detect_metadata(
            header_text=" ".join(parse_header(before, kind, None).header_texts.values()),
            context_texts=[],
            document_texts=[],
        )
        after_meta = detect_metadata(
            header_text=" ".join(layout.header_texts.values()), context_texts=[], document_texts=[]
        )
        assert after_meta == before_meta


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


@pytest.mark.parametrize("bound_col", [0, 1])
@pytest.mark.parametrize("interim_below_date", [False, True])
@pytest.mark.parametrize(
    "context,length",
    [
        ("Interim", None),
        ("For the period ended", None),
        ("Interim for the 3 months ended", 3),
        ("Interim for the 6 months ended", 6),
        ("Interim for the 9 months ended", 9),
    ],
)
def test_partial_recovery_with_separate_interim_context_preserves_original_binding(
    bound_col: int,
    interim_below_date: bool,
    context: str,
    length: int | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    header = "For the three months ended 30 June 2023 (Restated) (Unaudited)"
    observation = partial_observation(bound_col, header)
    target_col = 1 - bound_col
    headers = (header, "2024") if bound_col == 0 else ("2024", header)
    lines = safety_lines("30 June", headers)
    date_line = next(line for line in lines if line.text == "30 June")
    lines.append(
        replace(
            date_line, text=context, top=date_line.top + (0.06 if interim_below_date else -0.06)
        )
    )
    before = observation.grid
    result = recover(observation, monkeypatch, lines)
    assert result.cells == before.cells
    assert {c.text for c in result.recovered_context} == {"30 June", context}
    for kind in StatementType:
        for hint in (None, "For the six months ended 30 June 2023"):
            original = parse_header(before, kind, hint)
            layout = parse_header(result, kind, hint)
            assert layout.value_cols[bound_col] == original.value_cols[bound_col]
            assert layout.value_cols[bound_col].restated
            assert layout.value_cols[bound_col].audited is False
            if length is None:
                assert not result.recovered_headers
                assert target_col in layout.unbound_cols
                assert "header_recovery_period_uncertain" in result.flags
            else:
                assert [c.col for c in result.recovered_headers] == [target_col]
                assert layout.value_cols[target_col].end_date.isoformat() == "2024-06-30"
                if kind != StatementType.BALANCE:
                    assert layout.value_cols[target_col].months == length
