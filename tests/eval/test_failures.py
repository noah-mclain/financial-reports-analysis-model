"""The one rule for a document the OCR engine could not read (week 2 tightening, Task 2)."""

from __future__ import annotations

import pytest
from harness.failures import (
    DOCUMENT_ERRORS,
    ENGINE_PROBE,
    EngineFailure,
    EngineGuard,
    document_failure,
)

from fra_ingest.errors import IngestError
from fra_ingest.ocr import OcrEngineError, OcrTimeoutError, OcrUnavailableError


def test_ocr_timeout_and_engine_errors_map_to_their_reasons_with_detail() -> None:
    assert document_failure(OcrTimeoutError("did not finish within 120.0 s")) == {
        "error": "ocr_timeout",
        "detail": "did not finish within 120.0 s",
    }
    assert document_failure(OcrEngineError("exit 1")) == {"error": "ocr_engine", "detail": "exit 1"}
    assert document_failure(IngestError("unreadable_pdf", "x.pdf: not a pdf")) == {
        "error": "unreadable_pdf",
        "detail": "x.pdf: not a pdf",
    }
    assert set(DOCUMENT_ERRORS) == {IngestError, OcrTimeoutError, OcrEngineError}


def test_unavailable_engine_is_not_a_document_failure() -> None:
    assert OcrUnavailableError not in DOCUMENT_ERRORS
    with pytest.raises(OcrUnavailableError):
        document_failure(OcrUnavailableError("no tesseract"))


def test_guard_stops_when_the_first_three_fail_the_same_ocr_way() -> None:
    assert ENGINE_PROBE == 3
    guard = EngineGuard(planned=10)
    guard.record("ocr_engine")
    guard.record("ocr_engine")
    with pytest.raises(EngineFailure, match="ocr_engine") as caught:
        guard.record("ocr_engine")
    assert isinstance(caught.value, RuntimeError)


def test_guard_does_not_stop_on_mixed_or_ingest_failures() -> None:
    mixed = EngineGuard(planned=10)
    for kind in ("ocr_engine", "ocr_timeout", "ocr_engine", "ocr_engine"):
        mixed.record(kind)
    mixed.finish()
    ingest = EngineGuard(planned=10)
    for kind in ("unreadable_pdf", "unreadable_pdf", "unreadable_pdf"):
        ingest.record(kind)
    ingest.finish()


def test_guard_stops_a_one_document_run_that_fails_on_the_engine() -> None:
    with pytest.raises(EngineFailure):
        EngineGuard(planned=1).record("ocr_timeout")
    short = EngineGuard(planned=5)  # three planned were never attempted: the files were missing
    short.record("ocr_timeout")
    short.record("ocr_timeout")
    with pytest.raises(EngineFailure):
        short.finish()


def test_guard_ignores_a_failure_after_a_success() -> None:
    guard = EngineGuard(planned=10)
    guard.record(None)
    for _ in range(5):
        guard.record("ocr_engine")
    guard.finish()
