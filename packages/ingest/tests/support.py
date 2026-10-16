"""Helpers shared by the ingest tests."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

import pypdfium2 as pdfium
from PIL import Image

from fra_core.schemas import Document, PageMode, PageProfile, StatementType, TextSource
from fra_ingest.converter import RangeOutput
from fra_ingest.docling_json import DlDocument
from fra_ingest.locate import LOCATE_VERSION
from fra_ingest.ocr import OcrLine
from fra_ingest.results import (
    ConvertResult,
    IndustrySignal,
    LocateResult,
    PageScore,
    PageText,
    RangeConversion,
    RangePlan,
    StatementRange,
)
from fra_ingest.structure import StructureInputs

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_DIR = REPO_ROOT / "eval" / "golden" / "documents"


def artifact_dir(pdf: Path) -> Path:
    """Where Part 2 left this document's artifacts, under the repository's var/artifacts."""
    return REPO_ROOT / "var" / "artifacts" / hashlib.sha256(pdf.read_bytes()).hexdigest()


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
    primary_type: StatementType = StatementType.BALANCE,
    statement_ranges: Sequence[StatementRange] | None = None,
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
        pages=[
            PageScore(page_no=n, type_scores={primary_type: 8}, title_types=[primary_type])
            for n in range(1, len(modes) + 1)
        ],
        ranges=list(statement_ranges)
        if statement_ranges is not None
        else [
            StatementRange(
                type=primary_type,
                first_page=min(a for a, _ in ranges),
                last_page=max(b for _, b in ranges),
                rank=1,
                score=8,
            )
        ]
        if ranges
        else [],
        convert_ranges=list(ranges),
        industry=IndustrySignal(kind="corporate"),
    )


class FakeRunner:
    """Stands in for DoclingRunner. ``behaviour`` maps a range label such as "2-3" to one of
    ok, partial, failed, raise, interrupt, outside, no_image, short, zero_pages or write_fails;
    unlisted ranges are ok. ``interrupt`` raises ``KeyboardInterrupt``, a ``BaseException`` the
    stage must let through rather than turn into a failed range."""

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
        if kind == "zero_pages":
            pages = []
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


def located_titles(
    titles: Mapping[int, Sequence[StatementType]],
    cues: Mapping[int, Sequence[StatementType]] | None = None,
) -> LocateResult:
    """Explicit primary sources for synthetic tables, with page evidence for every page."""
    cues = cues or {}
    count = max([2, *titles, *cues])
    where = located([PageMode.TEXT] * count, [(1, count)], sha256="b" * 64, statement_ranges=[])
    where.pages = [
        PageScore(
            page_no=n,
            type_scores={t: 8 for t in (*titles.get(n, ()), *cues.get(n, ()))},
            title_types=list(titles.get(n, ())),
            cue_types=list(cues.get(n, ())),
        )
        for n in range(1, count + 1)
    ]
    where.ranges = [
        StatementRange(
            type=t,
            first_page=min(p.page_no for p in where.pages if t in (*p.title_types, *p.cue_types)),
            last_page=max(p.page_no for p in where.pages if t in (*p.title_types, *p.cue_types)),
            rank=1,
            score=8,
        )
        for t in StatementType
        if any(t in (*p.title_types, *p.cue_types) for p in where.pages)
    ]
    return where


STRUCTURE_SHA = "b" * 64


def cells(rows: list[list[str]], x0: float = 60) -> list[dict[str, object]]:
    out = []
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            if not text:
                continue
            left = x0 + 150 * c
            out.append(
                {
                    "text": text,
                    "start_row_offset_idx": r,
                    "end_row_offset_idx": r + 1,
                    "start_col_offset_idx": c,
                    "end_col_offset_idx": c + 1,
                    "column_header": r == 0,
                    "bbox": {
                        "l": left,
                        "t": 100 + 20 * r,
                        "r": left + 120,
                        "b": 112 + 20 * r,
                        "coord_origin": "TOPLEFT",
                    },
                }
            )
    return out


def table(ref: int, page: int, rows: list[list[str]], x0: float = 60) -> dict[str, object]:
    return {
        "self_ref": f"#/tables/{ref}",
        "prov": [
            {
                "page_no": page,
                "bbox": {"l": 50, "t": 700, "r": 700, "b": 300, "coord_origin": "BOTTOMLEFT"},
            }
        ],
        "data": {"num_rows": len(rows), "num_cols": 4, "table_cells": cells(rows, x0)},
    }


HEADER = ["", "Notes", "31 December 2025 SAR '000", "31 December 2024 SAR '000"]


PAGE_1 = [
    HEADER,
    ["Inventories", "19", "10", "8"],
    ["Cash", "21", "5", "4"],
    ["Total current assets", "", "15", "12"],
    ["Total assets", "", "15", "12"],
]


PAGE_2 = [
    HEADER,
    ["Trade payables", "", "6", "5"],
    ["Total liabilities", "", "6", "5"],
    ["Share capital", "", "9", "7"],
    ["Total equity", "", "9", "7"],
]


def inputs(documents: list[tuple[str, DlDocument]]) -> StructureInputs:
    convert = ConvertResult(
        version="1",
        sha256=STRUCTURE_SHA,
        locate_version="2",
        docling_version="2.126.0",
        device="mps",
        settings_hash="h",
        ranges=[
            RangeConversion(
                first_page=1,
                last_page=2,
                ocr="pdf_aware",
                ocr_language="en-US",
                docling_path="docling/p1-2.json",
                status="ok",
            )
        ],
    )
    where = located(
        [PageMode.TEXT] * max([2, *(int(n) for _, d in documents for n in d.pages)]),
        [(1, 2)],
        sha256=STRUCTURE_SHA,
    )
    where.ranges = [
        StatementRange(type=StatementType.BALANCE, first_page=1, last_page=2, rank=1, score=8)
    ]
    where.pages = [
        PageScore(
            page_no=n, type_scores={StatementType.BALANCE: 8}, title_types=[StatementType.BALANCE]
        )
        for n in range(1, where.document.page_count + 1)
    ]
    return StructureInputs(
        located=where,
        sha256=STRUCTURE_SHA,
        language="en",
        industry_flags=(),
        page_modes={1: PageMode.TEXT, 2: PageMode.TEXT},
        visual_pages=set(),
        page_texts={1: "Statement of financial position", 2: ""},
        documents=documents,
        convert=convert,
    )


INCOME_HEADER = ["", "Notes", "2025 SAR '000", "2024 SAR '000"]


INCOME_1 = [
    INCOME_HEADER,
    ["Revenue", "5", "100", "90"],
    ["Cost of sales", "", "(60)", "(55)"],
    ["Gross profit", "", "40", "35"],
]


def span(kind: StatementType, first: int, last: int | None = None, rank: int = 1) -> StatementRange:
    return StatementRange(type=kind, first_page=first, last_page=last or first, rank=rank, score=8)


def page(n: int, *kinds: StatementType, negatives: tuple[str, ...] = ()) -> PageScore:
    return PageScore(
        page_no=n,
        type_scores={t: 8 for t in kinds},
        title_types=list(kinds),
        negatives=list(negatives),
    )


def structured_source(
    rows: list[tuple[int, list[list[str]]]],
    ranges: list[StatementRange],
    scores: list[PageScore],
    titles: tuple[tuple[int, int, str], ...] | list[tuple[int, int, str]] = (),
) -> StructureInputs:
    tables = [table(i, n, r) for i, (n, r) in enumerate(rows)]
    texts = [
        {
            "self_ref": f"#/texts/{i}",
            "text": text,
            "label": "section_header",
            "prov": [
                {
                    "page_no": n,
                    "bbox": {"l": 50, "t": y, "r": 700, "b": y + 10, "coord_origin": "TOPLEFT"},
                }
            ],
        }
        for i, (n, y, text) in enumerate(titles)
    ]
    doc = DlDocument.model_validate(
        {
            "tables": tables,
            "texts": texts,
            "pages": {
                str(n): {"page_no": n, "size": {"width": 800, "height": 1000}} for n, _ in rows
            },
        }
    )
    if len(rows) > 1 and rows[0][0] == rows[1][0]:
        tail = doc.tables[1]
        shifted = [
            c.model_copy(
                update={
                    "bbox": c.bbox.model_copy(
                        update={"top": c.bbox.top + 350, "bottom": c.bbox.bottom + 350}
                    )
                }
            )
            if c.bbox
            else c
            for c in tail.data.table_cells
        ]
        doc.tables[1] = tail.model_copy(
            update={"data": tail.data.model_copy(update={"table_cells": shifted})}
        )
    base = inputs([("docling/source.json", doc)])
    where = base.located.model_copy(
        update={
            "document": base.located.document.model_copy(update={"page_count": 65}),
            "ranges": ranges,
            "pages": scores,
        }
    )
    return base.model_copy(update={"located": where})
