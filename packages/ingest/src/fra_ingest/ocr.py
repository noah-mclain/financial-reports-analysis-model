"""OCR engines behind one interface.

Apple Vision is the only engine in week 1. Tesseract and RapidOCR join in week 2 behind the
same protocol, so nothing that calls ``recognize`` changes.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from PIL import Image

# Script letters a read must contain to count as a read in that language. Measured at 72 dpi:
# Arabic reads of English pages found 0 to 5 Arabic letters, of Arabic pages 798 to 1,338.
MIN_SCRIPT_CHARS = 10


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
    """The engine cannot run on this machine."""


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
            msg = "Apple Vision OCR needs macOS and the ocrmac package"
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
        if last or _script_chars(lines, language) >= MIN_SCRIPT_CHARS:
            return lines, language
    return lines, languages[-1]


def _script_chars(lines: Sequence[OcrLine], language: str) -> int:
    text = "".join(line.text for line in lines)
    if language.startswith("ar"):
        return sum(1 for char in text if "\u0600" <= char <= "\u06ff")
    return sum(1 for char in text if char.isascii() and char.isalpha())


def default_engine() -> OcrEngine | None:
    """The engine for this machine, or None when there is none."""
    try:
        return VisionOcr()
    except OcrUnavailableError:
        return None
