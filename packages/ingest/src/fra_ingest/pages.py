"""Per-page text for the locator: the text layer where there is one, OCR where there is not.

Only OCR is expensive, so only this stage is cached. The cache is keyed by document, stage
version and every setting that changes what is read (ADR 0005).
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PIL import Image
from pydantic import ValidationError

from fra_core.numbers import strip_bidi
from fra_core.schemas import PageMode, PageProfile, TextSource
from fra_ingest.config import IngestConfig
from fra_ingest.errors import IngestError
from fra_ingest.ocr import (
    OcrEngine,
    OcrEngineError,
    OcrLine,
    OcrTimeoutError,
    OcrUnavailableError,
    read_with_fallback,
)
from fra_ingest.results import PageText
from fra_ingest.text_match import is_visual_arabic

PAGES_STAGE_VERSION = "5"  # strict Tesseract output validation; v4 empty successes are unsafe

_FPDF_ERR_PASSWORD = 4
# Errors that mean the code calling the engine is wrong, not that recognition failed. They are
# raised, not recorded as ocr_failed, so a broken engine cannot pass as unreadable scans.
_PROGRAMMING_ERRORS = (TypeError, AttributeError, NameError, IndexError, KeyError)
# Errors that mean the engine cannot be trusted to read at all (no binary, no language data, no
# answer in time, a failed exit or output that is not its format). They are raised too: flagging
# them would score a broken setup as unreadable scans.
_ENGINE_ERRORS = (OcrUnavailableError, OcrTimeoutError, OcrEngineError)
_GARBLE_MIN_CHARS = 100
_GARBLE_READABLE_SHARE = 0.9
_TEXT_SETTINGS = ("min_text_chars", "header_fraction", "ocr_dpi", "ocr_languages")


# The page flag that says the text layer is noise; the locator and the corpus screening read it.
GARBLED_TEXT_LAYER = "garbled_text_layer"


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


def text_layer_is_garbled(text: str) -> bool:
    """Whether a text layer is unusable: too many of its visible characters belong to no
    script a filing is written in. Some filings embed fonts whose glyphs map to Greek letters
    or to modifier and combining marks (measured on the train pool: Al Kathiri's text, Naba's
    digits), and their text layer reads as noise."""
    visible = [char for char in text if not char.isspace()]
    if len(visible) < _GARBLE_MIN_CHARS:
        return False
    readable = sum(1 for char in visible if _readable(char))
    return readable / len(visible) < _GARBLE_READABLE_SHARE


def _readable(char: str) -> bool:
    code = ord(char)
    return (
        char.isascii()
        or char.isdecimal()
        or 0x00A0 <= code <= 0x024F  # Latin-1 punctuation and Latin with diacritics
        or 0x0600 <= code <= 0x06FF  # Arabic, including its digits and punctuation
        or 0xFB50 <= code <= 0xFDFF  # Arabic presentation forms A
        or 0xFE70 <= code <= 0xFEFF  # Arabic presentation forms B
        or 0x2000 <= code <= 0x20CF  # general punctuation and currency signs
    )


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
            header_box, body_box = _regions(page, width, height, config.header_fraction)
            header = strip_bidi(textpage.get_text_bounded(*header_box))
            body = strip_bidi(textpage.get_text_bounded(*body_box))
        finally:
            textpage.close()

        char_count = sum(1 for char in full if not char.isspace())
        garbled = char_count >= config.min_text_chars and text_layer_is_garbled(full)
        if char_count >= config.min_text_chars and not (garbled and ocr is not None):
            return _page(
                page_no,
                PageMode.TEXT,
                TextSource.TEXT,
                header,
                body,
                char_count,
                width,
                height,
                flags=[GARBLED_TEXT_LAYER] if garbled else None,
            )
        if garbled and ocr is not None:
            page_text = _ocr_page(page, page_no, config, ocr, char_count, width, height)
            page_text.flags.append(GARBLED_TEXT_LAYER)
            return page_text
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


def _regions(
    page: Any, width: float, height: float, header_fraction: float
) -> tuple[tuple[float, float, float, float], tuple[float, float, float, float]]:
    """Header and body rectangles in page space, for ``get_text_bounded``.

    The header is the top of the page as displayed. Text coordinates are in unrotated page
    space while ``get_size`` gives the displayed size, so both rectangles are drawn in display
    space and mapped back through pdfium, which also accounts for a mediabox that does not
    start at the origin.
    """
    split = height * header_fraction
    return (
        _to_page_rect(page, width, height, (0.0, 0.0, width, split)),
        _to_page_rect(page, width, height, (0.0, split, width, height)),
    )


def _to_page_rect(
    page: Any, width: float, height: float, rect: tuple[float, float, float, float]
) -> tuple[float, float, float, float]:
    rotate = page.get_rotation() // 90
    size_x, size_y = round(width), round(height)
    xs: list[float] = []
    ys: list[float] = []
    for device_x, device_y in ((rect[0], rect[1]), (rect[2], rect[3])):
        page_x, page_y = ctypes.c_double(), ctypes.c_double()
        pdfium_c.FPDF_DeviceToPage(
            page.raw,
            0,
            0,
            size_x,
            size_y,
            rotate,
            round(device_x),
            round(device_y),
            ctypes.byref(page_x),
            ctypes.byref(page_y),
        )
        xs.append(page_x.value)
        ys.append(page_y.value)
    return min(xs), min(ys), max(xs), max(ys)


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
    timed = _TimedEngine(ocr)
    try:
        lines, language = read_with_fallback(timed, image, config.ocr_languages)
    except (*_PROGRAMMING_ERRORS, *_ENGINE_ERRORS):
        raise
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
        ocr_call_seconds=timed.seconds,
    )


class _TimedEngine:
    """An engine that records how long each of its reads took. A page can take two: Arabic,
    then English when the Arabic read found no Arabic."""

    def __init__(self, engine: OcrEngine) -> None:
        self._engine = engine
        self.name = engine.name
        self.seconds: list[float] = []

    def recognize(self, image: Image.Image, languages: Sequence[str]) -> list[OcrLine]:
        started = time.perf_counter()
        lines = self._engine.recognize(image, languages)
        self.seconds.append(time.perf_counter() - started)
        return lines


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
    ocr_call_seconds: Sequence[float] = (),
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
        ocr_seconds=sum(ocr_call_seconds),
        ocr_call_seconds=list(ocr_call_seconds),
        flags=flags or [],
    )


def _settings(config: IngestConfig, ocr: OcrEngine | None) -> dict[str, Any]:
    return {
        "min_text_chars": config.min_text_chars,
        "header_fraction": config.header_fraction,
        "ocr_dpi": config.ocr_dpi,
        "ocr_languages": list(config.ocr_languages),
        "ocr_engine": ocr.name if ocr is not None else None,
        # What the settings change in a Tesseract read; no other engine looks at them.
        "ocr_options": (
            {
                "psm": config.tesseract_psm,
                "arabic_language": config.tesseract_arabic_language,
            }
            if ocr is not None and ocr.name == "tesseract"
            else None
        ),
    }


def ocr_key(config: IngestConfig, ocr: OcrEngine | None) -> str:
    """A digest of everything that decides what the pages read as, for stages that keep
    results derived from them."""
    payload = {"pages": PAGES_STAGE_VERSION, **_settings(config, ocr)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _usable(payload: Any, settings: dict[str, Any]) -> bool:
    """Same text settings, and OCR at least as good as this run's: a cache with OCR also
    serves a run without an engine, never the other way round. A cache holding a failed OCR
    read is never reused while an engine is available, so a transient failure is retried."""
    if not isinstance(payload, dict) or payload.get("version") != PAGES_STAGE_VERSION:
        return False
    if settings["ocr_engine"] is not None and any(
        "ocr_failed" in page.get("flags", []) for page in payload.get("pages", [])
    ):
        return False
    cached = payload.get("settings")
    if not isinstance(cached, dict):
        return False
    if any(cached.get(key) != settings[key] for key in _TEXT_SETTINGS):
        return False
    engine, cached_engine = settings["ocr_engine"], cached.get("ocr_engine")
    if engine is None:
        return cached_engine is not None
    return bool(cached_engine == engine and cached.get("ocr_options") == settings["ocr_options"])


def _load_cache(path: Path, settings: dict[str, Any]) -> list[PageText] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not _usable(payload, settings):
        return None
    try:
        pages = [PageText.model_validate(page) for page in payload["pages"]]
    except (KeyError, TypeError, ValidationError):
        return None
    # The cache stores text, not this run's cost: no OCR ran for these pages now.
    return [page.model_copy(update={"ocr_seconds": 0.0, "ocr_call_seconds": []}) for page in pages]


def recorded_ocr_calls(cache_dir: Path) -> list[float] | None:
    """The seconds of every OCR call the run that wrote the page cache in ``cache_dir`` made,
    in page order; None when there is no readable cache. The cache keeps the cost of the run
    that wrote it, so this is real work only when that run was this one."""
    path = cache_dir / f"pages.v{PAGES_STAGE_VERSION}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pages = [PageText.model_validate(page) for page in payload["pages"]]
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValidationError):
        return None
    return [seconds for page in pages for seconds in page.ocr_call_seconds]


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
