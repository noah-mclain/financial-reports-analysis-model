"""Which OCR mode and which one language docling uses for each range (spec 10, Data flow).

Our own types, not docling's, so this is tested without loading docling.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from fra_core.schemas import PageMode
from fra_ingest.config import IngestConfig
from fra_ingest.results import LocateResult, RangeOcr, RangePlan

_DOCUMENT_LANGUAGE = {"ar": "ar-SA"}
_DEFAULT_LANGUAGE = "en-US"


def plan_ranges(
    located: LocateResult,
    ocr_languages: Mapping[int, str | None],
    config: IngestConfig,
) -> list[RangePlan]:
    modes = {page.page_no: page.mode for page in located.document.pages}
    fallback = _DOCUMENT_LANGUAGE.get(located.document.language, _DEFAULT_LANGUAGE)
    plans = []
    for first, last in located.convert_ranges:
        image_pages = tuple(n for n in range(first, last + 1) if modes.get(n) is PageMode.IMAGE)
        ocr: RangeOcr
        language: str | None
        if config.convert_ocr == "none":
            ocr = "skipped" if image_pages else "pdf_aware"
            language = None
        else:
            ocr = "full_page" if image_pages else "pdf_aware"
            # Counter keeps first-seen order among equal counts, so a tie goes to the earliest page.
            reads = Counter(lang for n in image_pages if (lang := ocr_languages.get(n)))
            language = reads.most_common(1)[0][0] if reads else fallback
        plans.append(
            RangePlan(
                first_page=first,
                last_page=last,
                ocr=ocr,
                ocr_language=language,
                image_pages=image_pages,
            )
        )
    return plans
