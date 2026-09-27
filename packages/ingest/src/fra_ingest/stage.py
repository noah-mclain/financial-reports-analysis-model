"""The locate stage end to end: read pages, locate, time it, write ``locate.json``."""

from __future__ import annotations

import os
import time
from pathlib import Path

from fra_ingest.config import IngestConfig
from fra_ingest.locate import locate
from fra_ingest.ocr import OcrEngine
from fra_ingest.pages import read_pages, sha256_file
from fra_ingest.results import LocateResult


def locate_pdf(
    pdf_path: Path,
    config: IngestConfig,
    ocr: OcrEngine | None,
    *,
    use_cache: bool = True,
) -> LocateResult:
    sha256 = sha256_file(pdf_path)
    out_dir = config.artifact_root / sha256

    started = time.perf_counter()
    pages = read_pages(pdf_path, config, ocr, cache_dir=out_dir if use_cache else None)
    read_seconds = time.perf_counter() - started
    ocr_seconds = sum(page.ocr_seconds for page in pages)

    result = locate(
        pages,
        config,
        sha256=sha256,
        filename=pdf_path.name,
        timings={"read": read_seconds, "ocr": ocr_seconds},
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    temporary = out_dir / "locate.json.tmp"
    temporary.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    os.replace(temporary, out_dir / "locate.json")
    return result
