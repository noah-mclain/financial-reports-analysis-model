"""The locate stage end to end: read pages, locate, time it, write ``locate.json``."""

from __future__ import annotations

import os
import time
from pathlib import Path

from pydantic import ValidationError

from fra_ingest.config import IngestConfig
from fra_ingest.errors import IngestError
from fra_ingest.locate import LOCATE_VERSION, locate
from fra_ingest.ocr import OcrEngine
from fra_ingest.pages import ocr_key, read_pages, sha256_file
from fra_ingest.progress import Progress
from fra_ingest.results import LocateResult


def locate_pdf(
    pdf_path: Path,
    config: IngestConfig,
    ocr: OcrEngine | None,
    *,
    use_cache: bool = True,
    progress: Progress | None = None,
) -> LocateResult:
    if not pdf_path.is_file():
        raise IngestError("unreadable_pdf", f"{pdf_path}: not a file")
    sha256 = sha256_file(pdf_path)
    out_dir = config.artifact_root / sha256

    started = time.perf_counter()
    pages = read_pages(
        pdf_path, config, ocr, cache_dir=out_dir if use_cache else None, progress=progress
    )
    read_seconds = time.perf_counter() - started
    ocr_seconds = min(sum(page.ocr_seconds for page in pages), read_seconds)

    result = locate(
        pages,
        config,
        sha256=sha256,
        filename=pdf_path.name,
        timings={"read": read_seconds, "text": read_seconds - ocr_seconds, "ocr": ocr_seconds},
    ).model_copy(
        update={
            "ocr_engine": ocr.name if ocr is not None else None,
            "ocr_key": ocr_key(config, ocr),
        }
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    temporary = out_dir / "locate.json.tmp"
    temporary.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    os.replace(temporary, out_dir / "locate.json")
    return result


def load_or_locate(
    pdf_path: Path,
    config: IngestConfig,
    ocr: OcrEngine | None,
    *,
    progress: Progress | None = None,
) -> LocateResult:
    """The stored locate result when it is current and was made by the same engine and OCR
    settings, else a fresh one. A corrupt or older ``locate.json`` is recomputed, never
    trusted."""
    if not pdf_path.is_file():
        raise IngestError("unreadable_pdf", f"{pdf_path}: not a file")
    stored = config.artifact_root / sha256_file(pdf_path) / "locate.json"
    if stored.is_file():
        try:
            result = LocateResult.model_validate_json(stored.read_text(encoding="utf-8"))
        except (OSError, ValidationError):
            result = None
        # Pages read by another engine, or with other OCR settings, would locate differently.
        if (
            result is not None
            and result.version == LOCATE_VERSION
            and result.ocr_key == ocr_key(config, ocr)
            and not (
                ocr is not None and any(f.startswith("ocr_failed_pages:") for f in result.flags)
            )
        ):
            return result
    return locate_pdf(pdf_path, config, ocr, progress=progress)


def page_ocr_languages(
    pdf_path: Path,
    config: IngestConfig,
    ocr: OcrEngine | None,
    *,
    progress: Progress | None = None,
) -> dict[int, str | None]:
    """The OCR language Part 1 kept for each page, read back from the page cache locate
    filled, so no OCR runs here when locate ran with the same engine."""
    out_dir = config.artifact_root / sha256_file(pdf_path)
    return {
        page.page_no: page.ocr_language
        for page in read_pages(pdf_path, config, ocr, cache_dir=out_dir, progress=progress)
    }
