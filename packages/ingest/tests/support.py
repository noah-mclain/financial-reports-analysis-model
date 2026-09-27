"""Helpers shared by the ingest tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

from fra_ingest.ocr import OcrLine

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_DIR = REPO_ROOT / "eval" / "golden" / "documents"


class FakeOcr:
    """Returns fixed lines and records calls. ``by_language`` answers per first language, as
    Vision effectively reads only in the first language it is given. ``fail`` makes every call
    raise."""

    name = "fake"

    def __init__(
        self,
        lines: Sequence[OcrLine] = (),
        *,
        by_language: Mapping[str, Sequence[OcrLine]] | None = None,
        fail: bool = False,
    ) -> None:
        self.lines = list(lines)
        self.by_language = {k: list(v) for k, v in (by_language or {}).items()}
        self.fail = fail
        self.calls = 0
        self.languages: list[tuple[str, ...]] = []

    def recognize(self, image: Image.Image, languages: Sequence[str]) -> list[OcrLine]:
        self.calls += 1
        self.languages.append(tuple(languages))
        if self.fail:
            msg = "vision request failed"
            raise RuntimeError(msg)
        return list(self.by_language.get(languages[0], self.lines))


def make_blank_pdf(path: Path, pages: int = 1) -> Path:
    """A PDF whose pages have no text layer, so every page is an image page."""
    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(595, 842)
    pdf.save(path)
    pdf.close()
    return path
