"""Uploaded document plus per-page readability.

Text layer is tracked per page: Juhayna FY2025 EN standalone has text on pp. 1-2 and 6-61,
but pp. 3-5 are scans and the balance sheet is one of them.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class PageMode(StrEnum):
    """How the text of a page can be obtained."""

    TEXT = "text"
    """The page has a usable text layer."""

    IMAGE = "image"
    """The page is a scan and needs OCR."""


class PageProfile(BaseModel):
    """What one page looks like to the extractor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page_no: int = Field(ge=1, description="1-based, matching the page numbers docling reports")
    mode: PageMode
    char_count: int = Field(ge=0, description="Characters recovered from the text layer")
    width_pt: float = Field(gt=0)
    height_pt: float = Field(gt=0)

    @property
    def needs_ocr(self) -> bool:
        return self.mode is PageMode.IMAGE


class Document(BaseModel):
    """An uploaded file, identified by content rather than by name."""

    model_config = ConfigDict(extra="forbid")

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filename: str
    page_count: int = Field(ge=1)
    pages: list[PageProfile] = Field(default_factory=list)
    language: str = Field(default="en", description="Dominant script: 'en' or 'ar'")
    flags: list[str] = Field(default_factory=list)

    @property
    def scanned_pages(self) -> list[int]:
        return [p.page_no for p in self.pages if p.needs_ocr]

    @property
    def is_fully_scanned(self) -> bool:
        return bool(self.pages) and all(p.needs_ocr for p in self.pages)
