"""Per-page text: text layer where there is one, OCR where there is not, cached per document."""

from pathlib import Path

import pypdfium2 as pdfium
import pytest
from support import FakeOcr, make_blank_pdf

from fra_core.schemas import PageMode, TextSource
from fra_ingest.config import IngestConfig
from fra_ingest.errors import IngestError
from fra_ingest.ocr import OcrLine
from fra_ingest.pages import document_language, profiles, read_pages, text_layer_is_garbled
from fra_ingest.text_match import is_visual_arabic

TITLE = OcrLine("Statement of financial position", 0.98, left=0.2, top=0.05, width=0.6, height=0.03)
ROW = OcrLine("Total assets 1,234 1,100", 0.97, left=0.1, top=0.7, width=0.8, height=0.02)
ARABIC_TITLE = OcrLine("قائمة المركز المالي", 0.9, left=0.3, top=0.05, width=0.4, height=0.03)


def test_image_pages_are_read_by_ocr_and_split_at_the_header(tmp_path: Path) -> None:
    ocr = FakeOcr(by_language={"en-US": [TITLE, ROW]})
    pages = read_pages(make_blank_pdf(tmp_path / "scan.pdf", pages=2), IngestConfig(), ocr)

    # Arabic first finds no Arabic letters, so each page is read again in English.
    assert ocr.languages == [("ar-SA",), ("en-US",)] * 2
    assert [p.page_no for p in pages] == [1, 2]
    assert pages[0].mode is PageMode.IMAGE
    assert pages[0].source is TextSource.OCR
    assert pages[0].ocr_language == "en-US"
    assert pages[0].header_text == "Statement of financial position"
    assert pages[0].body_text == "Total assets 1,234 1,100"
    assert pages[0].latin_chars > 0


def test_an_arabic_scan_is_read_once(tmp_path: Path) -> None:
    ocr = FakeOcr(by_language={"ar-SA": [ARABIC_TITLE] * 3})
    pages = read_pages(make_blank_pdf(tmp_path / "scan.pdf"), IngestConfig(), ocr)
    assert ocr.calls == 1
    assert pages[0].ocr_language == "ar-SA"


def test_without_an_engine_image_pages_are_flagged_not_blank(tmp_path: Path) -> None:
    pages = read_pages(make_blank_pdf(tmp_path / "scan.pdf"), IngestConfig(), None)
    assert pages[0].mode is PageMode.IMAGE
    assert pages[0].source is None
    assert pages[0].flags == ["ocr_unavailable"]


def test_an_ocr_failure_is_flagged_and_the_run_continues(tmp_path: Path) -> None:
    pages = read_pages(make_blank_pdf(tmp_path / "scan.pdf", 2), IngestConfig(), FakeOcr(fail=True))
    assert [p.flags for p in pages] == [["ocr_failed"], ["ocr_failed"]]


def test_the_cache_is_reused(tmp_path: Path) -> None:
    pdf = make_blank_pdf(tmp_path / "scan.pdf")
    read_pages(pdf, IngestConfig(), FakeOcr([TITLE]), cache_dir=tmp_path / "cache")
    again = FakeOcr(fail=True)
    pages = read_pages(pdf, IngestConfig(), again, cache_dir=tmp_path / "cache")
    assert again.calls == 0
    assert pages[0].header_text == TITLE.text


def test_a_cache_without_ocr_is_not_reused_once_an_engine_exists(tmp_path: Path) -> None:
    pdf = make_blank_pdf(tmp_path / "scan.pdf")
    read_pages(pdf, IngestConfig(), None, cache_dir=tmp_path / "cache")
    ocr = FakeOcr([TITLE])
    pages = read_pages(pdf, IngestConfig(), ocr, cache_dir=tmp_path / "cache")
    assert ocr.calls > 0
    assert pages[0].source is TextSource.OCR


def test_a_cache_with_ocr_serves_a_run_without_an_engine(tmp_path: Path) -> None:
    pdf = make_blank_pdf(tmp_path / "scan.pdf")
    read_pages(pdf, IngestConfig(), FakeOcr([TITLE]), cache_dir=tmp_path / "cache")
    pages = read_pages(pdf, IngestConfig(), None, cache_dir=tmp_path / "cache")
    assert pages[0].source is TextSource.OCR
    assert pages[0].flags == []


def test_changing_the_ocr_resolution_invalidates_the_cache(tmp_path: Path) -> None:
    pdf = make_blank_pdf(tmp_path / "scan.pdf")
    read_pages(pdf, IngestConfig(), FakeOcr([TITLE]), cache_dir=tmp_path / "cache")
    ocr = FakeOcr([TITLE])
    read_pages(pdf, IngestConfig(ocr_dpi=150), ocr, cache_dir=tmp_path / "cache")
    assert ocr.calls > 0


def test_a_truncated_file_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"%PDF-1.4 truncated")
    with pytest.raises(IngestError) as caught:
        read_pages(path, IngestConfig(), None)
    assert caught.value.reason == "unreadable_pdf"


def test_a_password_protected_file_is_reported_as_encrypted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise pdfium.PdfiumError(
            "Failed to load document (PDFium: Incorrect password error).", err_code=4
        )

    monkeypatch.setattr(pdfium, "PdfDocument", refuse)
    with pytest.raises(IngestError) as caught:
        read_pages(tmp_path / "locked.pdf", IngestConfig(), None)
    assert caught.value.reason == "encrypted_pdf"


def test_a_pdf_without_pages_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty.pdf"
    pdf = pdfium.PdfDocument.new()
    pdf.save(path)
    pdf.close()
    with pytest.raises(IngestError) as caught:
        read_pages(path, IngestConfig(), None)
    # pdfium 5.13 refuses to open a zero-page file, so this arrives as unreadable_pdf.
    assert caught.value.reason in {"unreadable_pdf", "empty_pdf"}


def test_profiles_and_language(tmp_path: Path) -> None:
    ocr = FakeOcr(by_language={"ar-SA": [ARABIC_TITLE] * 3})
    pages = read_pages(make_blank_pdf(tmp_path / "scan.pdf"), IngestConfig(), ocr)
    assert profiles(pages)[0].mode is PageMode.IMAGE
    assert profiles(pages)[0].width_pt == pytest.approx(595)
    assert document_language(pages) == "ar"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ةدحوملا يلاملا زكرملا ةمئاق\nتادوجوملا ةلوادتملا ريغ تادوجوملا", True),
        ("قائمة المركز المالي الموحدة\nالموجودات غير المتداولة الموجودات", False),
        ("Statement of financial position", False),
        ("", False),
    ],
)
def test_visual_order_arabic_is_detected(text: str, expected: bool) -> None:
    assert is_visual_arabic(text) is expected


@pytest.mark.parametrize(
    ("text", "garbled"),
    [
        ("قائمة المركز المالي الموحدة الأصول غير المتداولة الممتلكات والمعدات " * 3, False),
        ("Statement of financial position Property, plant and equipment " * 3, False),
        # Al Kathiri: a font whose glyphs map to Greek and odd Arabic forms.
        ("ΔمΗΗ قلΗγلم΍ΕΎΑΎγΣل΍ϊΟ΍έمέيέقΗ ΓΩΣلمو΍ΔليΎلم΍م΍΋لقو΍ΔعΟ΍έمعنϊΟ΍έلم " * 3, True),
        # Naba: digits drawn from modifier letters.
        ("الممتلكات والمعدات ˿ ˽́˹٬˺˻̀٬̀̂ ̂٬˺́˹٬̂̀ ˼˺ ˿˿٬˽˻̂٬́˹˽ ٬˻˾˻٬̀˼ ˼٬̀̀˺٬˿˺˹ ٬́˻̀٬́̂˾ " * 3, True),
        ("short", False),
    ],
)
def test_a_garbled_text_layer_is_recognised(text: str, garbled: bool) -> None:
    assert text_layer_is_garbled(text) is garbled


# Review fixes, 2026-09-28.


def test_a_failed_ocr_read_is_not_cached(tmp_path: Path) -> None:
    pdf = make_blank_pdf(tmp_path / "scan.pdf")
    read_pages(pdf, IngestConfig(), FakeOcr(fail=True), cache_dir=tmp_path / "cache")
    working = FakeOcr(by_language={"en-US": [TITLE]})
    pages = read_pages(pdf, IngestConfig(), working, cache_dir=tmp_path / "cache")
    assert working.calls > 0
    assert pages[0].header_text == TITLE.text


@pytest.mark.parametrize("payload", ['{"version": "3", "settings": {}}', '{"version": "3"', "[]"])
def test_a_corrupt_cache_is_a_miss(tmp_path: Path, payload: str) -> None:
    pdf = make_blank_pdf(tmp_path / "scan.pdf")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "pages.v3.json").write_text(payload, encoding="utf-8")
    pages = read_pages(pdf, IngestConfig(), FakeOcr([TITLE]), cache_dir=cache)
    assert pages[0].source is TextSource.OCR


def test_ocr_time_is_only_counted_when_ocr_ran(tmp_path: Path) -> None:
    pdf = make_blank_pdf(tmp_path / "scan.pdf")
    read_pages(pdf, IngestConfig(), FakeOcr([TITLE]), cache_dir=tmp_path / "cache")
    cached = read_pages(pdf, IngestConfig(), FakeOcr([TITLE]), cache_dir=tmp_path / "cache")
    assert cached[0].ocr_seconds == 0.0


class BrokenEngine:
    """An engine with a programming error, not a recognition failure."""

    name = "broken"

    def recognize(self, image: object, languages: object) -> list[OcrLine]:
        msg = "unexpected argument"
        raise TypeError(msg)


def test_a_programming_error_in_an_engine_is_not_hidden_as_an_ocr_failure(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        read_pages(make_blank_pdf(tmp_path / "scan.pdf"), IngestConfig(), BrokenEngine())
