"""read_pages on real golden documents."""

from collections.abc import Callable
from pathlib import Path

import pytest
from support import FakeOcr

from fra_core.schemas import PageMode
from fra_ingest.config import IngestConfig
from fra_ingest.pages import read_pages

pytestmark = pytest.mark.golden

BIDI_CONTROLS = set("‎‏‪‫‬‭‮⁦⁧⁨⁩")


def test_text_layer_is_decided_page_by_page(golden: Callable[[str], Path]) -> None:
    ocr = FakeOcr()
    pages = read_pages(golden("juhayna-2025-en-standalone.pdf"), IngestConfig(), ocr)
    image_pages = [p.page_no for p in pages if p.mode is PageMode.IMAGE]
    assert image_pages == [3, 4, 5]
    assert ocr.calls == 2 * 3  # the empty fake read finds no Arabic, so English follows


def test_arabic_text_layer_has_no_bidi_controls_and_is_marked_visual(
    golden: Callable[[str], Path],
) -> None:
    pages = read_pages(golden("almarai-2025-ar-annualreport.pdf"), IngestConfig(), FakeOcr())
    balance = pages[155]
    assert not BIDI_CONTROLS & set(balance.text)
    assert balance.visual_arabic
    assert "ةمئاق" in balance.header_text


def test_english_statement_header_holds_the_title(golden: Callable[[str], Path]) -> None:
    pages = read_pages(golden("almarai-2025-en-annualreport.pdf"), IngestConfig(), FakeOcr())
    assert "Consolidated Statement of Financial Position" in pages[155].header_text
    assert "Consolidated Statement of Profit or Loss" in pages[158].header_text
    assert not pages[155].visual_arabic


def test_scanned_arabic_is_never_marked_visual(golden: Callable[[str], Path]) -> None:
    pages = read_pages(golden("juhayna-2025-ar-standalone.pdf"), IngestConfig(), FakeOcr())
    assert not any(page.visual_arabic for page in pages)  # OCR gives logical order
