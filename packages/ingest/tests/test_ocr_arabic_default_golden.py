"""Why ``tesseract.arabic_language`` is ``ara+eng`` (14, "Why Tesseract with ara read no English").

The Arabic read of an English scan must fall below the script check, so the fallback moves on to
English. With ``ara`` Tesseract invents Arabic letters from Latin print and the read is kept.
"""

import shutil
from collections.abc import Callable
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from fra_ingest.config import IngestConfig
from fra_ingest.ocr import MIN_SCRIPT_CHARS, TesseractOcr, script_chars

pytestmark = [pytest.mark.slow, pytest.mark.golden]

# A scanned page of the dev document edita-2025-en-consolidated-ifrs (page 9, counting from 1),
# the clearest of the three statement pages the table in 14 counts.
DOCUMENT = "edita-2025-en-consolidated-ifrs.pdf"
PAGE_INDEX = 8


def arabic_letters(config: IngestConfig, golden: Callable[[str], Path]) -> int:
    pdf = pdfium.PdfDocument(str(golden(DOCUMENT)))
    image = pdf[PAGE_INDEX].render(scale=config.ocr_dpi / 72).to_pil()
    lines = TesseractOcr(config).recognize(image, ["ar-SA"])
    return script_chars(lines, "ar-SA")


def test_only_ara_plus_eng_lets_an_english_scan_fall_through_to_english(
    golden: Callable[[str], Path],
) -> None:
    if shutil.which("tesseract") is None:
        pytest.skip("tesseract is not installed")
    base = IngestConfig(convert_ocr="tesseract")
    with_ara = arabic_letters(base.model_copy(update={"tesseract_arabic_language": "ara"}), golden)
    with_both = arabic_letters(
        base.model_copy(update={"tesseract_arabic_language": "ara+eng"}), golden
    )
    assert with_ara >= MIN_SCRIPT_CHARS
    assert with_both < MIN_SCRIPT_CHARS
