"""Per-page text for the locator: the text layer where there is one, OCR where there is not.

Only OCR is expensive, so only this stage is cached. The cache is keyed by document, stage
version and every setting that changes what is read (ADR 0005).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium

from fra_core.numbers import strip_bidi
from fra_core.schemas import PageMode, PageProfile, TextSource
from fra_ingest.config import IngestConfig
from fra_ingest.errors import IngestError
from fra_ingest.ocr import OcrEngine, read_with_fallback
from fra_ingest.results import PageText
from fra_ingest.text_match import is_visual_arabic

PAGES_STAGE_VERSION = "1"

_FPDF_ERR_PASSWORD = 4
_TEXT_SETTINGS = ("min_text_chars", "header_fraction", "ocr_dpi", "ocr_languages")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_pages(
    pdf_path: Path,
    config: IngestConfig,
    ocr: OcrEngine | None,
    *,
    cache_dir: Path | None = None,
) -> list[PageText]:
    """Read every page. ``cache_dir`` holds ``pages.v<N>.json``; None disables the cache."""
    settings = _settings(config, ocr)
    cache_file = cache_dir / f"pages.v{PAGES_STAGE_VERSION}.json" if cache_dir else None
    if cache_file is not None:
        cached = _load_cache(cache_file, settings)
        if cached is not None:
            return cached

    pdf = _open(pdf_path)
    try:
        if len(pdf) == 0:
            raise IngestError("empty_pdf", str(pdf_path))
        pages = [_read_page(pdf[index], index + 1, config, ocr) for index in range(len(pdf))]
    finally:
        pdf.close()

    if cache_file is not None:
        _write_cache(cache_file, settings, pages)
    return pages


def profiles(pages: Sequence[PageText]) -> list[PageProfile]:
    return [
        PageProfile(
            page_no=page.page_no,
            mode=page.mode,
            char_count=page.char_count,
            width_pt=page.width_pt,
            height_pt=page.height_pt,
        )
        for page in pages
    ]


def document_language(pages: Sequence[PageText]) -> str:
    arabic = sum(page.arabic_chars for page in pages)
    latin = sum(page.latin_chars for page in pages)
    return "ar" if arabic > latin else "en"


def _open(path: Path) -> Any:
    try:
        return pdfium.PdfDocument(path)
    except pdfium.PdfiumError as exc:
        if getattr(exc, "err_code", None) == _FPDF_ERR_PASSWORD:
            raise IngestError("encrypted_pdf", str(path)) from exc
        raise IngestError("unreadable_pdf", f"{path}: {exc}") from exc


def _read_page(page: Any, page_no: int, config: IngestConfig, ocr: OcrEngine | None) -> PageText:
    try:
        width, height = page.get_size()
        textpage = page.get_textpage()
        try:
            full = strip_bidi(textpage.get_text_range())
            split_y = height * (1.0 - config.header_fraction)
            header = strip_bidi(textpage.get_text_bounded(0, split_y, width, height))
            body = strip_bidi(textpage.get_text_bounded(0, 0, width, split_y))
        finally:
            textpage.close()

        char_count = sum(1 for char in full if not char.isspace())
        if char_count >= config.min_text_chars:
            return _page(
                page_no, PageMode.TEXT, TextSource.TEXT, header, body, char_count, width, height
            )
        if ocr is None:
            return _page(
                page_no,
                PageMode.IMAGE,
                None,
                "",
                "",
                char_count,
                width,
                height,
                flags=["ocr_unavailable"],
            )
        return _ocr_page(page, page_no, config, ocr, char_count, width, height)
    finally:
        page.close()


def _ocr_page(
    page: Any,
    page_no: int,
    config: IngestConfig,
    ocr: OcrEngine,
    char_count: int,
    width: float,
    height: float,
) -> PageText:
    image = page.render(scale=config.ocr_dpi / 72).to_pil()
    started = time.perf_counter()
    try:
        lines, language = read_with_fallback(ocr, image, config.ocr_languages)
    except Exception:  # Vision failures surface as assorted Objective-C bridge errors.
        return _page(
            page_no,
            PageMode.IMAGE,
            TextSource.OCR,
            "",
            "",
            char_count,
            width,
            height,
            flags=["ocr_failed"],
        )
    elapsed = time.perf_counter() - started
    header = "\n".join(line.text for line in lines if line.top < config.header_fraction)
    body = "\n".join(line.text for line in lines if line.top >= config.header_fraction)
    return _page(
        page_no,
        PageMode.IMAGE,
        TextSource.OCR,
        header,
        body,
        char_count,
        width,
        height,
        ocr_language=language,
        ocr_seconds=elapsed,
    )


def _page(
    page_no: int,
    mode: PageMode,
    source: TextSource | None,
    header: str,
    body: str,
    char_count: int,
    width: float,
    height: float,
    *,
    flags: list[str] | None = None,
    ocr_language: str | None = None,
    ocr_seconds: float = 0.0,
) -> PageText:
    text = f"{header}\n{body}"
    return PageText(
        page_no=page_no,
        mode=mode,
        source=source,
        header_text=header,
        body_text=body,
        char_count=char_count,
        width_pt=width,
        height_pt=height,
        arabic_chars=sum(1 for char in text if "؀" <= char <= "ۿ"),
        latin_chars=sum(1 for char in text if char.isascii() and char.isalpha()),
        visual_arabic=mode is PageMode.TEXT and is_visual_arabic(text),
        ocr_language=ocr_language,
        ocr_seconds=ocr_seconds,
        flags=flags or [],
    )


def _settings(config: IngestConfig, ocr: OcrEngine | None) -> dict[str, Any]:
    return {
        "min_text_chars": config.min_text_chars,
        "header_fraction": config.header_fraction,
        "ocr_dpi": config.ocr_dpi,
        "ocr_languages": list(config.ocr_languages),
        "ocr_engine": ocr.name if ocr is not None else None,
    }


def _usable(payload: dict[str, Any], settings: dict[str, Any]) -> bool:
    """Same text settings, and OCR at least as good as this run's: a cache with OCR also
    serves a run without an engine, never the other way round."""
    if payload.get("version") != PAGES_STAGE_VERSION:
        return False
    cached = payload.get("settings", {})
    if any(cached.get(key) != settings[key] for key in _TEXT_SETTINGS):
        return False
    engine, cached_engine = settings["ocr_engine"], cached.get("ocr_engine")
    return bool(cached_engine == engine or (engine is None and cached_engine is not None))


def _load_cache(path: Path, settings: dict[str, Any]) -> list[PageText] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not _usable(payload, settings):
        return None
    return [PageText.model_validate(page) for page in payload["pages"]]


def _write_cache(path: Path, settings: dict[str, Any], pages: Sequence[PageText]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": PAGES_STAGE_VERSION,
        "settings": settings,
        "pages": [page.model_dump(mode="json") for page in pages],
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)
