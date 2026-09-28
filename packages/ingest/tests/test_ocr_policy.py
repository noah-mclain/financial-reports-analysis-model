"""OCR mode and language per range (spec 10, Data flow step 3)."""

from __future__ import annotations

from support import located

from fra_core.schemas import PageMode
from fra_ingest.config import IngestConfig
from fra_ingest.ocr_policy import plan_ranges

TXT, IMG = PageMode.TEXT, PageMode.IMAGE
MAC = IngestConfig()
NO_OCR = IngestConfig(convert_ocr="none")


def test_text_ranges_are_pdf_aware_in_the_document_language() -> None:
    plans = plan_ranges(located([TXT] * 6, [(2, 3), (5, 6)]), {}, MAC)
    assert [(p.first_page, p.last_page, p.ocr, p.ocr_language) for p in plans] == [
        (2, 3, "pdf_aware", "en-US"),
        (5, 6, "pdf_aware", "en-US"),
    ]
    arabic = plan_ranges(located([TXT] * 3, [(1, 3)], language="ar"), {}, MAC)
    assert arabic[0].ocr_language == "ar-SA"


def test_one_image_page_makes_the_whole_range_full_page() -> None:
    (plan,) = plan_ranges(located([TXT, TXT, IMG, TXT], [(2, 4)]), {3: "en-US"}, MAC)
    assert plan.ocr == "full_page"
    assert plan.image_pages == (3,)


def test_the_language_is_the_one_most_pages_were_read_in() -> None:
    (plan,) = plan_ranges(
        located([IMG] * 4, [(1, 4)], language="ar"),
        {1: "ar-SA", 2: "en-US", 3: "en-US", 4: None},
        MAC,
    )
    assert plan.ocr_language == "en-US"


def test_a_tie_goes_to_the_earliest_page() -> None:
    (plan,) = plan_ranges(located([IMG, IMG], [(1, 2)]), {1: "ar-SA", 2: "en-US"}, MAC)
    assert plan.ocr_language == "ar-SA"


def test_image_pages_with_no_reads_fall_back_to_the_document_language() -> None:
    (plan,) = plan_ranges(located([IMG, IMG], [(1, 2)], language="ar"), {1: None}, MAC)
    assert plan.ocr_language == "ar-SA"


def test_without_an_ocr_engine_image_ranges_are_skipped() -> None:
    plans = plan_ranges(located([TXT, IMG, TXT, TXT], [(1, 2), (3, 4)]), {2: "en-US"}, NO_OCR)
    assert [(p.ocr, p.ocr_language) for p in plans] == [("skipped", None), ("pdf_aware", None)]


def test_no_convert_ranges_means_no_plans() -> None:
    assert plan_ranges(located([TXT] * 3, []), {}, MAC) == []
