"""OCR engine interface. Vision itself is exercised only in the slow tests."""

from collections.abc import Callable
from pathlib import Path

import pypdfium2 as pdfium
import pytest
from PIL import Image
from support import FakeOcr

from fra_ingest.ocr import (
    OcrLine,
    OcrUnavailableError,
    VisionOcr,
    line_from_vision,
    read_with_fallback,
    sort_lines,
)


def test_vision_boxes_become_top_left_page_fractions() -> None:
    # Vision reports (x, y, width, height) with the origin at the bottom left.
    line = line_from_vision("Total assets", 0.9, (0.1, 0.7, 0.3, 0.05))
    assert line.top == pytest.approx(0.25)
    assert line.bottom == pytest.approx(0.30)
    assert line.left == pytest.approx(0.1)
    assert line.confidence == pytest.approx(0.9)


def test_lines_read_top_to_bottom_then_left_to_right() -> None:
    lines = [
        OcrLine("b", 1.0, left=0.5, top=0.201, width=0.1, height=0.02),
        OcrLine("c", 1.0, left=0.1, top=0.6, width=0.1, height=0.02),
        OcrLine("a", 1.0, left=0.1, top=0.2, width=0.1, height=0.02),
    ]
    assert [line.text for line in sort_lines(lines)] == ["a", "b", "c"]


ARABIC = [OcrLine("قائمة المركز المالي المجمعة", 0.9, left=0.3, top=0.05, width=0.4, height=0.03)]
LATIN_GARBLE = [
    OcrLine("Statement of flnancial posltion", 0.66, left=0.2, top=0.05, width=0.6, height=0.03)
]
ENGLISH = [
    OcrLine("Statement of financial position", 0.98, left=0.2, top=0.05, width=0.6, height=0.03)
]
BLANK = Image.new("RGB", (10, 10))


def test_an_arabic_page_is_read_once_in_arabic() -> None:
    engine = FakeOcr(by_language={"ar-SA": ARABIC, "en-US": LATIN_GARBLE})
    lines, language = read_with_fallback(engine, BLANK, ("ar-SA", "en-US"))
    assert (lines, language) == (ARABIC, "ar-SA")
    assert engine.languages == [("ar-SA",)]


def test_an_arabic_read_without_arabic_letters_is_retried_in_english() -> None:
    # Measured: an Arabic-first read of an English page returns Latin text and no Arabic.
    engine = FakeOcr(by_language={"ar-SA": LATIN_GARBLE, "en-US": ENGLISH})
    lines, language = read_with_fallback(engine, BLANK, ("ar-SA", "en-US"))
    assert (lines, language) == (ENGLISH, "en-US")
    assert engine.languages == [("ar-SA",), ("en-US",)]


def test_the_last_language_is_kept_even_when_it_finds_little() -> None:
    engine = FakeOcr()
    lines, language = read_with_fallback(engine, BLANK, ("ar-SA", "en-US"))
    assert (lines, language) == ([], "en-US")


@pytest.mark.slow
@pytest.mark.golden
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("juhayna-2025-en-consolidated.pdf", "KPMG"),
        ("juhayna-2025-ar-consolidated.pdf", "الرأي"),
    ],
)
def test_vision_reads_a_scanned_page(
    golden: Callable[[str], Path], name: str, expected: str
) -> None:
    try:
        engine = VisionOcr()
    except OcrUnavailableError:
        pytest.skip("Apple Vision OCR needs macOS with ocrmac installed")
    pdf = pdfium.PdfDocument(golden(name))
    image = pdf[3].render(scale=1.0).to_pil()
    lines = engine.recognize(image, ("ar-SA", "en-US"))
    assert any(expected in line.text for line in lines)
