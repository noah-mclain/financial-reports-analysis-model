"""Helpers shared by the ingest tests."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

import pypdfium2 as pdfium
from PIL import Image

from fra_core.schemas import Document, PageMode, PageProfile, TextSource
from fra_ingest.converter import RangeOutput
from fra_ingest.locate import LOCATE_VERSION
from fra_ingest.ocr import OcrLine
from fra_ingest.results import IndustrySignal, LocateResult, PageText, RangePlan

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


def text_page(header: str, body: str = "", *, page_no: int = 1, visual: bool = False) -> PageText:
    return PageText(
        page_no=page_no,
        mode=PageMode.TEXT,
        source=TextSource.TEXT,
        header_text=header,
        body_text=body,
        char_count=len((header + body).replace(" ", "")),
        width_pt=595,
        height_pt=842,
        visual_arabic=visual,
    )


def numbers_block(rows: int = 25, label: str = "Line") -> str:
    """Statement-like rows with two period columns of values in the thousands."""
    return "\n".join(f"{label} {i} {1000 + i * 37:,} {900 + i * 29:,}" for i in range(rows))


def located(
    modes: Sequence[PageMode],
    ranges: Sequence[tuple[int, int]],
    *,
    language: str = "en",
    sha256: str = "0" * 64,
) -> LocateResult:
    """A locate result over pages of the given modes, with the given convert ranges."""
    document = Document(
        sha256=sha256,
        filename="doc.pdf",
        page_count=len(modes),
        pages=[
            PageProfile(
                page_no=i + 1,
                mode=mode,
                char_count=0 if mode is PageMode.IMAGE else 500,
                width_pt=595,
                height_pt=842,
            )
            for i, mode in enumerate(modes)
        ],
        language=language,
    )
    return LocateResult(
        version=LOCATE_VERSION,
        document=document,
        pages=[],
        ranges=[],
        convert_ranges=list(ranges),
        industry=IndustrySignal(kind="corporate"),
    )


class FakeRunner:
    """Stands in for DoclingRunner. ``behaviour`` maps a range label such as "2-3" to one of
    ok, partial, failed, raise, interrupt, outside, no_image, short or write_fails; unlisted
    ranges are ok. ``interrupt`` raises ``KeyboardInterrupt``, a ``BaseException`` the stage
    must let through rather than turn into a failed range."""

    def __init__(self, behaviour: Mapping[str, str] | None = None) -> None:
        self.behaviour = dict(behaviour or {})
        self.calls: list[RangePlan] = []
        self.models_seconds = 0.0

    def __call__(self, pdf: Path, plan: RangePlan) -> RangeOutput:
        self.calls.append(plan)
        kind = self.behaviour.get(plan.label, "ok")
        if kind == "raise":
            msg = "docling stopped"
            raise RuntimeError(msg)
        if kind == "interrupt":
            raise KeyboardInterrupt
        pages = list(range(plan.first_page, plan.last_page + 1))
        if kind == "outside":
            pages = [n + 100 for n in pages]
        if kind == "short":
            pages = pages[:1]
        images: dict[int, Image.Image | None] = {
            n: None if kind == "no_image" else Image.new("RGB", (20, 30), "white") for n in pages
        }
        statuses: dict[str, Literal["ok", "partial", "failed"]] = {
            "partial": "partial",
            "failed": "failed",
        }
        status = statuses.get(kind, "ok")
        return RangeOutput(
            status=status,
            page_numbers=pages,
            page_images=images,
            tables=1,
            errors=[] if status == "ok" else ["page 3: document timeout exceeded"],
            write_json=_write_failing_json if kind == "write_fails" else _write_stub_json,
        )


def _write_stub_json(path: Path) -> None:
    path.write_text("{}", encoding="utf-8")


def _write_failing_json(path: Path) -> None:
    msg = "disk full"
    raise OSError(msg)


def run_python(code: str) -> subprocess.CompletedProcess[str]:
    """Run code in a fresh interpreter that sees the same packages as the tests. uv writes the
    workspace .pth files hidden on this machine and Python skips hidden .pth files, so the
    path is passed explicitly."""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    )
