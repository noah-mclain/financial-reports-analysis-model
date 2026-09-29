"""The convert harness's arithmetic and verdict (spec 10, Scoring)."""

from __future__ import annotations

from harness.convert import failure_row, summarize, verdict

from fra_ingest.errors import IngestError
from fra_ingest.results import ConvertResult, RangeConversion, RangeStatus


def result(statuses: list[RangeStatus], peak: float = 2.0) -> ConvertResult:
    return ConvertResult(
        version="1",
        sha256="d" * 64,
        locate_version="2",
        docling_version="2.126.0",
        device="mps",
        settings_hash="h",
        ranges=[
            RangeConversion(
                first_page=i * 10 + 1,
                last_page=i * 10 + 2,
                ocr="pdf_aware",
                ocr_language="en-US",
                status=status,
                tables=2,
            )
            for i, status in enumerate(statuses)
        ],
        peak_footprint_gb=peak,
        timings={"locate": 1.0, "models": 10.0, "convert": 8.0, "write": 0.1, "child_wall": 25.0},
    )


def test_a_summary_counts_pages_and_seconds_per_page() -> None:
    row = summarize("almarai-2025-en", result(["ok", "ok"]))
    assert row["pages"] == 4
    assert row["tables"] == 4
    assert row["seconds_per_page"] == 2.0
    assert row["statuses"] == ["ok", "ok"]
    assert row["startup_s"] == 25.0 - (1.0 + 10.0 + 8.0 + 0.1)


def test_all_ok_within_budget_passes() -> None:
    assert (
        verdict([summarize("a", result(["ok"])), summarize("b", result(["ok", "ok"]))], 3.0) == []
    )


def test_each_kind_of_miss_is_named() -> None:
    rows = [
        summarize("partial", result(["ok", "partial"])),
        summarize("heavy", result(["ok"], peak=3.4)),
        failure_row("crashed", IngestError("convert_crashed", "signal 9")),
    ]
    reasons = verdict(rows, 3.0)
    assert any(r.startswith("partial:") and "partial" in r for r in reasons)
    assert any(r.startswith("heavy:") and "3.40 GB" in r for r in reasons)
    assert any(r.startswith("crashed:") and "convert_crashed" in r for r in reasons)
