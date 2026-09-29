"""What the convert stage writes to convert.json (spec 10, Data model)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fra_ingest.results import ConvertResult, RangeConversion, RangePlan, RangeStatus

SHA = "a" * 64


def conversion(status: RangeStatus, first: int = 2, last: int = 3) -> RangeConversion:
    return RangeConversion(
        first_page=first,
        last_page=last,
        ocr="pdf_aware",
        ocr_language="en-US",
        status=status,
    )


def result(*statuses: RangeStatus) -> ConvertResult:
    return ConvertResult(
        version="1",
        sha256=SHA,
        locate_version="2",
        docling_version="2.126.0",
        device="mps",
        settings_hash="h",
        ranges=[conversion(s, i * 10 + 1, i * 10 + 2) for i, s in enumerate(statuses)],
    )


def test_a_result_survives_a_json_round_trip() -> None:
    original = result("ok", "partial").model_copy(
        update={"page_images": {12: "pages/12.png"}, "peak_footprint_gb": 2.4}
    )
    again = ConvertResult.model_validate_json(original.model_dump_json())
    assert again == original
    assert again.page_images == {12: "pages/12.png"}


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (("failed",), True),
        (("failed", "skipped"), True),
        (("failed", "ok"), False),
        (("failed", "partial"), False),
        (("skipped",), False),
        ((), False),
    ],
)
def test_all_failed_counts_only_attempted_ranges(
    statuses: tuple[RangeStatus, ...], expected: bool
) -> None:
    assert result(*statuses).all_failed is expected


def test_a_plan_has_a_label_and_rejects_a_reversed_range() -> None:
    plan = RangePlan(first_page=12, last_page=15, ocr="full_page", ocr_language="ar-SA")
    assert plan.label == "12-15"
    with pytest.raises(ValidationError):
        RangePlan(first_page=5, last_page=4, ocr="pdf_aware", ocr_language="en-US")
