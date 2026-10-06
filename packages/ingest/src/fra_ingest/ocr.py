"""OCR engines behind one interface: Apple Vision and Tesseract.

``make_engine`` picks one from the settings, the same value that picks docling's OCR options
(``converter.pipeline_options``). An engine that cannot run raises ``OcrUnavailableError``;
nothing falls back to another.
"""

from __future__ import annotations

import io
import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Any, Protocol

from PIL import Image

from fra_ingest.config import IngestConfig

# Script letters a read must contain to count as a read in that language. Measured at 72 dpi:
# Arabic reads of English pages found 0 to 5 Arabic letters, of Arabic pages 798 to 1,338.
MIN_SCRIPT_CHARS = 10

# The Tesseract executable, for our reads and for docling's TesseractCliOcrOptions.
TESSERACT_COMMAND = "tesseract"

# Seconds one Tesseract read, or its language listing, may take before it fails.
TESSERACT_READ_TIMEOUT_S = 120.0
TESSERACT_LIST_TIMEOUT_S = 30.0

# Our language codes in each engine's own. Vision takes ours as they are; Tesseract's Arabic is
# a setting (``tesseract_arabic_language``), so it is added in ``engine_language``.
_ENGINE_LANGUAGES: Mapping[str, Mapping[str, str]] = {
    "ocrmac": {"ar-SA": "ar-SA", "en-US": "en-US"},
    "tesseract": {"en-US": "eng"},
}
# Direction marks Tesseract appends to words; they are not text.
_DIRECTION_MARKS = "\u200e\u200f"
_STDERR_CHARS = 300


@dataclass(frozen=True)
class OcrLine:
    """One recognised line. Positions are fractions of the page, origin at the top left."""

    text: str
    confidence: float
    left: float
    top: float
    width: float
    height: float

    @property
    def bottom(self) -> float:
        return self.top + self.height


class OcrEngine(Protocol):
    name: str

    def recognize(self, image: Image.Image, languages: Sequence[str]) -> list[OcrLine]: ...


class OcrUnavailableError(RuntimeError):
    """The engine cannot run on this machine, or lacks data for a language that is configured."""


class OcrTimeoutError(RuntimeError):
    """The engine did not answer within its time limit."""


class OcrEngineError(RuntimeError):
    """The engine ran and broke: a failed exit, or output that is not in its format."""


def line_from_vision(
    text: str, confidence: float, bbox: tuple[float, float, float, float]
) -> OcrLine:
    """Vision boxes are (x, y, width, height) with the origin at the bottom left."""
    x, y, width, height = bbox
    return OcrLine(
        text=text,
        confidence=float(confidence),
        left=float(x),
        top=1.0 - float(y) - float(height),
        width=float(width),
        height=float(height),
    )


def engine_language(config: IngestConfig, language: str) -> str:
    """The code the engine ``config.convert_ocr`` names uses for one of our language codes."""
    engine = config.convert_ocr
    codes = dict(_ENGINE_LANGUAGES.get(engine, {}))
    if engine == "tesseract":
        codes["ar-SA"] = config.tesseract_arabic_language
    try:
        return codes[language]
    except KeyError:
        known = sorted(codes)
        msg = f"OCR engine {engine!r} has no mapping for language {language!r}; known: {known}"
        raise ValueError(msg) from None


def lines_from_tesseract_tsv(tsv: str, size: tuple[int, int]) -> list[OcrLine]:
    """Words of Tesseract's TSV output grouped into lines, in reading order. ``size`` is the
    image's (width, height) in pixels. Confidence is the mean word confidence as a fraction."""
    rows = tsv.splitlines()
    columns = rows[0].split("\t") if rows else []
    required = {
        "level",
        "page_num",
        "block_num",
        "par_num",
        "line_num",
        "word_num",
        "left",
        "top",
        "width",
        "height",
        "conf",
        "text",
    }
    missing = sorted(required - set(columns))
    duplicates = sorted({c for c in columns if columns.count(c) > 1})
    if missing or duplicates or columns[-1:] != ["text"]:
        raise OcrEngineError(
            f"tesseract TSV columns: missing {missing}, duplicate {duplicates}; text must be last"
        )
    if columns[:1] != ["level"]:
        msg = f"tesseract output is not TSV: {tsv[:80]!r}"
        raise OcrEngineError(msg)
    width, height = size
    if width <= 0 or height <= 0:
        raise OcrEngineError(f"tesseract image size must be positive, got {size}")
    words: dict[tuple[str, str, str], list[tuple[str, float, int, int, int, int]]] = {}
    for row_no, row in enumerate(rows[1:], start=2):
        cells = row.split("\t")
        if len(cells) != len(columns):
            raise OcrEngineError(
                f"tesseract TSV row {row_no}: expected {len(columns)} columns, got {len(cells)}"
            )
        record = dict(zip(columns, cells, strict=True))
        level = _number(record, "level", int)
        confidence = _number(record, "conf", float)
        if level not in range(1, 6) or not isfinite(confidence) or not -1 <= confidence <= 100:
            raise OcrEngineError(f"tesseract TSV row {row_no}: invalid level or confidence")
        for column in (
            "page_num",
            "block_num",
            "par_num",
            "line_num",
            "word_num",
            "left",
            "top",
            "width",
            "height",
        ):
            if _number(record, column, int) < 0:
                raise OcrEngineError(f"tesseract TSV row {row_no}: negative {column}")
        text = record["text"].strip(f" {_DIRECTION_MARKS}")
        if record["level"] != "5" or not text or _number(record, "conf", float) < 0:
            continue
        key = (record["block_num"], record["par_num"], record["line_num"])
        words.setdefault(key, []).append(
            (
                text,
                _number(record, "conf", float) / 100,
                _number(record, "left", int),
                _number(record, "top", int),
                _number(record, "width", int),
                _number(record, "height", int),
            )
        )
    lines = []
    for line in words.values():
        left = min(w[2] for w in line)
        top = min(w[3] for w in line)
        right = max(w[2] + w[4] for w in line)
        bottom = max(w[3] + w[5] for w in line)
        lines.append(
            OcrLine(
                text=" ".join(w[0] for w in line),
                confidence=sum(w[1] for w in line) / len(line),
                left=left / width,
                top=top / height,
                width=(right - left) / width,
                height=(bottom - top) / height,
            )
        )
    return sort_lines(lines)


def _number[T: (int, float)](record: Mapping[str, str], column: str, kind: type[T]) -> T:
    """One numeric cell of a TSV row; a cell that is not a number means the engine broke."""
    try:
        return kind(record[column])
    except ValueError:
        msg = f"tesseract output has {column} {record[column]!r}, not a number"
        raise OcrEngineError(msg) from None


def sort_lines(lines: Iterable[OcrLine]) -> list[OcrLine]:
    """Reading order for grouping into header and body: rows first, then left to right."""
    return sorted(lines, key=lambda line: (round(line.top, 2), line.left))


class VisionOcr:
    """Apple Vision through ocrmac, accurate mode only: fast mode garbles English and
    rejects Arabic."""

    name = "vision"

    def __init__(self) -> None:
        try:
            from ocrmac import ocrmac
        except ImportError as exc:
            msg = "Apple Vision OCR needs macOS and the ocrmac package (ocrmac is not importable)"
            raise OcrUnavailableError(msg) from exc
        self._ocrmac: Any = ocrmac

    def recognize(self, image: Image.Image, languages: Sequence[str]) -> list[OcrLine]:
        request = self._ocrmac.OCR(
            image, recognition_level="accurate", language_preference=list(languages)
        )
        return sort_lines(
            line_from_vision(text, confidence, bbox)
            for text, confidence, bbox in request.recognize()
        )


class TesseractOcr:
    """Tesseract through its command line, reading the TSV it prints: no Python dependency.
    The page segmentation mode, the Arabic language string and the resolution come from the
    settings, the same ones docling's Tesseract options get."""

    name = "tesseract"

    def __init__(
        self,
        config: IngestConfig,
        *,
        command: str = TESSERACT_COMMAND,
        timeout_s: float = TESSERACT_READ_TIMEOUT_S,
    ) -> None:
        if shutil.which(command) is None:
            msg = f"Tesseract OCR needs the {command!r} executable on the PATH"
            raise OcrUnavailableError(msg)
        self._config = config
        self._command = command
        self._timeout_s = timeout_s
        self._installed: set[str] | None = None
        # Every language the settings name must be readable now, not on the first page of it.
        self._require([engine_language(config, language) for language in config.ocr_languages])

    def recognize(self, image: Image.Image, languages: Sequence[str]) -> list[OcrLine]:
        code = "+".join(engine_language(self._config, language) for language in languages)
        self._require([code])
        png = io.BytesIO()
        image.save(png, format="PNG")
        argv = [
            self._command,
            "stdin",
            "stdout",
            "-l",
            code,
            "--psm",
            str(self._config.tesseract_psm),
            "--dpi",
            str(self._config.ocr_dpi),
            "tsv",
        ]
        try:
            done = subprocess.run(
                argv,
                input=png.getvalue(),
                capture_output=True,
                check=False,
                timeout=self._timeout_s,
            )
        except subprocess.TimeoutExpired:
            msg = f"tesseract did not finish within {self._timeout_s} s"
            raise OcrTimeoutError(msg) from None
        if done.returncode != 0:
            tail = done.stderr.decode("utf-8", errors="replace").strip()[-_STDERR_CHARS:]
            msg = f"tesseract exit {done.returncode}: {tail}"
            raise OcrEngineError(msg)
        try:
            tsv = done.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            msg = f"tesseract output is not UTF-8: {exc}"
            raise OcrEngineError(msg) from None
        return lines_from_tesseract_tsv(tsv, image.size)

    def _require(self, codes: Sequence[str]) -> None:
        """``codes`` are language strings such as ``ara+eng``; each part needs its data."""
        wanted = {part for code in codes for part in code.split("+")}
        missing = sorted(wanted - self._languages())
        if missing:
            msg = f"Tesseract has no language data for {', '.join(missing)}"
            raise OcrUnavailableError(msg)

    def _languages(self) -> set[str]:
        if self._installed is None:
            try:
                done = subprocess.run(
                    [self._command, "--list-langs"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=TESSERACT_LIST_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired:
                msg = f"tesseract --list-langs did not finish within {TESSERACT_LIST_TIMEOUT_S} s"
                raise OcrTimeoutError(msg) from None
            if done.returncode != 0:
                tail = done.stderr.strip()[-_STDERR_CHARS:]
                msg = f"tesseract --list-langs exit {done.returncode}: {tail}"
                raise OcrEngineError(msg)
            # The first line is a heading, then one language code per line.
            self._installed = {line.strip() for line in done.stdout.splitlines()[1:]}
        return self._installed


def read_with_fallback(
    engine: OcrEngine, image: Image.Image, languages: Sequence[str]
) -> tuple[list[OcrLine], str]:
    """Read in each language in turn until the result is in that language's script.

    Vision effectively reads only in the first language it is given: English first destroys
    Arabic, Arabic first degrades English. An Arabic read of an English page returns no Arabic
    letters, so the read shows by itself whether the language was right. Returns the lines and
    the language that produced them; the last language is kept whatever it finds.
    """
    lines: list[OcrLine] = []
    for index, language in enumerate(languages):
        lines = engine.recognize(image, [language])
        last = index == len(languages) - 1
        if last or script_chars(lines, language) >= MIN_SCRIPT_CHARS:
            return lines, language
    return lines, languages[-1]


def script_chars(lines: Sequence[OcrLine], language: str) -> int:
    text = "".join(line.text for line in lines)
    if language.startswith("ar"):
        return sum(1 for char in text if "\u0600" <= char <= "\u06ff")
    return sum(1 for char in text if char.isascii() and char.isalpha())


def make_engine(config: IngestConfig) -> OcrEngine | None:
    """The engine ``config.convert_ocr`` names, or None for ``none``, which leaves image pages
    unread. Every language in ``config.ocr_languages`` must map to the engine's own, and the
    engine must be able to read it: otherwise ``ValueError`` or ``OcrUnavailableError``, here
    and not page by page."""
    if config.convert_ocr == "none":
        return None
    for language in config.ocr_languages:
        engine_language(config, language)
    if config.convert_ocr == "ocrmac":
        return VisionOcr()
    return TesseractOcr(config, command=TESSERACT_COMMAND)
