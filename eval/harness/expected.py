"""Expected files for the extraction eval, and drafts of them (spec 12, Data model).

    uv run python eval/harness/expected.py [--only ID] [--force]

An expected file holds what a document's statements print: periods, scale, currency and every
row's figures. A draft is written from the extraction with every figure listed as unconfirmed.
A period key leaves a row's ``unconfirmed`` list only when its figure has been compared with
the page image; the file becomes ``checked`` after two readings (05, V1). A checked file, or a
draft in which any figure has been confirmed, is never replaced without ``--force``.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from fra_core.schemas import Period, StatementType, TextSource
from fra_ingest.config import REPO_ROOT, load_config
from fra_ingest.results import StructureResult
from fra_ingest.structure import structure_pdf

MANIFEST = REPO_ROOT / "eval" / "golden" / "manifest.yaml"
EXPECTED_DIR = REPO_ROOT / "eval" / "golden" / "expected"
DRAFT_NOTE = "Prepared from the extraction. No figure has been compared with the page yet."


class ExpectedRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    values: dict[str, Decimal | None] = Field(
        description="By period key, as printed, before scale. None: nothing is printed there"
    )
    unconfirmed: list[str] = Field(
        default_factory=list, description="Period keys whose figure is not confirmed on the page"
    )


class ExpectedStatement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: StatementType
    pages: list[int]
    page_mode: Literal["digital", "scanned"]
    scale: int
    currency: str
    periods: list[Period]
    rows: list[ExpectedRow]


class ExpectedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    sha256: str
    status: Literal["draft", "checked"]
    checked_by: list[str] = Field(default_factory=list)
    note: str = ""
    statements: list[ExpectedStatement]

    @model_validator(mode="after")
    def _checked_means_read_twice(self) -> ExpectedFile:
        if self.status != "checked":
            return self
        if len(set(self.checked_by)) < 2:
            msg = "a checked file names its two readers in checked_by"
            raise ValueError(msg)
        if any(row.unconfirmed for s in self.statements for row in s.rows):
            msg = "a checked file has no unconfirmed figure"
            raise ValueError(msg)
        return self

    @property
    def confirmed_cells(self) -> int:
        return sum(
            1
            for s in self.statements
            for row in s.rows
            for key in row.values
            if key not in row.unconfirmed
        )


def draft_expected(document_id: str, result: StructureResult) -> ExpectedFile:
    """The first statement of each type, its rows that hold values, every figure unconfirmed."""
    statements: list[ExpectedStatement] = []
    seen: set[StatementType] = set()
    for s in result.statements:
        if s.type in seen:
            continue
        seen.add(s.type)
        cells = [c for item in s.line_items for c in item.cells]
        scanned = any(c.provenance.source is TextSource.OCR for c in cells)
        rows = [
            ExpectedRow(
                label=item.raw_label,
                values={c.period_key: c.reported for c in item.cells},
                unconfirmed=[c.period_key for c in item.cells],
            )
            for item in s.line_items
            if item.cells
        ]
        statements.append(
            ExpectedStatement(
                type=s.type,
                pages=s.source_pages,
                page_mode="scanned" if scanned else "digital",
                scale=s.scale,
                currency=s.currency,
                periods=s.periods,
                rows=rows,
            )
        )
    return ExpectedFile(
        id=document_id, sha256=result.sha256, status="draft", note=DRAFT_NOTE, statements=statements
    )


def load_expected(path: Path) -> ExpectedFile:
    return ExpectedFile.model_validate_json(path.read_text(encoding="utf-8"))


def write_draft(path: Path, draft: ExpectedFile, *, force: bool = False) -> bool:
    """Write the draft unless the file there is checked or has a confirmed figure."""
    if path.is_file() and not force:
        current = load_expected(path)
        if current.status == "checked" or current.confirmed_cells:
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(draft.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval/harness/expected.py")
    parser.add_argument("--only", default=None, help="one document id from the manifest")
    parser.add_argument("--force", action="store_true", help="replace checked or confirmed files")
    args = parser.parse_args(argv)
    config = load_config()
    documents = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["documents"]
    for entry in documents:
        if args.only and entry["id"] != args.only:
            continue
        result = structure_pdf(MANIFEST.parent / entry["file"], config, None)
        target = EXPECTED_DIR / f"{entry['id']}.json"
        written = write_draft(target, draft_expected(entry["id"], result), force=args.force)
        print(f"{entry['id']:34} {'written' if written else 'kept: checked or confirmed'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
