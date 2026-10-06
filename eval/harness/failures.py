"""One rule for a document a harness could not read.

A page the OCR engine cannot read, or does not answer for in time, fails its document, not the
run: the harness records the reason and the detail and goes on. When the first documents of a run
all fail the same way on the engine, the engine is broken and the run stops (`EngineGuard`); each
harness then writes the report of the rows it has, marked `aborted`, and raises again. An engine
that cannot run at all (`OcrUnavailableError`) is never a document failure.
"""

from __future__ import annotations

from fra_ingest.errors import IngestError
from fra_ingest.ocr import PER_DOCUMENT_OCR_ERRORS, ocr_failure

DOCUMENT_ERRORS = (IngestError, *PER_DOCUMENT_OCR_ERRORS)
# How many documents show that the engine, not the pool, is the problem.
ENGINE_PROBE = 3
_ENGINE_REASONS = ("ocr_timeout", "ocr_engine")


class EngineFailure(RuntimeError):
    """The first documents of a run all failed the same way on the OCR engine."""


def document_failure(exc: Exception) -> dict[str, str]:
    """The reason and detail to record against a document: an ingest error as it is, an OCR
    timeout or engine error as `ocr_timeout` or `ocr_engine`. Anything else is raised again."""
    failure = exc if isinstance(exc, IngestError) else ocr_failure(exc)
    return {"error": failure.reason, "detail": failure.detail}


class EngineGuard:
    """Watches the first `min(ENGINE_PROBE, planned)` documents a run attempts. `record` takes
    each one's failure reason (and its detail), or None when it was read."""

    def __init__(self, planned: int) -> None:
        self._planned = planned
        self._probe = min(ENGINE_PROBE, planned)
        self._outcomes: list[str | None] = []
        self._detail = ""

    def record(self, reason: str | None, detail: str = "") -> None:
        if len(self._outcomes) >= self._probe:
            return
        self._outcomes.append(reason)
        self._detail = detail
        if len(self._outcomes) == self._probe:
            self._check()

    def finish(self) -> None:
        """For a run that attempted fewer documents than it planned (files were missing)."""
        if len(self._outcomes) < self._probe:
            self._check()

    def _check(self) -> None:
        reasons = set(self._outcomes)
        if len(reasons) != 1 or not reasons <= set(_ENGINE_REASONS):
            return
        (reason,) = reasons
        count = len(self._outcomes)
        which = "every document" if count == self._planned else f"the first {count} documents"
        raise EngineFailure(f"{which} failed with {reason}: {self._detail}".rstrip(": "))
