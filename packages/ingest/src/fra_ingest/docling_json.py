"""The parts of docling's JSON that the structure stage reads.

Structure reads Part 2's output without importing docling or docling-core. Only the fields
used are modelled, everything else is ignored, and a golden test fails if docling's JSON stops
matching them (spec 11, R31).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from fra_core.schemas import BBox


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class DlBox(_Model):
    left: float = Field(alias="l")
    top: float = Field(alias="t")
    right: float = Field(alias="r")
    bottom: float = Field(alias="b")
    coord_origin: Literal["TOPLEFT", "BOTTOMLEFT"]

    def to_bbox(self, page_height: float) -> BBox | None:
        """PDF points with a top-left origin, or None for a box with no area."""
        if self.coord_origin == "TOPLEFT":
            top, bottom = self.top, self.bottom
        else:
            top, bottom = page_height - self.top, page_height - self.bottom
        left, right = min(self.left, self.right), max(self.left, self.right)
        top, bottom = min(top, bottom), max(top, bottom)
        if right <= left or bottom <= top:
            return None
        return BBox(left=left, top=top, right=right, bottom=bottom)


class DlProv(_Model):
    page_no: int
    bbox: DlBox


class DlCell(_Model):
    text: str = ""
    bbox: DlBox | None = None
    row_span: int = 1
    col_span: int = 1
    start_row_offset_idx: int
    start_col_offset_idx: int
    column_header: bool = False
    row_header: bool = False
    row_section: bool = False


class DlTableData(_Model):
    num_rows: int
    num_cols: int
    table_cells: list[DlCell] = Field(default_factory=list)


class DlTable(_Model):
    self_ref: str
    prov: list[DlProv] = Field(min_length=1)
    data: DlTableData


class DlText(_Model):
    self_ref: str
    label: str
    text: str = ""
    prov: list[DlProv] = Field(default_factory=list)


class DlSize(_Model):
    width: float
    height: float


class DlPage(_Model):
    page_no: int
    size: DlSize


class DlDocument(_Model):
    tables: list[DlTable] = Field(default_factory=list)
    texts: list[DlText] = Field(default_factory=list)
    pages: dict[str, DlPage] = Field(default_factory=dict)

    def page_size(self, page_no: int) -> DlSize:
        return self.pages[str(page_no)].size

    def texts_on(self, page_no: int) -> list[DlText]:
        return [t for t in self.texts if t.prov and t.prov[0].page_no == page_no]


def load_docling_json(path: Path) -> DlDocument:
    return DlDocument.model_validate_json(path.read_bytes())
