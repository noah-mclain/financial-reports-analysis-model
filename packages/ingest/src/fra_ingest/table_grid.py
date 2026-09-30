"""A docling table as a grid of cells with boxes in PDF points, top-left origin (spec 11)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from fra_core.schemas import BBox
from fra_ingest.docling_json import DlDocument, DlTable


class GridCell(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    row: int
    col: int
    row_span: int = 1
    col_span: int = 1
    bbox: BBox | None
    is_column_header: bool = False
    is_row_header: bool = False
    is_row_section: bool = False
    page_no: int
    flags: tuple[str, ...] = ()


class Grid(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    table_ref: str
    docling_path: str
    page_no: int
    page_width: float
    num_rows: int
    num_cols: int
    cells: tuple[GridCell, ...]

    def cell(self, row: int, col: int) -> GridCell | None:
        for c in self.cells:
            if c.row <= row < c.row + c.row_span and c.col <= col < c.col + c.col_span:
                return c
        return None

    def text(self, row: int, col: int) -> str:
        found = self.cell(row, col)
        return found.text if found else ""

    @property
    def table_index(self) -> str:
        return "t" + self.table_ref.rsplit("/", 1)[-1]

    def right_edge(self) -> float | None:
        rights = [c.bbox.right for c in self.cells if c.bbox is not None]
        return max(rights) if rights else None


def build_grid(table: DlTable, document: DlDocument, docling_path: str) -> Grid:
    page_no = table.prov[0].page_no
    size = document.page_size(page_no)
    cells = tuple(
        GridCell(
            text=" ".join(c.text.split()),
            row=c.start_row_offset_idx,
            col=c.start_col_offset_idx,
            row_span=max(1, c.row_span),
            col_span=max(1, c.col_span),
            bbox=c.bbox.to_bbox(size.height) if c.bbox is not None else None,
            is_column_header=c.column_header,
            is_row_header=c.row_header,
            is_row_section=c.row_section,
            page_no=page_no,
        )
        for c in table.data.table_cells
    )
    return Grid(
        table_ref=table.self_ref,
        docling_path=docling_path,
        page_no=page_no,
        page_width=size.width,
        num_rows=table.data.num_rows,
        num_cols=table.data.num_cols,
        cells=cells,
    )
