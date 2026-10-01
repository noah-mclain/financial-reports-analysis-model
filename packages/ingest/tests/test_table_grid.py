"""docling tables to grids (spec 11, Data flow step 2)."""

from fra_ingest.docling_json import DlDocument
from fra_ingest.table_grid import build_grid


def document(cells: list[dict[str, object]]) -> DlDocument:
    return DlDocument.model_validate(
        {
            "tables": [
                {
                    "self_ref": "#/tables/3",
                    "prov": [
                        {
                            "page_no": 7,
                            "bbox": {
                                "l": 0,
                                "t": 90,
                                "r": 500,
                                "b": 10,
                                "coord_origin": "BOTTOMLEFT",
                            },
                        }
                    ],
                    "data": {"num_rows": 2, "num_cols": 3, "table_cells": cells},
                }
            ],
            "pages": {"7": {"page_no": 7, "size": {"width": 600, "height": 800}}},
        }
    )


def cell(text: str, row: int, col: int, **extra: object) -> dict[str, object]:
    return {
        "text": text,
        "start_row_offset_idx": row,
        "end_row_offset_idx": row + 1,
        "start_col_offset_idx": col,
        "end_col_offset_idx": col + 1,
        "bbox": {
            "l": 100 * col + 10,
            "t": 20 * row + 5,
            "r": 100 * col + 90,
            "b": 20 * row + 15,
            "coord_origin": "TOPLEFT",
        },
        **extra,
    }


def test_cells_keep_position_header_flags_and_boxes() -> None:
    doc = document(
        [
            cell("  2025  ", 0, 1, column_header=True),
            cell("Revenue", 1, 0, row_header=True),
            cell("1,000", 1, 1),
        ]
    )
    grid = build_grid(doc.tables[0], doc, "docling/p7-7.json")
    assert (grid.page_no, grid.page_width, grid.num_rows, grid.num_cols) == (7, 600, 2, 3)
    assert grid.table_index == "t3"
    header = grid.cell(0, 1)
    assert header is not None and header.text == "2025" and header.is_column_header
    value = grid.cell(1, 1)
    assert value is not None and value.bbox is not None and value.bbox.left == 110
    assert grid.text(1, 0) == "Revenue"
    assert grid.cell(0, 0) is None and grid.text(0, 0) == ""
    assert grid.right_edge() == 190


def test_a_spanning_cell_covers_its_columns() -> None:
    doc = document([cell("For the year ended", 0, 1, col_span=2)])
    grid = build_grid(doc.tables[0], doc, "docling/p7-7.json")
    assert grid.text(0, 1) == grid.text(0, 2) == "For the year ended"


def test_a_cell_without_a_box_is_kept() -> None:
    raw = cell("12", 1, 2)
    raw.pop("bbox")
    doc = document([raw])
    grid = build_grid(doc.tables[0], doc, "docling/p7-7.json")
    kept = grid.cell(1, 2)
    assert kept is not None and kept.bbox is None
